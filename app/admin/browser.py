from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DataBrowser:
    """Small read-only query layer for the administration data browser."""

    def __init__(self, database_host_override: str | None = None) -> None:
        self.database_host_override = database_host_override
        self._engine: AsyncEngine | None = None
        self._engine_url: URL | None = None
        self._lock = asyncio.Lock()

    def _url(self, database_url: str) -> URL:
        url = make_url(database_url)
        if self.database_host_override:
            url = url.set(host=self.database_host_override)
        return url

    async def _get_engine(self, database_url: str) -> AsyncEngine:
        url = self._url(database_url)
        async with self._lock:
            if self._engine is not None and self._engine_url != url:
                await self._engine.dispose()
                self._engine = None
            if self._engine is None:
                self._engine = create_async_engine(url, pool_pre_ping=True, pool_size=3)
                self._engine_url = url
            return self._engine

    async def _rows(
        self, database_url: str, query: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        engine = await self._get_engine(database_url)
        async with engine.connect() as connection:
            result = await connection.execute(text(query), parameters or {})
            return [dict(row) for row in result.mappings().all()]

    async def _row(
        self, database_url: str, query: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        rows = await self._rows(database_url, query, parameters)
        return rows[0] if rows else None

    async def overview(self, database_url: str) -> dict[str, Any]:
        stats = await self._row(
            database_url,
            """
            SELECT
              (SELECT count(*) FROM td_events) AS events,
              (SELECT count(*) FROM telegram_chats) AS chats,
              (SELECT count(*) FROM telegram_messages) AS messages,
              (SELECT count(*) FROM telegram_message_versions) AS versions,
              (SELECT count(*) FROM telegram_messages WHERE is_deleted) AS deleted,
              (SELECT count(*) FROM telegram_files) AS files,
              (SELECT count(*) FROM telegram_files WHERE is_downloading_completed) AS files_ready,
              (SELECT max(received_at) FROM td_events) AS last_event_at
            """,
        )
        recent_messages = await self._rows(
            database_url,
            """
            SELECT m.chat_id, m.message_id, m.published_at, m.last_collected_at,
                   m.content_type, m.text, m.is_deleted, m.current_version,
                   c.title AS chat_title
              FROM telegram_messages m
              LEFT JOIN telegram_chats c ON c.chat_id = m.chat_id
             ORDER BY COALESCE(m.published_at, m.last_collected_at) DESC
             LIMIT 10
            """,
        )
        recent_events = await self._rows(
            database_url,
            """
            SELECT id, event_type, received_at, chat_id, message_id
              FROM td_events
             ORDER BY id DESC
             LIMIT 12
            """,
        )
        return {
            "stats": stats or {},
            "recent_messages": recent_messages,
            "recent_events": recent_events,
        }

    async def chats(
        self, database_url: str, *, page: int, per_page: int, query: str
    ) -> dict[str, Any]:
        where = "WHERE (c.title ILIKE :pattern OR c.chat_id::text ILIKE :pattern)" if query else ""
        parameters = {
            "pattern": f"%{query}%",
            "limit": per_page,
            "offset": (page - 1) * per_page,
        }
        total = await self._row(
            database_url,
            f"SELECT count(*) AS total FROM telegram_chats c {where}",
            parameters,
        )
        rows = await self._rows(
            database_url,
            f"""
            SELECT c.chat_id, c.title, c.chat_type, c.first_collected_at, c.last_collected_at,
                   count(m.message_id) AS message_count,
                   max(m.published_at) AS last_message_at
              FROM telegram_chats c
              LEFT JOIN telegram_messages m ON m.chat_id = c.chat_id
              {where}
             GROUP BY c.chat_id
             ORDER BY c.last_collected_at DESC
             LIMIT :limit OFFSET :offset
            """,
            parameters,
        )
        return {"rows": rows, "total": int((total or {}).get("total", 0))}

    async def chat(self, database_url: str, chat_id: int) -> dict[str, Any] | None:
        return await self._row(
            database_url,
            """
            SELECT c.*,
                   (SELECT count(*) FROM telegram_messages m WHERE m.chat_id = c.chat_id)
                     AS message_count
              FROM telegram_chats c
             WHERE c.chat_id = :chat_id
            """,
            {"chat_id": chat_id},
        )

    async def messages(
        self,
        database_url: str,
        *,
        page: int,
        per_page: int,
        chat_id: int | None,
        query: str,
        deleted: bool | None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {
            "limit": per_page,
            "offset": (page - 1) * per_page,
        }
        if chat_id is not None:
            clauses.append("m.chat_id = :chat_id")
            parameters["chat_id"] = chat_id
        if query:
            clauses.append("COALESCE(m.text, '') ILIKE :pattern")
            parameters["pattern"] = f"%{query}%"
        if deleted is not None:
            clauses.append("m.is_deleted = :deleted")
            parameters["deleted"] = deleted
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = await self._row(
            database_url,
            f"SELECT count(*) AS total FROM telegram_messages m {where}",
            parameters,
        )
        rows = await self._rows(
            database_url,
            f"""
            SELECT m.chat_id, m.message_id, m.published_at, m.last_collected_at,
                   m.edited_at, m.deleted_at, m.is_deleted, m.content_type, m.text,
                   m.current_version, c.title AS chat_title
              FROM telegram_messages m
              LEFT JOIN telegram_chats c ON c.chat_id = m.chat_id
              {where}
             ORDER BY COALESCE(m.published_at, m.last_collected_at) DESC
             LIMIT :limit OFFSET :offset
            """,
            parameters,
        )
        return {"rows": rows, "total": int((total or {}).get("total", 0))}

    async def message(
        self, database_url: str, chat_id: int, message_id: int
    ) -> dict[str, Any] | None:
        message = await self._row(
            database_url,
            """
            SELECT m.*, c.title AS chat_title
              FROM telegram_messages m
              LEFT JOIN telegram_chats c ON c.chat_id = m.chat_id
             WHERE m.chat_id = :chat_id AND m.message_id = :message_id
            """,
            {"chat_id": chat_id, "message_id": message_id},
        )
        if message is None:
            return None
        versions = await self._rows(
            database_url,
            """
            SELECT id, version_number, observed_at, change_type, snapshot_hash,
                   snapshot, source_event_id
              FROM telegram_message_versions
             WHERE chat_id = :chat_id AND message_id = :message_id
             ORDER BY version_number DESC
            """,
            {"chat_id": chat_id, "message_id": message_id},
        )
        return {"message": message, "versions": versions}

    async def events(
        self,
        database_url: str,
        *,
        page: int,
        per_page: int,
        event_type: str,
        chat_id: int | None,
        message_id: int | None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {
            "limit": per_page,
            "offset": (page - 1) * per_page,
        }
        if event_type:
            clauses.append("event_type = :event_type")
            parameters["event_type"] = event_type
        if chat_id is not None:
            clauses.append("chat_id = :chat_id")
            parameters["chat_id"] = chat_id
        if message_id is not None:
            clauses.append("message_id = :message_id")
            parameters["message_id"] = message_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = await self._row(
            database_url,
            f"SELECT count(*) AS total FROM td_events {where}",
            parameters,
        )
        rows = await self._rows(
            database_url,
            f"""
            SELECT id, event_type, received_at, chat_id, message_id
              FROM td_events
              {where}
             ORDER BY id DESC
             LIMIT :limit OFFSET :offset
            """,
            parameters,
        )
        event_types = await self._rows(
            database_url,
            """
            SELECT event_type, count(*) AS count
              FROM td_events
             GROUP BY event_type
             ORDER BY event_type
            """,
        )
        return {
            "rows": rows,
            "event_types": event_types,
            "total": int((total or {}).get("total", 0)),
        }

    async def event(self, database_url: str, event_id: int) -> dict[str, Any] | None:
        return await self._row(
            database_url,
            """
            SELECT id, event_type, received_at, chat_id, message_id,
                   event_fingerprint, payload
              FROM td_events
             WHERE id = :event_id
            """,
            {"event_id": event_id},
        )

    async def files(
        self, database_url: str, *, page: int, per_page: int, state: str
    ) -> dict[str, Any]:
        clauses = {
            "ready": "WHERE is_downloading_completed",
            "pending": "WHERE NOT is_downloading_completed AND download_requested_at IS NOT NULL",
            "available": "WHERE NOT is_downloading_completed AND can_be_downloaded",
        }
        where = clauses.get(state, "")
        parameters = {"limit": per_page, "offset": (page - 1) * per_page}
        total = await self._row(
            database_url,
            f"SELECT count(*) AS total FROM telegram_files {where}",
            parameters,
        )
        rows = await self._rows(
            database_url,
            f"""
            SELECT file_id, size, expected_size, local_path, can_be_downloaded,
                   is_downloading_active, is_downloading_completed, downloaded_size,
                   first_collected_at, last_updated_at, download_requested_at,
                   download_completed_at
              FROM telegram_files
              {where}
             ORDER BY last_updated_at DESC
             LIMIT :limit OFFSET :offset
            """,
            parameters,
        )
        return {"rows": rows, "total": int((total or {}).get("total", 0))}

    async def close(self) -> None:
        async with self._lock:
            if self._engine is not None:
                await self._engine.dispose()
            self._engine = None
            self._engine_url = None
