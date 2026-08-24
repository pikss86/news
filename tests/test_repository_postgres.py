import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.storage.models import Base, TdEvent, TelegramMessage, TelegramMessageVersion
from app.storage.repositories import UpdateRepository
from tests.fixtures import content_update, delete_update, new_message_update

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run PostgreSQL repository tests",
)


@pytest.mark.asyncio
async def test_repository_versioning_and_idempotency() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    schema = f"test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = create_async_engine(url, execution_options={"schema_translate_map": {None: schema}})

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        sessions = async_sessionmaker(engine, expire_on_commit=False)
        repository = UpdateRepository()
        async with sessions() as session, session.begin():
            assert await repository.process(session, new_message_update())
            assert not await repository.process(session, new_message_update())
            assert await repository.process(session, content_update())
            assert not await repository.process(session, content_update())
            assert await repository.process(session, delete_update())
            assert not await repository.process(session, delete_update())
            assert await repository.process(
                session, {"@type": "updateFutureThing", "future_field": 42}
            )
            assert await repository.process(
                session,
                {"@type": "updateNewMessage", "message": {"chat_id": -100123}},
            )

        async with sessions() as session:
            message = await session.get(TelegramMessage, (-100123, 200))
            version_count = await session.scalar(
                select(func.count()).select_from(TelegramMessageVersion)
            )
            raw_event_count = await session.scalar(select(func.count()).select_from(TdEvent))
            versions = list(
                (
                    await session.scalars(
                        select(TelegramMessageVersion).order_by(
                            TelegramMessageVersion.version_number
                        )
                    )
                ).all()
            )

        assert message is not None
        assert message.current_version == 3
        assert message.is_deleted
        assert message.text == "Text B"
        assert version_count == 3
        assert raw_event_count == 5
        assert [version.change_type for version in versions] == ["created", "edited", "deleted"]
        assert versions[0].snapshot["text"] == "Text A"
        assert versions[1].snapshot["text"] == "Text B"
        assert versions[2].snapshot["is_deleted"] is True
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
