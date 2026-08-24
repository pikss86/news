import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class MessageSnapshot(BaseModel):
    """An observed Telegram message state, independent of its observation time."""

    model_config = ConfigDict(frozen=True)

    chat_id: int
    message_id: int
    sender: dict[str, Any] | None = None
    published_at: datetime | None = None
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    is_deleted: bool = False
    content_type: str | None = None
    text: str | None = None
    content: dict[str, Any] | None = None
    forward_info: dict[str, Any] | None = None
    reply_to: dict[str, Any] | None = None
    media: dict[str, Any] | None = None
    interaction_info: dict[str, Any] | None = None

    def document(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        return canonical_hash(self.document())


def canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def unix_time(value: Any) -> datetime | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def content_fields(
    content: dict[str, Any] | None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if not content:
        return None, None, None
    content_type = content.get("@type")
    formatted = content.get("text") if content_type == "messageText" else content.get("caption")
    text = formatted.get("text") if isinstance(formatted, dict) else None
    media = None if content_type == "messageText" else content
    return content_type, text, media


def snapshot_from_message(message: dict[str, Any]) -> MessageSnapshot:
    content = message.get("content")
    content_type, text, media = content_fields(content)
    return MessageSnapshot(
        chat_id=message["chat_id"],
        message_id=message["id"],
        sender=message.get("sender_id"),
        published_at=unix_time(message.get("date")),
        edited_at=unix_time(message.get("edit_date")),
        content_type=content_type,
        text=text,
        content=content,
        forward_info=message.get("forward_info"),
        reply_to=message.get("reply_to"),
        media=media,
        interaction_info=message.get("interaction_info"),
    )


def with_content(current: MessageSnapshot, content: dict[str, Any]) -> MessageSnapshot:
    content_type, text, media = content_fields(content)
    return current.model_copy(
        update={"content": content, "content_type": content_type, "text": text, "media": media}
    )


def with_edit_date(current: MessageSnapshot, edit_date: int) -> MessageSnapshot:
    return current.model_copy(update={"edited_at": unix_time(edit_date)})


def with_interaction_info(
    current: MessageSnapshot, interaction_info: dict[str, Any] | None
) -> MessageSnapshot:
    return current.model_copy(update={"interaction_info": interaction_info})


def as_deleted(current: MessageSnapshot, observed_at: datetime) -> MessageSnapshot:
    if current.is_deleted:
        return current
    return current.model_copy(update={"is_deleted": True, "deleted_at": observed_at})


def empty_snapshot(chat_id: int, message_id: int) -> MessageSnapshot:
    return MessageSnapshot(chat_id=chat_id, message_id=message_id)
