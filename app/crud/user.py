import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


async def get_by_email(session: AsyncSession, email: str) -> Optional[User]:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> Optional[User]:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    phone: Optional[str],
    password_hash: str,
) -> User:
    user = User(name=name, email=email, phone=phone, password_hash=password_hash)
    session.add(user)
    await session.flush()
    return user
