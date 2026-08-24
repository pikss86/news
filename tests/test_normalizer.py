from datetime import UTC, datetime

from app.collector.normalizer import (
    as_deleted,
    canonical_hash,
    snapshot_from_message,
    with_content,
)
from tests.fixtures import content_update, new_message_update


def test_new_message_normalization() -> None:
    update = new_message_update()
    snapshot = snapshot_from_message(update["message"])

    assert snapshot.chat_id == -100123
    assert snapshot.message_id == 200
    assert snapshot.text == "Text A"
    assert snapshot.content_type == "messageText"
    assert snapshot.published_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert snapshot.interaction_info["view_count"] == 12
    assert not snapshot.is_deleted


def test_repeated_message_has_stable_fingerprint() -> None:
    first = snapshot_from_message(new_message_update()["message"])
    repeated = snapshot_from_message(new_message_update()["message"])

    assert first.fingerprint() == repeated.fingerprint()
    assert canonical_hash(new_message_update()) == canonical_hash(new_message_update())


def test_edit_creates_a_distinct_immutable_state() -> None:
    original = snapshot_from_message(new_message_update()["message"])
    edited = with_content(original, content_update()["new_content"])

    assert original.text == "Text A"
    assert edited.text == "Text B"
    assert edited.fingerprint() != original.fingerprint()


def test_delete_preserves_content_in_tombstone_version() -> None:
    original = snapshot_from_message(new_message_update()["message"])
    deleted_at = datetime(2026, 8, 24, 11, tzinfo=UTC)
    deleted = as_deleted(original, deleted_at)

    assert deleted.is_deleted
    assert deleted.deleted_at == deleted_at
    assert deleted.text == "Text A"
    assert original.is_deleted is False


def test_repeated_delete_is_idempotent() -> None:
    original = snapshot_from_message(new_message_update()["message"])
    first = as_deleted(original, datetime(2026, 8, 24, 11, tzinfo=UTC))
    repeated = as_deleted(first, datetime(2026, 8, 24, 12, tzinfo=UTC))

    assert repeated == first
    assert repeated.fingerprint() == first.fingerprint()
