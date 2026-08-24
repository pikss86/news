import asyncio
import logging
import signal

from app.collector.service import CollectorService
from app.config import get_settings
from app.logging import configure_logging
from app.storage.db import Database
from app.storage.repositories import UpdateRepository
from app.tdlib.auth import AuthorizationController
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


async def async_main() -> None:
    settings = get_settings()
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
    authorization = AuthorizationController(client, settings)
    service = CollectorService(client, database, UpdateRepository(), authorization)

    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    assert main_task is not None

    def request_shutdown() -> None:
        service.stop()
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_shutdown)

    try:
        await service.run()
    except asyncio.CancelledError:
        logger.info("shutdown requested")
    finally:
        logger.info("collector shutting down")
        client.close()
        await database.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
