"""
backend/app/api/v1/resumes.py

Resume upload and management endpoints.

POST /api/v1/resumes/upload
    - Validates file (extension, magic bytes, size)
    - Stores under UUID filename
    - Runs full processing pipeline synchronously:
        1. PDF text extraction
        2. NLP skill extraction
        3. ML domain classification
    - Persists Resume + ExtractedSkill records
    - Returns processing result

GET  /api/v1/resumes
    - List all resumes (hr, admin) or own uploads (readonly)
GET  /api/v1/resumes/{resume_id}
    - Resume detail with extracted skills

DELETE /api/v1/resumes/{resume_id}
    - Admin-only soft delete (mark as failed/archived)
"""


import datetime
import json
import logging
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.dependencies import AdminUser, AnyAuthUser, HRUser, get_current_user
from app.config import get_settings
from app.database import get_db
from app.models.candidate import Candidate
from app.models.resume import Resume, ProcessingStatus
from app.models.skill import ExtractedSkill
from app.schemas.resume import (
    ATSCheckRequest,
    ATSCheckResponse,
    BulletRewriteRequest,
    BulletRewriteResponse,
    CoverLetterRequest,
    CoverLetterResponse,
    LiveResumeAnalysisRequest,
    LiveResumeAnalysisResponse,
    ResumeDetailRead,
    ResumeRead,
    ResumeUploadResponse,
)
from app.services import audit_service
from app.services import pdf_service
from app.services.pdf_service import PDFValidationError
from app.services import skill_service
from app.services import classification_service
from app.services.bullet_rewriter import rewrite_bullet_point
from app.services.cover_letter_service import generate_cover_letter
from app.services.resume_builder_service import analyze_live_resume
from app.services.ats_analyzer_service import analyze_ats_compatibility
from app.rate_limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/resumes", tags=["Resumes"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_resume_or_404(db: Session, resume_id: str) -> Resume:
    resume = db.query(Resume).filter(Resume.public_id == resume_id).first()
    if not resume:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")
    return resume


import asyncio
from app.services.websocket_manager import ws_manager


