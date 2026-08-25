def new_message_update(text: str = "Text A") -> dict:
    return {
        "@type": "updateNewMessage",
        "message": {
            "@type": "message",
            "id": 200,
            "chat_id": -100123,
            "sender_id": {"@type": "messageSenderChat", "chat_id": -100123},
            "date": 1_700_000_000,
            "edit_date": 0,
            "content": {
                "@type": "messageText",
                "text": {"@type": "formattedText", "text": text, "entities": []},
            },
            "interaction_info": {"@type": "messageInteractionInfo", "view_count": 12},
        },
    }


def content_update(text: str = "Text B") -> dict:
    return {
        "@type": "updateMessageContent",
        "chat_id": -100123,
        "message_id": 200,
        "new_content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
        },
    }


def delete_update() -> dict:
    return {
        "@type": "updateDeleteMessages",
        "chat_id": -100123,
        "message_ids": [200],
        "is_permanent": True,
        "from_cache": False,
    }


def media_message_update(completed: bool = False, message_id: int = 201) -> dict:
    update = new_message_update()
    update["message"]["id"] = message_id
    update["message"]["content"] = {
        "@type": "messagePhoto",
        "photo": {
            "@type": "photo",
            "sizes": [
                {
                    "@type": "photoSize",
                    "type": "m",
                    "photo": file_object(501, completed=completed),
                },
                {
                    "@type": "photoSize",
                    "type": "x",
                    "photo": file_object(502),
                },
            ],
        },
        "caption": {"@type": "formattedText", "text": "Photo caption", "entities": []},
    }
    return update


def file_update(file_id: int, completed: bool = True) -> dict:
    return {"@type": "updateFile", "file": file_object(file_id, completed=completed)}


def file_object(file_id: int, completed: bool = False) -> dict:
    return {
        "@type": "file",
        "id": file_id,
        "size": 1024,
        "expected_size": 1024,
        "local": {
            "@type": "localFile",
            "path": f"/var/lib/tdlib/files/{file_id}.jpg" if completed else "",
            "can_be_downloaded": not completed,
            "can_be_deleted": completed,
            "is_downloading_active": False,
            "is_downloading_completed": completed,
            "download_offset": 0,
            "downloaded_prefix_size": 1024 if completed else 0,
            "downloaded_size": 1024 if completed else 0,
        },
        "remote": {
            "@type": "remoteFile",
            "id": f"remote-{file_id}",
            "unique_id": f"unique-{file_id}",
            "is_uploading_active": False,
            "is_uploading_completed": True,
            "uploaded_size": 1024,
        },
    }
