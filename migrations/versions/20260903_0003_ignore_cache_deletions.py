"""Ignore cache-only deletions and repair affected message projections.

Revision ID: 20260903_0003
Revises: 20260825_0002
"""

import hashlib
import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260903_0003"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


def _fingerprint(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _is_history_response(event_type: str, payload: dict[str, Any]) -> bool:
    extra = payload.get("@extra") or {}
    return event_type == "messages" and extra.get("request") == "chat_history"


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    messages = sa.Table("telegram_messages", metadata, autoload_with=bind)
    versions = sa.Table("telegram_message_versions", metadata, autoload_with=bind)

    affected = list(
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT v.chat_id, v.message_id
                FROM telegram_message_versions AS v
                JOIN td_events AS e ON e.id = v.source_event_id
                WHERE v.change_type = 'deleted'
                  AND COALESCE((e.payload->>'from_cache')::boolean, false)
                ORDER BY v.chat_id, v.message_id
                """
            )
        ).mappings()
    )

    for identity in affected:
        chat_id = identity["chat_id"]
        message_id = identity["message_id"]
        observed_versions = list(
            bind.execute(
                sa.text(
                    """
                    SELECT v.id, v.observed_at, v.change_type, v.snapshot, v.source_event_id,
                           e.event_type AS source_event_type, e.payload AS source_payload
                    FROM telegram_message_versions AS v
                    JOIN td_events AS e ON e.id = v.source_event_id
                    WHERE v.chat_id = :chat_id AND v.message_id = :message_id
                    ORDER BY v.version_number, v.id
                    """
                ),
                {"chat_id": chat_id, "message_id": message_id},
            ).mappings()
        )

        rebuilt: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        is_deleted = False
        deleted_at: Any = None

        for row in observed_versions:
            event_type = row["source_event_type"]
            payload = row["source_payload"]
            if event_type == "updateDeleteMessages" and payload.get("from_cache", False):
                continue

            snapshot = dict(row["snapshot"])
            if event_type == "updateDeleteMessages":
                is_deleted = True
                deleted_at = snapshot.get("deleted_at")
            elif event_type == "updateNewMessage" or _is_history_response(event_type, payload):
                is_deleted = False
                deleted_at = None

            snapshot["is_deleted"] = is_deleted
            snapshot["deleted_at"] = deleted_at
            snapshot_hash = _fingerprint(snapshot)
            if snapshot_hash in seen_hashes:
                continue
            seen_hashes.add(snapshot_hash)
            rebuilt.append(
                {
                    "id": row["id"],
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "version_number": len(rebuilt) + 1,
                    "observed_at": row["observed_at"],
                    "change_type": row["change_type"],
                    "snapshot_hash": snapshot_hash,
                    "snapshot": snapshot,
                    "source_event_id": row["source_event_id"],
                }
            )

        bind.execute(
            sa.delete(versions).where(
                versions.c.chat_id == chat_id,
                versions.c.message_id == message_id,
            )
        )
        if not rebuilt:
            bind.execute(
                sa.delete(messages).where(
                    messages.c.chat_id == chat_id,
                    messages.c.message_id == message_id,
                )
            )
            continue

        bind.execute(sa.insert(versions), rebuilt)
        current = rebuilt[-1]
        snapshot = current["snapshot"]
        bind.execute(
            sa.update(messages)
            .where(
                messages.c.chat_id == chat_id,
                messages.c.message_id == message_id,
            )
            .values(
                sender=snapshot.get("sender"),
                published_at=_datetime(snapshot.get("published_at")),
                edited_at=_datetime(snapshot.get("edited_at")),
                deleted_at=_datetime(snapshot.get("deleted_at")),
                is_deleted=snapshot["is_deleted"],
                content_type=snapshot.get("content_type"),
                text=snapshot.get("text"),
                content=snapshot.get("content"),
                forward_info=snapshot.get("forward_info"),
                reply_to=snapshot.get("reply_to"),
                media=snapshot.get("media"),
                interaction_info=snapshot.get("interaction_info"),
                current_version=current["version_number"],
                current_event_id=current["source_event_id"],
            )
        )


def downgrade() -> None:
    # The discarded tombstones were derived from raw events, which remain intact. Reintroducing
    # known-invalid projection state on downgrade would be harmful, so this data repair is retained.
    pass
