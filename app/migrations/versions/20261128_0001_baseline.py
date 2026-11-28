"""baseline: the schema the app already creates on startup

Revision ID: 0001
Revises:
Create Date: 2026-11-28

Safe to run on a brand new database AND on an existing one:
* new database      -> creates every table.
* existing database -> creates nothing that exists, only adds columns that are missing.

RULE FOR ALL LATER MIGRATIONS: because this baseline builds tables from the current
models, a fresh database may already contain a column that a later migration adds.
Write later migrations so they can be run twice, for example
    op.execute("ALTER TABLE t ADD COLUMN IF NOT EXISTS c VARCHAR(20)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_t_c ON t (c)")
"""
from importlib import import_module
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Columns that were added after their table first existed (create_all never adds columns).
_COLUMNS = (
    ("yt_upload_targets", "title VARCHAR(100)"),
    ("yt_upload_targets", "description TEXT"),
    ("yt_upload_targets", "tags JSON"),
    ("yt_upload_jobs", "default_audio_language VARCHAR(20)"),
    ("yt_upload_jobs", "contains_synthetic_media BOOLEAN"),
    ("yt_upload_jobs", "license VARCHAR(20)"),
    ("yt_upload_jobs", "embeddable BOOLEAN"),
    ("yt_upload_jobs", "public_stats_viewable BOOLEAN"),
)


def upgrade() -> None:
    from app.models.base import Base

    import_module("app.models")
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    for table, column in _COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column}")


def downgrade() -> None:
    raise RuntimeError(
        "The baseline cannot be undone: it would drop every table. "
        "Restore from a backup instead."
    )
