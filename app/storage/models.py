from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TdEvent(Base):
    __tablename__ = "td_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_td_events_type_received", "event_type", "received_at"),
        Index("ix_td_events_message", "chat_id", "message_id"),
    )


class TelegramChat(Base):
    __tablename__ = "telegram_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_type: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    first_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_chat: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_update: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    sender: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str | None] = mapped_column(Text)
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    forward_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reply_to: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    media: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    interaction_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["chat_id"], ["telegram_chats.chat_id"]),
        ForeignKeyConstraint(["current_event_id"], ["td_events.id"]),
        Index("ix_telegram_messages_published", "published_at"),
        Index("ix_telegram_messages_deleted", "is_deleted", "deleted_at"),
    )


class TelegramMessageVersion(Base):
    __tablename__ = "telegram_message_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["chat_id", "message_id"],
            ["telegram_messages.chat_id", "telegram_messages.message_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["source_event_id"], ["td_events.id"]),
        UniqueConstraint("chat_id", "message_id", "version_number"),
        UniqueConstraint("chat_id", "message_id", "snapshot_hash"),
        Index("ix_message_versions_observed", "chat_id", "message_id", "observed_at"),
    )


class TelegramFile(Base):
    __tablename__ = "telegram_files"

    file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expected_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    local_path: Mapped[str | None] = mapped_column(Text)
    can_be_downloaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_downloading_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_downloading_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    downloaded_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    remote: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    raw_file: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    first_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    download_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_download_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_telegram_files_download_queue",
            "is_downloading_completed",
            "download_requested_at",
        ),
    )
