import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-automated-tests-only-0123456789"
os.environ["APP_ENV"] = "test"
os.environ["DB_AUTO_CREATE_TABLES"] = "false"
os.environ["PASSWORD_RESET_OTP_RESEND_COOLDOWN_SECONDS"] = "60"
os.environ["PASSWORD_RESET_OTP_MAX_ATTEMPTS"] = "5"
os.environ["SMTP_HOST"] = "smtp.test.local"
os.environ["SMTP_PORT"] = "587"
os.environ["SMTP_USERNAME"] = "sender@test.local"
os.environ["SMTP_PASSWORD"] = "test password value"
os.environ["SMTP_FROM_EMAIL"] = "sender@test.local"

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.dependencies import get_email_service
from app.main import app
from app.models import Base
from app.service.email_service import OtpDelivery


class RecordingEmailService:
    def __init__(self) -> None:
        self.sent = []

    async def send_password_reset_otp(self, delivery: OtpDelivery) -> None:
        self.sent.append(delivery)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def email_service():
    return RecordingEmailService()


@pytest_asyncio.fixture
async def client(session_factory, email_service):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_email_service] = lambda: email_service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()