def _broadcast_pipeline_event(event_type: str, data: dict) -> None:
    """Safely emit WebSocket events without blocking pipeline execution."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast_event(event_type, data))
    except Exception as exc:
        logger.debug(f"WebSocket broadcast skipped ({exc})")


def _run_processing_pipeline(
    resume: Resume,
    file_path: str,
    db: Session,
) -> None:
    """
    Run the full processing pipeline on an uploaded PDF.

    Steps:
      1. Extract text from PDF
      2. Extract skills via NLP PhraseMatcher
      3. Classify domain with ML model
      4. Persist results
      5. Emit real-time WebSocket state events

    This function updates the Resume record in-place.
    Must be called within an active DB transaction.
    """
    resume.status = ProcessingStatus.PROCESSING
    _broadcast_pipeline_event(
        "resume.uploaded",
        {"resume_id": resume.public_id, "filename": resume.original_filename},
    )

    # --- Step 1: Extract text ---
    _broadcast_pipeline_event("resume.extracting", {"resume_id": resume.public_id})
    extraction = pdf_service.extract_text_from_path(file_path)

    if not extraction.success:
        resume.status = ProcessingStatus.FAILED
        resume.error_message = extraction.error
        resume.page_count = extraction.page_count
        resume.processed_at = datetime.datetime.now(datetime.timezone.utc)
        _broadcast_pipeline_event(
            "pipeline.failed",
            {"resume_id": resume.public_id, "error": extraction.error},
        )
        logger.warning(
            "PDF extraction failed for resume %s: %s",
            resume.public_id, extraction.error
        )
        return

    resume.extracted_text = extraction.text
    resume.text_char_count = extraction.char_count
    resume.page_count = extraction.page_count

    # --- Step 2: Skill extraction ---
    try:
        matched_skills = skill_service.match_skills(extraction.text)
    except ImportError as e:
        logger.warning("ML packages missing, skipping skill extraction: %s", e)
        matched_skills = []

    skill_objects = [
        ExtractedSkill(
            resume_id=resume.id,
            canonical_name=m.canonical_name,
            matched_text=m.matched_text,
            domain=m.domain,
            category=m.category,
            evidence_snippet=m.evidence_snippet,
            extraction_method=m.extraction_method,
            frequency=m.frequency,
        )
        for m in matched_skills
    ]

    for skill in skill_objects:
        db.add(skill)

    _broadcast_pipeline_event(
        "resume.nlp_extracted",
        {"resume_id": resume.public_id, "skill_count": len(skill_objects)},
    )

    # --- Step 3: ML Classification & OOD Policy Evaluation ---
    try:
        classification = classification_service.predict(extraction.text)
    except ImportError as e:
        logger.warning("ML packages missing, skipping classification: %s", e)
        from backend.app.services.classification_service import ClassificationResult
        classification = ClassificationResult(
            predicted_domain="Unknown (Lightweight Mode)",
            confidence_score=0.0,
            confidence_label="low",
            is_uncertain=True,
            status="accepted",
            review_required=True,
            ood_status="in_domain",
            policy_version="fallback",
            reason="ML features disabled on Vercel deployment"
        )

    resume.predicted_domain = classification.predicted_domain
    resume.prediction_confidence = classification.confidence_label
    resume.classification_status = classification.status
    resume.review_required = classification.review_required
    resume.ood_status = classification.ood_status
    resume.policy_version = classification.policy_version
    resume.policy_reason = classification.reason

    _broadcast_pipeline_event(
        "resume.classified",
        {
            "resume_id": resume.public_id,
            "predicted_domain": classification.predicted_domain,
            "prediction_confidence": classification.confidence_label,
            "status": classification.status,
            "review_required": classification.review_required,
            "ood_status": classification.ood_status,
            "policy_version": classification.policy_version,
            "reason": classification.reason,
        },
    )

    # Decide final status (NEEDS_REVIEW if review_required or low confidence)
    if classification.review_required or classification.is_uncertain:
        resume.status = ProcessingStatus.NEEDS_REVIEW
    else:
        resume.status = ProcessingStatus.COMPLETED

    resume.processed_at = datetime.datetime.now(datetime.timezone.utc)
    _broadcast_pipeline_event(
        "pipeline.completed",
        {
            "resume_id": resume.public_id,
            "status": resume.status,
            "predicted_domain": resume.predicted_domain,
            "confidence": resume.prediction_confidence,
            "review_required": classification.review_required,
        },
    )

    logger.info(
        "Processed resume %s: domain=%s confidence=%s skills=%d",
        resume.public_id,
        resume.predicted_domain,
        resume.prediction_confidence,
        len(skill_objects),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/upload",
    response_model=ResumeDetailRead,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and process a resume PDF",
)
@limiter.limit(get_settings().rate_limit_upload)
async def upload_resume(
    request: Request,
    current_user: HRUser,
    file: UploadFile = File(...),
    candidate_reference: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> Resume:
    """
    Upload a PDF resume. The file is:
      1. Validated (extension, MIME, magic bytes, size limit).
      2. Stored under a server-generated UUID filename.
      3. Processed synchronously (text extraction → skill extraction → classification).
      4. Persisted with full results.

    Returns the Resume record with extracted skills.
    The raw PDF text is NOT included in the response.
    """
    content = await file.read()
    original_filename = file.filename or "unnamed.pdf"

    # --- Validate ---
    try:
        pdf_service.validate_upload(
            filename=original_filename,
            content=content,
            content_type=file.content_type,
        )
    except PDFValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    # --- Save file ---
    stored_filename = pdf_service.generate_stored_filename(original_filename)
    try:
        file_path = pdf_service.save_upload(content, stored_filename)
    except OSError as exc:
        logger.error("Failed to save upload (details suppressed).")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store the uploaded file. Please try again.",
        )

    # --- Create or find candidate ---
    candidate: Candidate | None = None
    if candidate_reference:
        candidate = (
            db.query(Candidate)
            .filter(Candidate.reference_code == candidate_reference)
            .first()
        )
        if not candidate:
            candidate = Candidate(reference_code=candidate_reference)
            db.add(candidate)
            db.flush()
            
    if not candidate:
        candidate = Candidate(
            display_name=original_filename.replace('.pdf', '').replace('_', ' ').title()
        )
        db.add(candidate)
        db.flush()

    # --- Create Resume record ---
    resume = Resume(
        candidate_id=candidate.id,
        original_filename=original_filename,
        stored_filename=stored_filename,
        file_size_bytes=len(content),
        mime_type=file.content_type or "application/pdf",
        status=ProcessingStatus.UPLOADED,
        uploaded_by=current_user.id,
    )
    db.add(resume)
    db.flush()  # get resume.id before processing

    # --- Run pipeline ---
    _run_processing_pipeline(resume, file_path, db)

    db.commit()
    db.refresh(resume)

    # --- Audit ---
    audit_service.log_resume_access(
        db,
        actor_id=current_user.id,
        actor_email=current_user.email,
        resume_public_id=resume.public_id,
        action="upload",
        ip_address=request.client.host if request.client else None,
    )

    return resume


@router.post(
    "/upload-batch",
    response_model=List[ResumeDetailRead],
    status_code=status.HTTP_201_CREATED,
    summary="Batch upload and process multiple resume PDFs",
)
@limiter.limit(get_settings().rate_limit_upload)
async def upload_batch_resumes(
    request: Request,
    current_user: HRUser,
    files: List[UploadFile] = File(...),
    job_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> list[Resume]:
    """
    Upload and process multiple PDF resumes in a single batch.
    Optionally matches processed resumes immediately against a target job if job_id is provided.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided for batch upload.",
        )

    processed_resumes: list[Resume] = []
    job = None
    if job_id:
        job = db.query(Job).filter(Job.public_id == job_id).first()

    for file in files:
        content = await file.read()
        original_filename = file.filename or "unnamed.pdf"

        try:
            pdf_service.validate_upload(
                filename=original_filename,
                content=content,
                content_type=file.content_type,
            )
        except PDFValidationError as exc:
            logger.warning(f"Batch upload file validation skipped: {exc}")
            continue

        stored_filename = pdf_service.generate_stored_filename(original_filename)
        try:
            file_path = pdf_service.save_upload(content, stored_filename)
        except OSError:
            logger.error("Failed to store batch upload file.")
            continue

        candidate = Candidate(
            display_name=original_filename.replace('.pdf', '').replace('_', ' ').title()
        )
        db.add(candidate)
        db.flush()

        resume = Resume(
            candidate_id=candidate.id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            file_size_bytes=len(content),
            mime_type=file.content_type or "application/pdf",
            status=ProcessingStatus.UPLOADED,
            uploaded_by=current_user.id,
        )
        db.add(resume)
        db.flush()

        _run_processing_pipeline(resume, file_path, db)

        # Auto-match against job if provided
        if job and resume.status in (ProcessingStatus.COMPLETED, ProcessingStatus.NEEDS_REVIEW):
            try:
                candidate_skills = resume.extracted_skills
                req_skills, pref_skills = matching_service.extract_job_skills(job.requirements)
                match = matching_service.compute_match(req_skills, pref_skills, candidate_skills)
                
                domain_bonus = 0.0
                if (
                    resume.predicted_domain
                    and job.domain
                    and resume.predicted_domain.lower() == job.domain.lower()
                    and resume.prediction_confidence in ("high", "medium")
                ):
                    domain_bonus = 10.0 if resume.prediction_confidence == "high" else 5.0

                relevance_score = min(100.0, round(match.combined_match + domain_bonus, 2))

                score_breakdown = {
                    **match.score_breakdown,
                    "domain_bonus": domain_bonus,
                    "final_relevance_score": relevance_score,
                    "missing_required_skills": match.missing_required,
                    "matched_evidence_snippets": match.matched_evidence,
                }

                existing_scr = db.query(ScreeningResult).filter(
                    ScreeningResult.resume_id == resume.id,
                    ScreeningResult.job_id == job.id,
                ).first()

                if not existing_scr:
                    scr = ScreeningResult(
                        resume_id=resume.id,
                        job_id=job.id,
                        candidate_id=resume.candidate_id,
                        required_skill_coverage=match.required_coverage,
                        preferred_skill_coverage=match.preferred_coverage,
                        combined_skill_match=match.combined_match,
                        matched_required_skills=json.dumps(match.matched_required),
                        missing_required_skills=json.dumps(match.missing_required),
                        matched_preferred_skills=json.dumps(match.matched_preferred),
                        predicted_domain=resume.predicted_domain,
                        prediction_confidence=resume.prediction_confidence,
                        relevance_score=relevance_score,
                        score_breakdown=json.dumps(score_breakdown),
                        review_status=ReviewStatus.PENDING,
                        screened_at=datetime.datetime.now(datetime.timezone.utc),
                    )
                    db.add(scr)
            except Exception as exc:
                logger.error(f"Auto-match in batch upload failed for {resume.public_id}: {exc}")

        processed_resumes.append(resume)

    db.commit()
    for r in processed_resumes:
        db.refresh(r)

    return processed_resumes


