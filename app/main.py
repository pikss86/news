import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.collector.service import CollectorService
from app.config import Settings, get_settings
from app.logging import configure_logging
from app.storage.db import Database
from app.storage.repositories import UpdateRepository
from app.tdlib.auth import AuthorizationController, AuthorizationInput
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


@dataclass
class CollectorRuntime:
    client: TdJsonClient
    database: Database
    service: CollectorService

    async def run(self) -> None:
        try:
            await self.service.run()
        finally:
            self.client.close()
            await self.database.close()

    def stop(self) -> None:
        self.service.stop()


def create_collector(
    settings: Settings,
    *,
    authorization_input: AuthorizationInput | None = None,
    on_persisted_update: Callable[[], Awaitable[None] | None] | None = None,
    on_started: Callable[[], Awaitable[None] | None] | None = None,
    on_history_loaded: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> CollectorRuntime:
    configure_logging(settings.log_level)
    client = TdJsonClient(settings.tdlib_library_path)
    client.execute(
        {
            "@type": "setLogVerbosityLevel",
            "new_verbosity_level": settings.tdlib_log_verbosity,
        }
    )
    database = Database(
        settings.database_url,
        settings.database_retry_initial_seconds,
        settings.database_retry_max_seconds,
    )
    authorization = AuthorizationController(client, settings, authorization_input)
    service = CollectorService(
        client,
        database,
        UpdateRepository(),
        authorization,
        download_media=settings.telegram_download_media,
        download_priority=settings.telegram_media_download_priority,
        on_persisted_update=on_persisted_update,
        on_started=on_started,
        on_history_loaded=on_history_loaded,
    )
    return CollectorRuntime(client, database, service)


async def async_main() -> None:
    settings = get_settings()
    runtime = create_collector(settings)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None

    def request_shutdown() -> None:
        runtime.stop()
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_shutdown)

    try:
        await runtime.run()
    except asyncio.CancelledError:
        logger.info("shutdown requested")
    finally:
        logger.info("collector shutting down")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
