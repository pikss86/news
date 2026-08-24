"""Initial Telegram ingestion schema.

Revision ID: 20260824_0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260824_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "td_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("event_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_fingerprint"),
    )
    op.create_index("ix_td_events_message", "td_events", ["chat_id", "message_id"], unique=False)
    op.create_index(
        "ix_td_events_type_received", "td_events", ["event_type", "received_at"], unique=False
    )

    op.create_table(
        "telegram_chats",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_type", sa.String(length=64), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_chat", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_update", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("chat_id"),
    )

    op.create_table(
        "telegram_messages",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("sender", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("forward_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply_to", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("media", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("interaction_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("current_event_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["telegram_chats.chat_id"]),
        sa.ForeignKeyConstraint(["current_event_id"], ["td_events.id"]),
        sa.PrimaryKeyConstraint("chat_id", "message_id"),
    )
    op.create_index(
        "ix_telegram_messages_deleted",
        "telegram_messages",
        ["is_deleted", "deleted_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_messages_published",
        "telegram_messages",
        ["published_at"],
        unique=False,
    )

    op.create_table(
        "telegram_message_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_event_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["td_events.id"]),
        sa.ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["telegram_messages.chat_id", "telegram_messages.message_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "message_id", "snapshot_hash"),
        sa.UniqueConstraint("chat_id", "message_id", "version_number"),
    )
    op.create_index(
        "ix_message_versions_observed",
        "telegram_message_versions",
        ["chat_id", "message_id", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_versions_observed", table_name="telegram_message_versions")
    op.drop_table("telegram_message_versions")
    op.drop_index("ix_telegram_messages_published", table_name="telegram_messages")
    op.drop_index("ix_telegram_messages_deleted", table_name="telegram_messages")
    op.drop_table("telegram_messages")
    op.drop_table("telegram_chats")
    op.drop_index("ix_td_events_type_received", table_name="td_events")
    op.drop_index("ix_td_events_message", table_name="td_events")
    op.drop_table("td_events")
