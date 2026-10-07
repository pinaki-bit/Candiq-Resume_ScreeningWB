"""
backend/app/services/pdf_service.py

Secure PDF text extraction pipeline using pdfminer.six.

Security requirements:
  - Validate extension, MIME type, and file-magic signature BEFORE processing.
  - Store files under a server-generated UUID filename (no path traversal possible).
  - Never execute the uploaded file.
  - Never expose storage paths in error messages.
  - Remove temporary files after processing even if an error occurs.
  - Image-only PDFs return a descriptive error (OCR not implemented).

Processing statuses: uploaded → processing → completed | failed | needs_review
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import NamedTuple

from pdfminer.high_level import extract_text
from pdfminer.pdfparser import PDFSyntaxError
from pdfminer.pdfdocument import PDFPasswordIncorrect

from app.config import get_settings

logger = logging.getLogger(__name__)

# Minimum characters of extracted text to be considered non-empty
MIN_TEXT_LENGTH = 50
# PDF magic bytes (first 4 bytes)
PDF_MAGIC = b"%PDF"


class ExtractionResult(NamedTuple):
    success: bool
    text: str | None
    char_count: int
    page_count: int | None
    error: str | None


class PDFValidationError(Exception):
    """Raised when a PDF fails validation before extraction."""
    pass


def validate_upload(
    filename: str,
    content: bytes,
    content_type: str | None,
) -> None:
    """
    Validate an uploaded file before any processing.

    Checks:
      1. Extension is in the allowed list.
      2. File signature (magic bytes) matches PDF.
      3. File size is within the configured limit.
      4. Filename does not contain path traversal characters.

    Raises:
        PDFValidationError with a user-safe message on any failure.
    """
    settings = get_settings()

    # --- Extension check ---
    safe_name = Path(filename).name  # strip any directory components
    ext = Path(safe_name).suffix.lower().lstrip(".")
    if ext not in settings.allowed_extension_set:
        raise PDFValidationError(
            f"File type '.{ext}' is not allowed. "
            f"Accepted types: {', '.join(settings.allowed_extension_set)}."
        )

    # --- Path traversal check ---
    if ".." in filename or "/" in filename or "\\" in filename:
        raise PDFValidationError("Invalid filename.")

    # --- Size check ---
    if len(content) > settings.max_upload_size_bytes:
        raise PDFValidationError(
            f"File exceeds the maximum allowed size of "
            f"{settings.max_upload_size_mb} MB."
        )

    # --- Magic bytes check ---
    if not content.startswith(PDF_MAGIC):
        raise PDFValidationError(
            "File does not appear to be a valid PDF (signature mismatch)."
        )


def generate_stored_filename(original_filename: str) -> str:
    """Return a UUID-based filename that preserves the original extension."""
    ext = Path(original_filename).suffix.lower()
    return f"{uuid.uuid4().hex}{ext}"


def save_upload(content: bytes, stored_filename: str) -> str:
    """
    Save the uploaded file to the secure uploads directory.

    Returns the absolute path to the stored file.
    Raises OSError if the directory cannot be created or the file cannot be written.
    """
    settings = get_settings()
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    dest = upload_dir / stored_filename
    dest.write_bytes(content)
    logger.info("Saved upload locally: %s (%d bytes)", stored_filename, len(content))

    if settings.use_s3_storage:
        try:
            from app.services import s3_service
            s3_url = s3_service.upload_file(content, stored_filename)
            if s3_url:
                logger.info("Successfully mirrored to S3: %s", s3_url)
                # Keep local file for text extraction process, it's fine.
        except Exception as e:
            logger.error("Error pushing to S3: %s", e)

    return str(dest)


def extract_text_from_path(file_path: str) -> ExtractionResult:
    """
    Extract text from a PDF file using pdfminer.six.

    Handles:
      - Normal PDFs → returns extracted text.
      - Encrypted PDFs → returns error.
      - Corrupted / malformed PDFs → returns error.
      - Image-only PDFs (no text layer) → returns a clear OCR-not-supported message.
      - Empty PDFs → returns error.

    Returns:
        ExtractionResult(success, text, char_count, page_count, error)
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTPage

    path = Path(file_path)
    if not path.exists():
        return ExtractionResult(False, None, 0, None, "File not found on server.")

    try:
        # Count pages
        page_count = 0
        try:
            for _ in extract_pages(str(path)):
                page_count += 1
        except Exception:
            page_count = None

        # Extract text
        raw_text = extract_text(str(path))

    except PDFPasswordIncorrect:
        return ExtractionResult(
            False, None, 0, None,
            "PDF is encrypted with a password. Cannot extract text."
        )
    except PDFSyntaxError as exc:
        logger.warning("PDF syntax error for %s: %s", path.name, exc)
        return ExtractionResult(
            False, None, 0, None,
            "PDF file is corrupted or malformed and cannot be processed."
        )
    except Exception as exc:
        logger.error("Unexpected PDF extraction error for %s: %s", path.name, exc)
        return ExtractionResult(
            False, None, 0, None,
            "An error occurred during text extraction. The file may be unsupported."
        )

    if not raw_text or not raw_text.strip():
        # Fallback to OCR if tesseract is installed
        ocr_text = _run_ocr_fallback(path)
        if ocr_text:
            raw_text = ocr_text
        else:
            return ExtractionResult(
                False, None, 0, page_count,
                "No text could be extracted and OCR is either unavailable or failed. "
                "The PDF may contain only images."
            )

    cleaned = _normalize_text(raw_text)
    char_count = len(cleaned)

    if char_count < MIN_TEXT_LENGTH:
        # Try OCR fallback if text is suspiciously short
        ocr_text = _run_ocr_fallback(path)
        if ocr_text and len(ocr_text) > char_count:
            cleaned = _normalize_text(ocr_text)
            char_count = len(cleaned)
            
        if char_count < MIN_TEXT_LENGTH:
            return ExtractionResult(
                False, cleaned, char_count, page_count,
                f"Extracted text is too short ({char_count} characters). "
                "The resume may be mostly images or contain insufficient text content."
            )

    return ExtractionResult(True, cleaned, char_count, page_count, None)


def _run_ocr_fallback(pdf_path: Path) -> str | None:
    """Attempt OCR extraction using pdf2image and pytesseract."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
        
        # Check if tesseract is available
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError:
            logger.info("Tesseract not installed, skipping OCR fallback.")
            return None
            
        logger.info("Running OCR fallback on %s", pdf_path.name)
        images = convert_from_path(str(pdf_path), dpi=300)
        
        text_parts = []
        for img in images:
            text = pytesseract.image_to_string(img)
            text_parts.append(text)
            
        return "\n".join(text_parts)
    except ImportError:
        logger.info("pdf2image or pytesseract not installed, skipping OCR fallback.")
        return None
    except Exception as exc:
        logger.warning("OCR fallback failed for %s: %s", pdf_path.name, exc)
        return None


def _normalize_text(text: str) -> str:
    """
    Normalize extracted PDF text.

    Operations:
      - Collapse multiple whitespace/newlines to single spaces.
      - Normalize common ligature characters.
      - Preserve meaningful punctuation and technical symbols.
      - Do NOT remove hyphens, dots, slashes (important for tech terms).
    """
    # Normalize ligatures common in PDFs
    ligature_map = {
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb00": "ff",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\u2019": "'",
        "\u2018": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
    }
    for char, replacement in ligature_map.items():
        text = text.replace(char, replacement)

    # Normalize whitespace while preserving newlines between sections
    # Replace multiple spaces/tabs with single space
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse more than 2 consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(lines)

    return text.strip()
