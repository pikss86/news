import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
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
        self._download_aliases: dict[int, int] = {}

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
        await self._handle_manual_download_update(update)
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

    async def request_file_download(self, file_id: int, remote_file_id: str | None = None) -> bool:
        """Queue one operator-selected file regardless of the automatic media flag."""

        async def operation(session: Any) -> list[int]:
            return await self.repository.queue_file_downloads(session, [file_id])

        queued_ids = await self.database.transaction(operation)
        if file_id not in queued_ids:
            return False
        if remote_file_id:
            self.client.send(
                {
                    "@type": "getRemoteFile",
                    "remote_file_id": remote_file_id,
                    "file_type": {"@type": "fileTypeUnknown"},
                    "@extra": {
                        "request": "resolve_media_download",
                        "source_file_id": file_id,
                    },
                }
            )
            logger.info("manual media file resolving", extra={"file_id": file_id})
            return True
        self._send_download_request(file_id, file_id)
        return True

    def _send_download_request(self, file_id: int, source_file_id: int) -> None:
        self.client.send(
            {
                "@type": "downloadFile",
                "file_id": file_id,
                "priority": self.download_priority,
                "offset": 0,
                "limit": 0,
                "synchronous": False,
                "@extra": {
                    "request": "media_download",
                    "file_id": file_id,
                    "source_file_id": source_file_id,
                },
            }
        )
        logger.info(
            "manual media download requested",
            extra={
                "file_id": file_id,
                "source_file_id": source_file_id,
                "priority": self.download_priority,
            },
        )

    async def _handle_manual_download_update(self, update: dict[str, Any]) -> None:
        extra = update.get("@extra") or {}
        update_type = update.get("@type")
        if update_type == "file" and extra.get("request") == "resolve_media_download":
            source_file_id = int(extra["source_file_id"])
            resolved_file_id = int(update["id"])
            self._download_aliases[resolved_file_id] = source_file_id
            await self._store_resolved_file(update, source_file_id)
            self._send_download_request(resolved_file_id, source_file_id)
            return
        if update_type == "file" and extra.get("request") == "media_download":
            source_file_id = int(extra.get("source_file_id", update["id"]))
            await self._store_resolved_file(update, source_file_id)
            if (update.get("local") or {}).get("is_downloading_completed"):
                self._download_aliases.pop(int(update["id"]), None)
            return
        if update_type == "updateFile":
            file_object = update.get("file") or {}
            resolved_file_id = file_object.get("id")
            source_file_id = self._download_aliases.get(resolved_file_id)
            if source_file_id is not None:
                await self._store_resolved_file(file_object, source_file_id)
                if (file_object.get("local") or {}).get("is_downloading_completed"):
                    self._download_aliases.pop(resolved_file_id, None)

    async def _store_resolved_file(self, file_object: dict[str, Any], source_file_id: int) -> None:
        observed_at = datetime.now(UTC)

        async def operation(session: Any) -> None:
            await self.repository.upsert_file_object(session, file_object, observed_at)
            if file_object.get("id") != source_file_id:
                await self.repository.upsert_file_object(
                    session,
                    file_object,
                    observed_at,
                    file_id_override=source_file_id,
                )

        await self.database.transaction(operation)

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
