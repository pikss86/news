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
