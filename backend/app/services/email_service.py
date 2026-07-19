from __future__ import annotations

import structlog
from app.core.config import Settings

log = structlog.get_logger()


async def send_verification_email(
    settings: Settings, to_email: str, name: str, token: str
) -> None:
    verify_url = f"{settings.frontend_url}/auth/verify-email?token={token}"
    await _send(settings, to_email, "Verify your NeuroNova account", _verification_html(name, verify_url))


async def send_password_reset_email(
    settings: Settings, to_email: str, name: str, token: str
) -> None:
    reset_url = f"{settings.frontend_url}/auth/reset-password?token={token}"
    await _send(settings, to_email, "Reset your NeuroNova password", _reset_html(name, reset_url))


async def _send(settings: Settings, to: str, subject: str, html: str) -> None:
    if not settings.smtp_host:
        log.warning("email.skipped", reason="SMTP_HOST not configured", to=to, subject=subject)
        return
    try:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["From"] = settings.smtp_from_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))

        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            use_tls=settings.smtp_tls,
        )
        log.info("email.sent", to=to, subject=subject)
    except Exception as exc:
        log.error("email.failed", to=to, error=str(exc))


def _verification_html(name: str, url: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto;padding:32px">
      <h2 style="color:#1e1e2e">Welcome to NeuroNova, {name}!</h2>
      <p>Please verify your email address to get started.</p>
      <a href="{url}" style="display:inline-block;padding:12px 28px;background:#6366f1;
         color:white;text-decoration:none;border-radius:6px;font-weight:600">
        Verify Email
      </a>
      <p style="color:#888;font-size:12px;margin-top:24px">
        This link expires in 24 hours. If you did not create an account, ignore this email.
      </p>
    </div>"""


def _reset_html(name: str, url: str) -> str:
    return f"""
    <div style="font-family:sans-serif;max-width:600px;margin:auto;padding:32px">
      <h2 style="color:#1e1e2e">Reset your NeuroNova password</h2>
      <p>Hi {name}, we received a request to reset your password.</p>
      <a href="{url}" style="display:inline-block;padding:12px 28px;background:#6366f1;
         color:white;text-decoration:none;border-radius:6px;font-weight:600">
        Reset Password
      </a>
      <p style="color:#888;font-size:12px;margin-top:24px">
        This link expires in 1 hour. If you did not request this, you can safely ignore this email.
      </p>
    </div>"""
