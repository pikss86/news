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
