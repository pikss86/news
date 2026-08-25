from typing import Any


def extract_file_objects(value: Any) -> list[dict[str, Any]]:
    """Find unique TDLib file objects recursively in an arbitrary JSON value."""
    found: dict[int, dict[str, Any]] = {}
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get("@type") == "file":
                file_id = current.get("id")
                if isinstance(file_id, int) and file_id > 0:
                    found[file_id] = current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return sorted(found.values(), key=lambda item: item["id"])


def message_file_objects(update: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = update.get("@type")
    if event_type == "updateNewMessage":
        return extract_file_objects(update.get("message", {}).get("content"))
    if event_type == "updateMessageContent":
        return extract_file_objects(update.get("new_content"))
    return []


def downloadable_file_ids(update: dict[str, Any]) -> list[int]:
    result: list[int] = []
    for file_object in message_file_objects(update):
        local = file_object.get("local") or {}
        if local.get("is_downloading_completed", False):
            continue
        if local.get("can_be_downloaded", True) is False:
            continue
        result.append(file_object["id"])
    return result
