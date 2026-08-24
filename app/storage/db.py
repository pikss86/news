import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)
T = TypeVar("T")


class Database:
    def __init__(self, url: str, retry_initial: float = 1.0, retry_max: float = 30.0) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.retry_initial = retry_initial
        self.retry_max = retry_max

    async def wait_until_ready(self) -> None:
        from sqlalchemy import text

        delay = self.retry_initial
        while True:
            try:
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                logger.info("database connected")
                return
            except (OSError, DBAPIError) as error:
                if isinstance(error, DBAPIError) and not (
                    isinstance(error, OperationalError) or error.connection_invalidated
                ):
                    raise
                logger.warning(
                    "database unavailable; retrying",
                    extra={"delay_seconds": delay, "error": str(error)},
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max)

    async def transaction(self, operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
        delay = self.retry_initial
        while True:
            try:
                async with self.sessions() as session, session.begin():
                    return await operation(session)
            except (OSError, OperationalError, DBAPIError) as error:
                if isinstance(error, DBAPIError) and not (
                    isinstance(error, OperationalError) or error.connection_invalidated
                ):
                    raise
                logger.warning(
                    "database transaction failed; reconnecting",
                    extra={"delay_seconds": delay, "error": str(error)},
                )
                await self.engine.dispose()
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max)

    async def close(self) -> None:
        await self.engine.dispose()
