import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.collector.media import extract_file_objects
from app.collector.normalizer import (
    MessageSnapshot,
    as_deleted,
    canonical_hash,
    empty_snapshot,
    snapshot_from_message,
    with_content,
    with_edit_date,
    with_interaction_info,
)
from app.storage.models import (
    TdEvent,
    TelegramChat,
    TelegramFile,
    TelegramMessage,
    TelegramMessageVersion,
)

logger = logging.getLogger(__name__)


class UpdateRepository:
    async def process(self, session: AsyncSession, update: dict[str, Any]) -> bool:
        """Persist one TDLib object and update projections in the same transaction.

        Returns False if this exact TDLib object was committed previously.
        """
        received_at = datetime.now(UTC)
        event_type = str(update.get("@type", "unknown"))
        chat_id, message_id = self._event_identity(update)
        fingerprint = canonical_hash(update)
        result = await session.execute(
            insert(TdEvent)
            .values(
                event_type=event_type,
                received_at=received_at,
                chat_id=chat_id,
                message_id=message_id,
                event_fingerprint=fingerprint,
                payload=update,
            )
            .on_conflict_do_nothing(index_elements=[TdEvent.event_fingerprint])
            .returning(TdEvent.id)
        )
        event_id = result.scalar_one_or_none()
        if event_id is None:
            logger.debug("duplicate event skipped", extra={"event_type": event_type})
            return False

        try:
            async with session.begin_nested():
                await self._apply(session, update, event_id, received_at)
        except DBAPIError:
            raise
        except Exception:
            # The raw observation is still valuable even when a newly introduced
            # TDLib shape cannot yet be normalized.
            logger.exception(
                "processing error; raw event preserved",
                extra={"event_id": event_id, "event_type": event_type},
            )
        logger.info(
            "event persisted",
            extra={
                "event_id": event_id,
                "event_type": event_type,
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )
        return True

    async def _apply(
        self,
        session: AsyncSession,
        update: dict[str, Any],
        event_id: int,
        observed_at: datetime,
    ) -> None:
        event_type = update.get("@type")
        if event_type == "updateFile":
            await self._upsert_file(session, update["file"], observed_at, log_completion=True)
            return
        if event_type == "updateNewChat":
            await self._upsert_chat(session, update["chat"], update, observed_at)
            return
        if event_type == "updateChatTitle":
            await self._patch_chat(
                session, update["chat_id"], update, observed_at, title=update["title"]
            )
            return
        if event_type == "updateChatPhoto":
            await self._patch_chat(session, update["chat_id"], update, observed_at)
            return
        if event_type == "updateNewMessage":
            message = update["message"]
            await self._ensure_chat(session, message["chat_id"], observed_at)
            await self._store_snapshot(
                session,
                snapshot_from_message(message),
                event_id,
                observed_at,
                "created",
            )
            await self._upsert_files_from_content(session, message.get("content"), observed_at)
            logger.info(
                "new message received",
                extra={"chat_id": message["chat_id"], "message_id": message["id"]},
            )
            return
        if (
            event_type == "messages"
            and (update.get("@extra") or {}).get("request") == "chat_history"
        ):
            messages = update.get("messages") or []
            for message in messages:
                await self._ensure_chat(session, message["chat_id"], observed_at)
                await self._store_snapshot(
                    session,
                    snapshot_from_message(message),
                    event_id,
                    observed_at,
                    "history",
                )
                await self._upsert_files_from_content(session, message.get("content"), observed_at)
            logger.info(
                "chat history normalized",
                extra={
                    "chat_id": (update.get("@extra") or {}).get("chat_id"),
                    "message_count": len(messages),
                },
            )
            return
        if event_type == "updateMessageContent":
            current = await self._current_or_empty(
                session, update["chat_id"], update["message_id"], event_id, observed_at
            )
            await self._store_snapshot(
                session,
                with_content(current, update["new_content"]),
                event_id,
                observed_at,
                "edited",
            )
            await self._upsert_files_from_content(session, update.get("new_content"), observed_at)
            logger.info(
                "message edited",
                extra={"chat_id": update["chat_id"], "message_id": update["message_id"]},
            )
            return
        if event_type == "updateMessageEdited":
            current = await self._current_or_empty(
                session, update["chat_id"], update["message_id"], event_id, observed_at
            )
            await self._store_snapshot(
                session,
                with_edit_date(current, update.get("edit_date", 0)),
                event_id,
                observed_at,
                "edited",
            )
            return
        if event_type == "updateMessageInteractionInfo":
            current = await self._current_or_empty(
                session, update["chat_id"], update["message_id"], event_id, observed_at
            )
            await self._store_snapshot(
                session,
                with_interaction_info(current, update.get("interaction_info")),
                event_id,
                observed_at,
                "metadata",
            )
            return
        if event_type == "updateDeleteMessages":
            chat_id = update["chat_id"]
            await self._ensure_chat(session, chat_id, observed_at)
            for deleted_message_id in update.get("message_ids", []):
                current = await self._current_or_empty(
                    session, chat_id, deleted_message_id, event_id, observed_at
                )
                await self._store_snapshot(
                    session,
                    as_deleted(current, observed_at),
                    event_id,
                    observed_at,
                    "deleted",
                )
                logger.info(
                    "message deleted",
                    extra={"chat_id": chat_id, "message_id": deleted_message_id},
                )

    async def queue_file_downloads(
        self, session: AsyncSession, file_ids: list[int] | None = None
    ) -> list[int]:
        now = datetime.now(UTC)
        statement = (
            sql_update(TelegramFile)
            .where(
                TelegramFile.is_downloading_completed.is_(False),
                TelegramFile.can_be_downloaded.is_(True),
            )
            .values(
                download_requested_at=func.coalesce(TelegramFile.download_requested_at, now),
                last_download_requested_at=now,
            )
            .returning(TelegramFile.file_id)
        )
        if file_ids is not None:
            if not file_ids:
                return []
            statement = statement.where(TelegramFile.file_id.in_(set(file_ids)))
        result = await session.scalars(statement)
        return list(result.all())

    async def inventory_existing_message_files(self, session: AsyncSession) -> int:
        contents = await session.scalars(
            select(TelegramMessage.content).where(TelegramMessage.content.is_not(None))
        )
        discovered = 0
        observed_at = datetime.now(UTC)
        for content in contents:
            for file_object in extract_file_objects(content):
                await self._upsert_file(
                    session,
                    file_object,
                    observed_at,
                    update_existing=False,
                )
                discovered += 1
        if discovered:
            logger.info("existing media inventory scanned", extra={"file_count": discovered})
        return discovered

    async def upsert_file_object(
        self,
        session: AsyncSession,
        file_object: dict[str, Any],
        observed_at: datetime,
        *,
        file_id_override: int | None = None,
    ) -> None:
        normalized = dict(file_object)
        if file_id_override is not None:
            normalized["id"] = file_id_override
        await self._upsert_file(session, normalized, observed_at, log_completion=True)

    async def _upsert_files_from_content(
        self, session: AsyncSession, content: Any, observed_at: datetime
    ) -> None:
        for file_object in extract_file_objects(content):
            await self._upsert_file(session, file_object, observed_at)

    async def _upsert_file(
        self,
        session: AsyncSession,
        file_object: dict[str, Any],
        observed_at: datetime,
        *,
        update_existing: bool = True,
        log_completion: bool = False,
    ) -> None:
        local = file_object.get("local") or {}
        completed_at = observed_at if local.get("is_downloading_completed", False) else None
        statement = insert(TelegramFile).values(
            file_id=file_object["id"],
            size=file_object.get("size", 0),
            expected_size=file_object.get("expected_size", 0),
            local_path=local.get("path") or None,
            can_be_downloaded=local.get("can_be_downloaded", False),
            is_downloading_active=local.get("is_downloading_active", False),
            is_downloading_completed=local.get("is_downloading_completed", False),
            downloaded_size=local.get("downloaded_size", 0),
            remote=file_object.get("remote"),
            raw_file=file_object,
            first_collected_at=observed_at,
            last_updated_at=observed_at,
            download_completed_at=completed_at,
        )
        if update_existing:
            statement = statement.on_conflict_do_update(
                index_elements=[TelegramFile.file_id],
                set_={
                    "size": statement.excluded.size,
                    "expected_size": statement.excluded.expected_size,
                    "local_path": statement.excluded.local_path,
                    "can_be_downloaded": statement.excluded.can_be_downloaded,
                    "is_downloading_active": statement.excluded.is_downloading_active,
                    "is_downloading_completed": statement.excluded.is_downloading_completed,
                    "downloaded_size": statement.excluded.downloaded_size,
                    "remote": statement.excluded.remote,
                    "raw_file": statement.excluded.raw_file,
                    "last_updated_at": observed_at,
                    "download_completed_at": func.coalesce(
                        TelegramFile.download_completed_at,
                        statement.excluded.download_completed_at,
                    ),
                },
            )
        else:
            statement = statement.on_conflict_do_nothing(index_elements=[TelegramFile.file_id])
        await session.execute(statement)
        if completed_at is not None and log_completion:
            logger.info(
                "media download completed",
                extra={"file_id": file_object["id"], "local_path": local.get("path")},
            )

    async def _upsert_chat(
        self,
        session: AsyncSession,
        chat: dict[str, Any],
        update: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        chat_type = chat.get("type", {}).get("@type")
        statement = insert(TelegramChat).values(
            chat_id=chat["id"],
            chat_type=chat_type,
            title=chat.get("title"),
            first_collected_at=observed_at,
            last_collected_at=observed_at,
            raw_chat=chat,
            last_update=update,
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[TelegramChat.chat_id],
                set_={
                    "chat_type": statement.excluded.chat_type,
                    "title": statement.excluded.title,
                    "last_collected_at": observed_at,
                    "raw_chat": statement.excluded.raw_chat,
                    "last_update": statement.excluded.last_update,
                },
            )
        )

    async def _patch_chat(
        self,
        session: AsyncSession,
        chat_id: int,
        update: dict[str, Any],
        observed_at: datetime,
        **values: Any,
    ) -> None:
        await self._ensure_chat(session, chat_id, observed_at)
        chat = await session.get(TelegramChat, chat_id)
        assert chat is not None
        for key, value in values.items():
            setattr(chat, key, value)
        chat.last_collected_at = observed_at
        chat.last_update = update

    async def _ensure_chat(
        self, session: AsyncSession, chat_id: int, observed_at: datetime
    ) -> None:
        await session.execute(
            insert(TelegramChat)
            .values(
                chat_id=chat_id,
                first_collected_at=observed_at,
                last_collected_at=observed_at,
            )
            .on_conflict_do_nothing(index_elements=[TelegramChat.chat_id])
        )

    async def _current_or_empty(
        self,
        session: AsyncSession,
        chat_id: int,
        message_id: int,
        event_id: int,
        observed_at: datetime,
    ) -> MessageSnapshot:
        await self._ensure_chat(session, chat_id, observed_at)
        row = await self._locked_message(session, chat_id, message_id)
        if row is None:
            snapshot = empty_snapshot(chat_id, message_id)
            await self._insert_message(session, snapshot, event_id, observed_at)
            return snapshot
        return self._snapshot_from_row(row)

    async def _store_snapshot(
        self,
        session: AsyncSession,
        snapshot: MessageSnapshot,
        event_id: int,
        observed_at: datetime,
        change_type: str,
    ) -> bool:
        row = await self._locked_message(session, snapshot.chat_id, snapshot.message_id)
        if row is None:
            row = await self._insert_message(session, snapshot, event_id, observed_at)

        snapshot_hash = snapshot.fingerprint()
        existing_hash = self._snapshot_from_row(row).fingerprint() if row.current_version else None
        row.last_collected_at = observed_at
        if snapshot_hash == existing_hash:
            return False

        self._apply_snapshot(row, snapshot)
        row.current_version += 1
        row.current_event_id = event_id
        session.add(
            TelegramMessageVersion(
                chat_id=snapshot.chat_id,
                message_id=snapshot.message_id,
                version_number=row.current_version,
                observed_at=observed_at,
                change_type=change_type,
                snapshot_hash=snapshot_hash,
                snapshot=snapshot.document(),
                source_event_id=event_id,
            )
        )
        logger.info(
            "message normalized",
            extra={
                "chat_id": snapshot.chat_id,
                "message_id": snapshot.message_id,
                "version": row.current_version,
                "change_type": change_type,
            },
        )
        return True

    async def _insert_message(
        self,
        session: AsyncSession,
        snapshot: MessageSnapshot,
        event_id: int,
        observed_at: datetime,
    ) -> TelegramMessage:
        row = TelegramMessage(
            chat_id=snapshot.chat_id,
            message_id=snapshot.message_id,
            first_collected_at=observed_at,
            last_collected_at=observed_at,
            current_event_id=event_id,
            current_version=0,
        )
        session.add(row)
        await session.flush()
        return row

    async def _locked_message(
        self, session: AsyncSession, chat_id: int, message_id: int
    ) -> TelegramMessage | None:
        return await session.scalar(
            select(TelegramMessage)
            .where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.message_id == message_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _apply_snapshot(row: TelegramMessage, snapshot: MessageSnapshot) -> None:
        row.sender = snapshot.sender
        row.published_at = snapshot.published_at
        row.edited_at = snapshot.edited_at
        row.deleted_at = snapshot.deleted_at
        row.is_deleted = snapshot.is_deleted
        row.content_type = snapshot.content_type
        row.text = snapshot.text
        row.content = snapshot.content
        row.forward_info = snapshot.forward_info
        row.reply_to = snapshot.reply_to
        row.media = snapshot.media
        row.interaction_info = snapshot.interaction_info

    @staticmethod
    def _snapshot_from_row(row: TelegramMessage) -> MessageSnapshot:
        return MessageSnapshot(
            chat_id=row.chat_id,
            message_id=row.message_id,
            sender=row.sender,
            published_at=row.published_at,
            edited_at=row.edited_at,
            deleted_at=row.deleted_at,
            is_deleted=row.is_deleted,
            content_type=row.content_type,
            text=row.text,
            content=row.content,
            forward_info=row.forward_info,
            reply_to=row.reply_to,
            media=row.media,
            interaction_info=row.interaction_info,
        )

    @staticmethod
    def _event_identity(update: dict[str, Any]) -> tuple[int | None, int | None]:
        message = update.get("message")
        if isinstance(message, dict):
            return message.get("chat_id"), message.get("id")
        if update.get("@type") == "messages":
            return (update.get("@extra") or {}).get("chat_id"), None
        return update.get("chat_id"), update.get("message_id")
