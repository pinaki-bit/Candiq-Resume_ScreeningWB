"""
backend/app/services/email_service.py

Mock email service for sending secure notifications (e.g. system health, security alerts).
In a production environment, this would integrate with SendGrid, SES, or Mailgun.
"""

import logging
import json
import os
from datetime import datetime
from pathlib import Path
try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    import resend
except ImportError:
    resend = None

from app.config import get_settings

logger = logging.getLogger(__name__)

# In-memory mock inbox for testing
_MOCK_INBOX = []


class EmailNotification:
    def __init__(self, to_email: str, subject: str, body: str, is_secure: bool = True):
        self.to_email = to_email
        self.subject = subject
        self.body = body
        self.is_secure = is_secure
        self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self):
        return {
            "to": self.to_email,
            "subject": self.subject,
            "body": self.body,
            "secure": self.is_secure,
            "timestamp": self.timestamp
        }


def send_security_alert(user_email: str, event_type: str, details: str) -> bool:
    """Send a high-priority security alert."""
    subject = f"SECURITY ALERT: {event_type}"
    body = f"A security event was detected on your account.\n\nDetails: {details}\n\nIf you did not authorize this, contact your administrator immediately."
    
    notification = EmailNotification(user_email, subject, body, is_secure=True)
    _mock_send(notification)
    return True


def send_system_health_report(admin_email: str, metrics: dict) -> bool:
    """Send a daily or weekly system health report."""
    subject = "System Health Report"
    body = f"Current System Telemetry:\n\n{json.dumps(metrics, indent=2)}"
    
    notification = EmailNotification(admin_email, subject, body, is_secure=True)
    _mock_send(notification)
    return True


def send_candidate_stage_update(candidate_email: str, candidate_name: str, job_title: str, stage: str, custom_notes: str = None) -> bool:
    """Send automated status updates to candidates based on pipeline stage."""
    stage_display = stage.title()
    subject = ""
    body = ""
    
    # ---------------------------------------------------------
    # SMART GENERATION (LLM) & REAL EMAIL SENDING FOR "HIRED"
    # ---------------------------------------------------------
    if stage == "hired":
        settings = get_settings()
        gemini_key = settings.gemini_api_key
        resend_key = settings.resend_api_key
        
        if gemini_key and resend_key:
            try:
                # 1. Generate customized onboarding email via Gemini
                genai.configure(api_key=gemini_key)
                model = genai.GenerativeModel("gemini-1.5-flash")
                
                prompt = f"""
                You are the Hiring Manager at Candiq. Write a warm, enthusiastic, and highly professional
                welcome email to {candidate_name} who was just hired for the role of {job_title}.
                Include next steps for onboarding and tell them how excited we are to have them on the team.
                Keep it under 3 paragraphs.
                """
                if custom_notes:
                    prompt += f"\nInclude this specific note from the team: {custom_notes}"
                    
                response = model.generate_content(prompt)
                smart_body = response.text.strip()
                smart_subject = f"Welcome to Candiq, {candidate_name}! (Next Steps for {job_title})"
                
                # 2. Send the real email via Resend
                resend.api_key = resend_key
                resend.Emails.send({
                    "from": "onboarding@resend.dev", # Uses Resend's testing domain by default
                    "to": candidate_email,
                    "subject": smart_subject,
                    "text": smart_body
                })
                
                logger.info(f"REAL SMART EMAIL SENT to {candidate_email} via Resend!")
                return True
                
            except Exception as e:
                logger.error(f"Failed to send smart email via Gemini/Resend: {e}")
                # Fallback to mock email if API fails
        
        # Fallback if keys are missing or API fails
        subject = f"Welcome to the team! ({job_title})"
        body = f"Hi {candidate_name},\n\nWelcome to Candiq! We are incredibly excited to have you join us as our new {job_title}.\n\nHR will reach out shortly with your onboarding packet."
        
    # ---------------------------------------------------------
    # STANDARD TEMPLATES (Mock Send)
    # ---------------------------------------------------------
    elif stage == "interview":
        subject = f"Interview Invitation: {job_title} at Candiq"
        body = f"Hi {candidate_name},\n\nWe are excited to invite you to an interview for the {job_title} position! Our team was very impressed by your background.\n\nPlease let us know your availability for next week."
    elif stage == "rejected":
        subject = f"Update on your application for {job_title}"
        body = f"Hi {candidate_name},\n\nThank you for applying for the {job_title} position. While your qualifications are impressive, we have decided to move forward with other candidates who more closely align with our current needs.\n\nWe will keep your resume on file for future opportunities."
    elif stage == "offer":
        subject = f"Job Offer: {job_title} at Candiq!"
        body = f"Hi {candidate_name},\n\nCongratulations! We are thrilled to offer you the {job_title} position. We will be sending over the official offer letter and compensation details shortly."
    else:
        subject = f"Application Update: {job_title} - {stage_display}"
        body = f"Hi {candidate_name},\n\nYour application for {job_title} has been moved to the '{stage_display}' stage. We will be in touch with the next steps soon."

    if custom_notes and stage != "hired": # handled in prompt for hired
        body += f"\n\nAdditional Notes from our team:\n{custom_notes}"

    if stage != "hired":
        body += "\n\nBest regards,\nThe Candiq Hiring Team"
    
    notification = EmailNotification(candidate_email, subject, body, is_secure=False)
    _mock_send(notification)
    return True


def _mock_send(notification: EmailNotification) -> None:
    """Mock sending the email by logging it and storing it in memory."""
    _MOCK_INBOX.append(notification)
    logger.info("MOCK EMAIL SENT to %s: [%s]", notification.to_email, notification.subject)
    
    # Optionally write to a local log file for verification
    log_path = Path("email_logs.json")
    try:
        if log_path.exists():
            logs = json.loads(log_path.read_text())
        else:
            logs = []
        logs.append(notification.to_dict())
        log_path.write_text(json.dumps(logs, indent=2))
    except Exception as exc:
        logger.warning("Could not write email log: %s", exc)


def get_mock_inbox() -> list[dict]:
    """Retrieve all sent mock emails (useful for testing)."""
    return [msg.to_dict() for msg in _MOCK_INBOX]
