import logging
from dataclasses import dataclass

from app.config import settings
from app.integrations.smtp_email import SmtpEmailSender
from app.utils.email_templates import build_password_reset_email

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OtpDelivery:
    email: str
    name: str
    otp: str
    expires_in_minutes: int


class EmailService:
    def __init__(self, sender: SmtpEmailSender) -> None:
        self._sender = sender

    async def send_password_reset_otp(self, delivery: OtpDelivery) -> None:
        subject, text_body, html_body = build_password_reset_email(
            app_name=settings.app_name,
            user_name=delivery.name,
            otp=delivery.otp,
            expires_in_minutes=delivery.expires_in_minutes,
        )
        try:
            await self._sender.send(
                to_email=delivery.email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        except Exception:
            logger.exception("Password reset email could not be delivered")
