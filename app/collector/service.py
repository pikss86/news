import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.collector.media import downloadable_file_ids
from app.storage.db import Database
from app.storage.repositories import UpdateRepository
from app.tdlib.auth import AuthorizationController
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


class FatalCollectorError(RuntimeError):
    """An error that prevents the collector from reaching an operational state."""


class CollectorService:
    def __init__(
        self,
        client: TdJsonClient,
        database: Database,
        repository: UpdateRepository,
        authorization: AuthorizationController,
        download_media: bool = False,
        download_priority: int = 16,
        on_persisted_update: Callable[[], Awaitable[None] | None] | None = None,
        on_started: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self.client = client
        self.database = database
        self.repository = repository
        self.authorization = authorization
        self.download_media = download_media
        self.download_priority = download_priority
        self.on_persisted_update = on_persisted_update
        self.on_started = on_started
        self._stopping = asyncio.Event()
        self._started_notified = False

    async def run(self) -> None:
        await self.database.wait_until_ready()
        logger.info("collector database connected; waiting for TDLib authorization")
        while not self._stopping.is_set() and not self.authorization.closed.is_set():
            try:
                update = await self.client.receive(wait_seconds=1.0)
                if update is None:
                    continue
                await self._process(update)
            except asyncio.CancelledError:
                raise
            except FatalCollectorError:
                logger.exception("fatal collector startup error")
                raise
            except Exception as error:
                if not self.authorization.ready.is_set():
                    logger.exception("collector failed before TDLib authorization")
                    raise FatalCollectorError("TDLib authorization failed") from error
                logger.exception("processing error")

    async def _process(self, update: dict[str, Any]) -> None:
        async def operation(session: Any) -> bool:
            return await self.repository.process(session, update)

        persisted = await self.database.transaction(operation)
        was_ready = self.authorization.ready.is_set()
        await self.authorization.handle(update)
        if persisted:
            await self._call(self.on_persisted_update)
        if update.get("@type") == "error" and not self.authorization.ready.is_set():
            code = update.get("code", "unknown")
            raise FatalCollectorError(f"TDLib authorization failed with code {code}")
        became_ready = not was_ready and self.authorization.ready.is_set()
        if self.authorization.ready.is_set() and not self._started_notified:
            self._started_notified = True
            await self._call(self.on_started)
            logger.info("collector started")
        self._log_media_download_error(update)
        if not self.download_media:
            return
        if became_ready:
            await self._queue_and_request_downloads()
            return
        if persisted:
            file_ids = downloadable_file_ids(update)
            if file_ids:
                await self._queue_and_request_downloads(file_ids)

    async def _queue_and_request_downloads(self, file_ids: list[int] | None = None) -> None:
        async def operation(session: Any) -> list[int]:
            if file_ids is None:
                await self.repository.inventory_existing_message_files(session)
            return await self.repository.queue_file_downloads(session, file_ids)

        queued_ids = await self.database.transaction(operation)
        for file_id in queued_ids:
            self.client.send(
                {
                    "@type": "downloadFile",
                    "file_id": file_id,
                    "priority": self.download_priority,
                    "offset": 0,
                    "limit": 0,
                    "synchronous": False,
                    "@extra": {"request": "media_download", "file_id": file_id},
                }
            )
            logger.info(
                "media download requested",
                extra={"file_id": file_id, "priority": self.download_priority},
            )

    @staticmethod
    def _log_media_download_error(update: dict[str, Any]) -> None:
        extra = update.get("@extra") or {}
        if update.get("@type") == "error" and extra.get("request") == "media_download":
            logger.error(
                "media download failed",
                extra={
                    "file_id": extra.get("file_id"),
                    "error_code": update.get("code"),
                    "error_message": update.get("message"),
                },
            )

    def stop(self) -> None:
        self._stopping.set()

    def apply_dynamic(
        self, *, download_media: bool, download_priority: int, tdlib_log_verbosity: int
    ) -> None:
        self.download_media = download_media
        self.download_priority = download_priority
        self.client.execute(
            {"@type": "setLogVerbosityLevel", "new_verbosity_level": tdlib_log_verbosity}
        )

    @staticmethod
    async def _call(callback: Callable[[], Awaitable[None] | None] | None) -> None:
        if callback is None:
            return
        result = callback()
        if inspect.isawaitable(result):
            await result
