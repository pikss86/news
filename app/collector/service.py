import asyncio
import logging
from typing import Any

from app.storage.db import Database
from app.storage.repositories import UpdateRepository
from app.tdlib.auth import AuthorizationController
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


class CollectorService:
    def __init__(
        self,
        client: TdJsonClient,
        database: Database,
        repository: UpdateRepository,
        authorization: AuthorizationController,
    ) -> None:
        self.client = client
        self.database = database
        self.repository = repository
        self.authorization = authorization
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.database.wait_until_ready()
        logger.info("collector started")
        while not self._stopping.is_set() and not self.authorization.closed.is_set():
            try:
                update = await self.client.receive(wait_seconds=1.0)
                if update is None:
                    continue
                await self._process(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("processing error")

    async def _process(self, update: dict[str, Any]) -> None:
        async def operation(session: Any) -> bool:
            return await self.repository.process(session, update)

        await self.database.transaction(operation)
        await self.authorization.handle(update)

    def stop(self) -> None:
        self._stopping.set()
