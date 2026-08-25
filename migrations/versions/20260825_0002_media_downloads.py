"""Add persistent Telegram media file state.

Revision ID: 20260825_0002
Revises: 20260824_0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260825_0002"
down_revision = "20260824_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_files",
        sa.Column("file_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("can_be_downloaded", sa.Boolean(), nullable=False),
        sa.Column("is_downloading_active", sa.Boolean(), nullable=False),
        sa.Column("is_downloading_completed", sa.Boolean(), nullable=False),
        sa.Column("downloaded_size", sa.BigInteger(), nullable=False),
        sa.Column("remote", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_file", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("download_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_download_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("download_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("file_id"),
    )
    op.create_index(
        "ix_telegram_files_download_queue",
        "telegram_files",
        ["is_downloading_completed", "download_requested_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_telegram_files_download_queue", table_name="telegram_files")
    op.drop_table("telegram_files")