@router.get("", response_model=List[ResumeRead], summary="List resumes")
def list_resumes(
    current_user: AnyAuthUser,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
) -> list[Resume]:
    """
    List resumes.
    - Admin/HR: see all resumes.
    - Readonly: see only resumes they uploaded (none, typically — readonly role is view-only).
    """
    q = db.query(Resume)
    if current_user.role == "readonly":
        q = q.filter(Resume.uploaded_by == current_user.id)
    if status_filter and status_filter in ProcessingStatus.ALL:
        q = q.filter(Resume.status == status_filter)
    return q.order_by(Resume.uploaded_at.desc()).all()


@router.get("/{resume_id}", response_model=ResumeDetailRead, summary="Get resume detail")
def get_resume(
    resume_id: str,
    request: Request,
    current_user: AnyAuthUser,
    db: Session = Depends(get_db),
) -> Resume:
    """Get a resume by public_id, including extracted skills. Text is never returned."""
    resume = _get_resume_or_404(db, resume_id)

    # Readonly users can only see resumes they uploaded
    if current_user.role == "readonly" and resume.uploaded_by != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied.")

    audit_service.log_resume_access(
        db,
        actor_id=current_user.id,
        actor_email=current_user.email,
        resume_public_id=resume.public_id,
        action="view",
        ip_address=request.client.host if request.client else None,
    )

    return resume


