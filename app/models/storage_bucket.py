# app/models/storage_bucket.py
import enum
import uuid

from sqlalchemy import BigInteger, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class StorageFileType(str, enum.Enum):
    IMAGE = "image"


class StorageFile(TimestampMixin, Base):
    __tablename__ = "storage_files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    file_type: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)

    tags: Mapped[list["StorageFileTag"]] = relationship(
        "StorageFileTag",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="StorageFileTag.tag",
    )


class StorageFileTag(Base):
    __tablename__ = "storage_file_tags"

    file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("storage_files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag: Mapped[str] = mapped_column(String(100), primary_key=True)