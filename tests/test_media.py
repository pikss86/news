import asyncio

from app.collector.media import (
    downloadable_file_ids,
    extract_file_objects,
    message_file_objects,
)
from app.collector.service import CollectorService
from tests.fixtures import file_object, media_message_update, new_message_update


def test_extracts_all_unique_files_from_media_content() -> None:
    update = media_message_update()
    files = message_file_objects(update)

    assert {file["id"] for file in files} == {501, 502}
    assert downloadable_file_ids(update) == [501, 502]


def test_completed_file_is_not_requested_again() -> None:
    update = media_message_update(completed=True)

    assert downloadable_file_ids(update) == [502]


def test_text_message_has_no_downloads() -> None:
    assert message_file_objects(new_message_update()) == []
    assert downloadable_file_ids(new_message_update()) == []


def test_recursive_extraction_deduplicates_file_ids() -> None:
    file = file_object(700)

    assert extract_file_objects({"first": file, "second": [file]}) == [file]


async def test_download_request_uses_configured_priority() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request: dict) -> None:
            self.requests.append(request)

    class Database:
        async def transaction(self, operation):  # type: ignore[no-untyped-def]
            return await operation(None)

    class Repository:
        async def queue_file_downloads(self, session, file_ids):  # type: ignore[no-untyped-def]
            return file_ids

    client = Client()
    service = CollectorService(
        client,  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        download_media=True,
        download_priority=24,
    )

    await service._queue_and_request_downloads([501])

    assert client.requests == [
        {
            "@type": "downloadFile",
            "file_id": 501,
            "priority": 24,
            "offset": 0,
            "limit": 0,
            "synchronous": False,
            "@extra": {"request": "media_download", "file_id": 501},
        }
    ]


async def test_manual_download_resolves_persistent_remote_file_id() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request: dict) -> None:
            self.requests.append(request)

    class Database:
        async def transaction(self, operation):  # type: ignore[no-untyped-def]
            return await operation(None)

    class Repository:
        async def queue_file_downloads(self, session, file_ids):  # type: ignore[no-untyped-def]
            return file_ids

    client = Client()
    service = CollectorService(
        client,  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        download_media=False,
    )

    assert await service.request_file_download(501, "persistent-remote-id") is True
    assert client.requests == [
        {
            "@type": "getRemoteFile",
            "remote_file_id": "persistent-remote-id",
            "file_type": {"@type": "fileTypeUnknown"},
            "@extra": {"request": "resolve_media_download", "source_file_id": 501},
        }
    ]


async def test_disabled_flag_does_not_request_media() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request: dict) -> None:
            self.requests.append(request)

    class Database:
        async def transaction(self, operation):  # type: ignore[no-untyped-def]
            return await operation(None)

    class Repository:
        async def process(self, session, update):  # type: ignore[no-untyped-def]
            return True

    class Authorization:
        def __init__(self) -> None:
            self.ready = asyncio.Event()
            self.ready.set()
            self.closed = asyncio.Event()

        async def handle(self, update):  # type: ignore[no-untyped-def]
            return None

    client = Client()
    service = CollectorService(
        client,  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        Authorization(),  # type: ignore[arg-type]
        download_media=False,
    )

    await service._process(media_message_update())

    assert client.requests == []


async def test_manual_download_works_when_automatic_media_is_disabled() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request: dict) -> None:
            self.requests.append(request)

    class Database:
        async def transaction(self, operation):  # type: ignore[no-untyped-def]
            return await operation(None)

    class Repository:
        async def queue_file_downloads(self, session, file_ids):  # type: ignore[no-untyped-def]
            return file_ids

    client = Client()
    service = CollectorService(
        client,  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        download_media=False,
        download_priority=20,
    )

    assert await service.request_file_download(501) is True
    assert client.requests == [
        {
            "@type": "downloadFile",
            "file_id": 501,
            "priority": 20,
            "offset": 0,
            "limit": 0,
            "synchronous": False,
            "@extra": {
                "request": "media_download",
                "file_id": 501,
                "source_file_id": 501,
            },
        }
    ]
