import asyncio
import logging
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from app.config import settings
from app.core.exceptions import EmailDeliveryError, EmailNotConfiguredError

logger = logging.getLogger(__name__)

IMPLICIT_TLS_PORT = 465


class SmtpEmailSender:
    async def send(self, *, to_email: str, subject: str, text_body: str, html_body: str) -> None:
        if not settings.smtp_configured:
            logger.error("SMTP settings are incomplete, email was not sent")
            raise EmailNotConfiguredError()

        message = EmailMessage()
        message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        use_implicit_tls = settings.smtp_port == IMPLICIT_TLS_PORT
        try:
            await aiosmtplib.send(
                message,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password.get_secret_value(),
                use_tls=use_implicit_tls,
                start_tls=settings.smtp_use_tls and not use_implicit_tls,
                timeout=settings.smtp_timeout_seconds,
            )
        except (aiosmtplib.SMTPException, OSError, asyncio.TimeoutError) as exc:
            logger.exception("Failed to send email through SMTP")
            raise EmailDeliveryError() from exc