@router.delete(
    "/{resume_id}",
    status_code=status.HTTP_200_OK,
    summary="Archive a resume",
)
def archive_resume(
    resume_id: str,
    current_user: HRUser,
    db: Session = Depends(get_db),
) -> dict:
    """Mark a resume as failed/archived. The file on disk is NOT deleted (audit trail)."""
    resume = _get_resume_or_404(db, resume_id)
    resume.status = ProcessingStatus.FAILED
    resume.error_message = f"Archived by admin {current_user.email}."
    db.commit()
    return {"detail": f"Resume {resume_id} archived."}


@router.post(
    "/rewrite-bullet",
    response_model=BulletRewriteResponse,
    status_code=status.HTTP_200_OK,
    summary="Rewrite and optimize a resume bullet point using AI",
)
@limiter.limit(get_settings().rate_limit_default)
def rewrite_bullet(
    request: Request,
    payload: BulletRewriteRequest,
    current_user: AnyAuthUser,
) -> BulletRewriteResponse:
    """
    Rewrite a candidate resume bullet point using STAR, Technical, or ATS optimization modes.
    Enforces strict anti-hallucination factual guardrails and metric placeholder injection.
    """
    result = rewrite_bullet_point(
        bullet=payload.bullet,
        mode=payload.mode,
        target_job_title=payload.target_job_title,
        target_skills=payload.target_skills,
    )

    return BulletRewriteResponse(
        original_bullet=result.original_bullet,
        optimized_bullet=result.optimized_bullet,
        mode=result.mode,
        key_changes=result.key_changes,
        action_verb_used=result.action_verb_used,
        placeholders_needed=result.placeholders_needed,
        tokens_used=result.tokens_used,
        estimated_cost_usd=result.estimated_cost_usd,
        guardrail_warnings=result.guardrail_warnings,
    )


@router.post(
    "/generate-cover-letter",
    response_model=CoverLetterResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate a tailored, professional cover letter",
)
@limiter.limit(get_settings().rate_limit_default)
def create_cover_letter(
    request: Request,
    payload: CoverLetterRequest,
    current_user: AnyAuthUser,
) -> CoverLetterResponse:
    """
    Generate a personalized cover letter matching candidate background to target job title and company.
    Enforces anti-hallucination factual guardrails.
    """
    result = generate_cover_letter(
        job_title=payload.job_title,
        company_name=payload.company_name,
        candidate_name=payload.candidate_name,
        candidate_skills=payload.candidate_skills,
        candidate_text=payload.candidate_text,
        job_description=payload.job_description,
        tone=payload.tone,
    )

    return CoverLetterResponse(
        salutation=result.salutation,
        opening_hook=result.opening_hook,
        core_value_proposition=result.core_value_proposition,
        company_alignment_paragraph=result.company_alignment_paragraph,
        closing_call_to_action=result.closing_call_to_action,
        full_cover_letter=result.full_cover_letter,
        tone_used=result.tone_used,
        tokens_used=result.tokens_used,
        estimated_cost_usd=result.estimated_cost_usd,
        guardrail_warnings=result.guardrail_warnings,
    )


