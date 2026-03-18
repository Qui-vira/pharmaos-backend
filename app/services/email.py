"""
PharmaOS AI - Email Service
Sends verification codes and transactional emails via SMTP.
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.core.config import settings

logger = logging.getLogger(__name__)


def send_verification_email(to_email: str, code: str, full_name: str = "User") -> bool:
    """
    Send a 6-digit verification code to the user's email.
    Returns True on success, False on failure.
    Never logs the actual code.
    """
    if not all([settings.SMTP_HOST, settings.SMTP_USER, settings.SMTP_PASSWORD]):
        logger.warning("SMTP not configured — skipping email send to %s", to_email)
        return False

    subject = f"PharmaOS — Your verification code"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
        <div style="background: #0d9488; padding: 16px 24px; border-radius: 12px 12px 0 0;">
            <h2 style="color: white; margin: 0;">PharmaOS AI</h2>
        </div>
        <div style="background: #f8fafc; padding: 24px; border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 12px 12px;">
            <p style="color: #334155;">Hi {full_name},</p>
            <p style="color: #334155;">Your verification code is:</p>
            <div style="text-align: center; margin: 24px 0;">
                <span style="font-size: 32px; font-weight: bold; letter-spacing: 8px; color: #0d9488; background: #f0fdfa; padding: 12px 24px; border-radius: 8px; border: 2px dashed #0d9488;">{code}</span>
            </div>
            <p style="color: #64748b; font-size: 14px;">This code expires in <strong>10 minutes</strong>.</p>
            <p style="color: #64748b; font-size: 14px;">If you didn't request this, please ignore this email.</p>
            <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0;">
            <p style="color: #94a3b8; font-size: 12px; text-align: center;">PharmaOS AI — Pharmacy Management Platform</p>
        </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL or settings.SMTP_USER
    msg["To"] = to_email
    msg.attach(MIMEText(f"Your PharmaOS verification code is: {code}\nExpires in 10 minutes.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Verification email sent to %s", to_email)
        return True
    except Exception:
        logger.exception("Failed to send verification email to %s", to_email)
        return False
