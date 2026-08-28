import asyncio

import pytest

from app.collector.service import CollectorService, FatalCollectorError


class Database:
    async def transaction(self, operation):  # type: ignore[no-untyped-def]
        return await operation(None)


class Repository:
    def __init__(self) -> None:
        self.updates = []

    async def process(self, session, update):  # type: ignore[no-untyped-def]
        self.updates.append(update)
        return True


class Authorization:
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()

    async def handle(self, update):  # type: ignore[no-untyped-def]
        if update.get("authorization_state", {}).get("@type") == "authorizationStateReady":
            self.ready.set()


class Client:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def send(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)


async def test_tdlib_error_before_authorization_is_fatal_but_preserved() -> None:
    repository = Repository()
    persisted = 0

    async def on_persisted() -> None:
        nonlocal persisted
        persisted += 1

    service = CollectorService(
        object(),  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        Authorization(),  # type: ignore[arg-type]
        on_persisted_update=on_persisted,
    )
    update = {"@type": "error", "code": 400, "message": "database lock failed"}

    with pytest.raises(FatalCollectorError, match="code 400"):
        await service._process(update)

    assert repository.updates == [update]
    assert persisted == 1


async def test_started_callback_runs_only_after_tdlib_is_ready() -> None:
    started = 0

    async def on_started() -> None:
        nonlocal started
        started += 1

    service = CollectorService(
        object(),  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        Repository(),  # type: ignore[arg-type]
        Authorization(),  # type: ignore[arg-type]
        on_started=on_started,
    )
    await service._process({"@type": "updateOption"})
    assert started == 0

    ready = {
        "@type": "updateAuthorizationState",
        "authorization_state": {"@type": "authorizationStateReady"},
    }
    await service._process(ready)
    await service._process({"@type": "updateOption"})
    assert started == 1


async def test_chat_history_request_and_response_preserve_tdlib_batch() -> None:
    client = Client()
    authorization = Authorization()
    authorization.ready.set()
    repository = Repository()
    results: list[dict] = []
    service = CollectorService(
        client,  # type: ignore[arg-type]
        Database(),  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        authorization,  # type: ignore[arg-type]
        on_history_loaded=results.append,
    )

    service.request_chat_history(-100123, 200, 100, 17)
    assert client.requests == [
        {
            "@type": "getChatHistory",
            "chat_id": -100123,
            "from_message_id": 200,
            "offset": 0,
            "limit": 100,
            "only_local": False,
            "@extra": {
                "request": "chat_history",
                "control_request_id": 17,
                "chat_id": -100123,
                "from_message_id": 200,
            },
        }
    ]
    response = {
        "@type": "messages",
        "total_count": 900,
        "messages": [{"@type": "message", "id": 200}, {"@type": "message", "id": 100}],
        "@extra": client.requests[0]["@extra"],
    }
    await service._process(response)

    assert repository.updates[-1] == response
    assert results == [
        {
            "request_id": 17,
            "chat_id": -100123,
            "count": 2,
            "oldest_message_id": 100,
            "total_count": 900,
            "error": None,
        }
    ]