@router.post(
    "/live-analysis",
    response_model=LiveResumeAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Real-time debounced live resume builder analysis",
)
@limiter.limit(get_settings().rate_limit_default)
def live_resume_analysis(
    request: Request,
    payload: LiveResumeAnalysisRequest,
    current_user: AnyAuthUser,
) -> LiveResumeAnalysisResponse:
    """
    Perform instantaneous, debounced ATS readability scoring, skill extraction,
    and job match simulation on raw resume text or markdown.
    """
    result = analyze_live_resume(
        resume_markdown=payload.resume_markdown,
        job_description=payload.job_description,
        target_skills=payload.target_skills,
    )

    return LiveResumeAnalysisResponse(
        char_count=result.char_count,
        word_count=result.word_count,
        estimated_pages=result.estimated_pages,
        ats_score=result.ats_score,
        extracted_skills=result.extracted_skills,
        matched_skills=result.matched_skills,
        missing_skills=result.missing_skills,
        live_match_score=result.live_match_score,
        ats_warnings=result.ats_warnings,
        suggestions=result.suggestions,
    )


@router.post(
    "/ats-check",
    response_model=ATSCheckResponse,
    status_code=status.HTTP_200_OK,
    summary="Run comprehensive ATS compatibility scan on raw text",
)
@limiter.limit(get_settings().rate_limit_default)
def check_ats_compatibility(
    request: Request,
    payload: ATSCheckRequest,
    current_user: AnyAuthUser,
) -> ATSCheckResponse:
    """
    Run 4-dimension ATS compatibility scan on provided resume text.
    Dimensions: Document Structure (25%), Readability (25%), Contact Info (25%), Keyword Density (25%).
    """
    result = analyze_ats_compatibility(payload.resume_text)

    return ATSCheckResponse(
        overall_score=result.overall_score,
        structure_score=result.structure_score,
        readability_score=result.readability_score,
        contact_score=result.contact_score,
        density_score=result.density_score,
        compliance_category=result.compliance_category,
        detected_sections=result.detected_sections,
        missing_essential_sections=result.missing_essential_sections,
        critical_issues=result.critical_issues,
        warnings=result.warnings,
        actionable_recommendations=result.actionable_recommendations,
    )


@router.post(
    "/{resume_id}/ats-check",
    response_model=ATSCheckResponse,
    status_code=status.HTTP_200_OK,
    summary="Run ATS compatibility scan on an uploaded resume record",
)
@limiter.limit(get_settings().rate_limit_default)
def check_uploaded_resume_ats(
    resume_id: str,
    request: Request,
    current_user: AnyAuthUser,
    db: Session = Depends(get_db),
) -> ATSCheckResponse:
    """
    Run ATS compatibility analysis on an existing uploaded resume record.
    """
    resume = _get_resume_or_404(db, resume_id)
    if not resume.extracted_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Resume has no extracted text to analyze.",
        )

    result = analyze_ats_compatibility(resume.extracted_text)

    return ATSCheckResponse(
        overall_score=result.overall_score,
        structure_score=result.structure_score,
        readability_score=result.readability_score,
        contact_score=result.contact_score,
        density_score=result.density_score,
        compliance_category=result.compliance_category,
        detected_sections=result.detected_sections,
        missing_essential_sections=result.missing_essential_sections,
        critical_issues=result.critical_issues,
        warnings=result.warnings,
        actionable_recommendations=result.actionable_recommendations,
    )


from app.schemas.candidate import CandidateATSAnalysisResponse
from app.services.candidate_ats_service import analyze_candidate_resume
import tempfile
import os

@router.post(
    "/candidate-analyze",
    response_model=CandidateATSAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Candidate Portal - REAL PDF ATS Analyzer",
)
@limiter.limit(get_settings().rate_limit_default)
async def analyze_candidate_pdf(
    request: Request,
    current_user: AnyAuthUser,
    file: UploadFile = File(...),
    job_description: str = Form(None),
) -> CandidateATSAnalysisResponse:
    """
    Extracts text from the PDF, performs OCR if necessary, 
    and runs a comprehensive ATS Analysis for the Candidate Portal.
    Does NOT save the candidate to the recruiter database.
    """
    content = await file.read()
    
    # 1. Validate Upload
    try:
        pdf_service.validate_upload(file.filename or "resume.pdf", content, file.content_type)
    except PDFValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    # 2. Save to Temp File and Extract
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
        
    try:
        extraction = pdf_service.extract_text_from_path(tmp_path)
        if not extraction.success or not extraction.text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, 
                detail=extraction.error or "Failed to extract text from PDF."
            )
            
        # 3. Analyze
        result = analyze_candidate_resume(extraction.text, job_description)
        return result
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
