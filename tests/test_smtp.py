import aiosmtplib
import pytest

from app.core.exceptions import EmailDeliveryError, EmailNotConfiguredError
from app.integrations import smtp_email
from app.integrations.smtp_email import SmtpEmailSender


async def test_sender_builds_message_and_uses_starttls(monkeypatch):
    captured = {}

    async def fake_send(message, **kwargs):
        captured["message"] = message
        captured["kwargs"] = kwargs

    monkeypatch.setattr(smtp_email.aiosmtplib, "send", fake_send)
    await SmtpEmailSender().send(
        to_email="user@example.com", subject="Subject", text_body="Plain", html_body="<p>Html</p>"
    )

    message = captured["message"]
    assert message["To"] == "user@example.com"
    assert message["Subject"] == "Subject"
    assert "sender@test.local" in message["From"]
    assert message.is_multipart()
    assert captured["kwargs"]["hostname"] == "smtp.test.local"
    assert captured["kwargs"]["start_tls"] is True
    assert captured["kwargs"]["use_tls"] is False
    assert captured["kwargs"]["password"] == "testpasswordvalue"


async def test_sender_wraps_smtp_failures(monkeypatch):
    async def failing_send(message, **kwargs):
        raise aiosmtplib.SMTPAuthenticationError(535, "Authentication failed")

    monkeypatch.setattr(smtp_email.aiosmtplib, "send", failing_send)
    with pytest.raises(EmailDeliveryError):
        await SmtpEmailSender().send(
            to_email="user@example.com", subject="S", text_body="T", html_body="<p>H</p>"
        )


async def test_sender_wraps_network_failures(monkeypatch):
    async def failing_send(message, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(smtp_email.aiosmtplib, "send", failing_send)
    with pytest.raises(EmailDeliveryError):
        await SmtpEmailSender().send(
            to_email="user@example.com", subject="S", text_body="T", html_body="<p>H</p>"
        )


async def test_sender_requires_configuration(monkeypatch):
    monkeypatch.setattr(smtp_email.settings, "smtp_host", None)
    with pytest.raises(EmailNotConfiguredError):
        await SmtpEmailSender().send(
            to_email="user@example.com", subject="S", text_body="T", html_body="<p>H</p>"
        )
