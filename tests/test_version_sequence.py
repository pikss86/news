from datetime import UTC, datetime

from app.collector.normalizer import as_deleted, snapshot_from_message, with_content
from tests.fixtures import content_update, new_message_update


def test_created_edited_deleted_version_sequence_and_idempotency() -> None:
    versions: list[tuple[str, str]] = []

    def observe(change_type: str, state: object) -> None:
        fingerprint = state.fingerprint()  # type: ignore[attr-defined]
        if not versions or versions[-1][1] != fingerprint:
            versions.append((change_type, fingerprint))

    created = snapshot_from_message(new_message_update()["message"])
    observe("created", created)
    observe("created", snapshot_from_message(new_message_update()["message"]))

    edited = with_content(created, content_update()["new_content"])
    observe("edited", edited)
    observe("edited", with_content(edited, content_update()["new_content"]))

    deleted = as_deleted(edited, datetime(2026, 8, 24, 11, tzinfo=UTC))
    observe("deleted", deleted)
    observe("deleted", as_deleted(deleted, datetime(2026, 8, 24, 12, tzinfo=UTC)))

    assert [kind for kind, _ in versions] == ["created", "edited", "deleted"]
    assert len({fingerprint for _, fingerprint in versions}) == 3
