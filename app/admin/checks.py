from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.settings.redaction import redact_text
from app.tdlib.client import TdJsonClient


def result(ok: bool, status: str, message: str) -> dict[str, Any]:
    return {"ok": ok, "status": status, "message": redact_text(message)}


class PreflightRunner:
    def __init__(
        self,
        alembic_ini: Path = Path("alembic.ini"),
        database_host_override: str | None = None,
    ) -> None:
        self.alembic_ini = alembic_ini
        self.database_host_override = database_host_override

    async def check_database(self, settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
        database_url = make_url(settings.database_url)
        if self.database_host_override:
            database_url = database_url.set(host=self.database_host_override)
        engine = create_async_engine(database_url, pool_pre_ping=True)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

                def current_revision(sync_connection: Any) -> str | None:
                    return MigrationContext.configure(sync_connection).get_current_revision()

                current = await connection.run_sync(current_revision)
            config = Config(str(self.alembic_ini))
            expected = ScriptDirectory.from_config(config).get_current_head()
            migration_ok = current == expected
            return (
                result(True, "ready", "PostgreSQL connection succeeded"),
                result(
                    True,
                    "ready" if migration_ok else "action_required",
                    "Database schema is current"
                    if migration_ok
                    else f"Database migration required: current={current}, expected={expected}",
                ),
            )
        except Exception as error:
            message = f"PostgreSQL connection failed: {type(error).__name__}"
            failed = result(False, "error", message)
            return failed, result(False, "blocked", "Migration status unavailable")
        finally:
            await engine.dispose()

    async def check_tdlib(self, settings: Settings) -> dict[str, Any]:
        try:
            client = await asyncio.to_thread(TdJsonClient, settings.tdlib_library_path)
            client.close()
            return result(True, "ready", "TDLib loaded successfully")
        except Exception as error:
            return result(False, "error", f"TDLib load failed: {type(error).__name__}")

    async def check_storage(self, settings: Settings) -> dict[str, Any]:
        try:
            directories = [settings.tdlib_data_dir, settings.tdlib_data_dir / "files"]
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".news-write-probe"
                probe.write_bytes(b"ok")
                probe.unlink()
            free = shutil.disk_usage(settings.tdlib_data_dir).free
            if free < 100 * 1024 * 1024:
                return result(False, "error", "Less than 100 MiB is available for TDLib data")
            return result(True, "ready", f"Storage is writable; {free // (1024**2)} MiB free")
        except Exception as error:
            return result(False, "error", f"Storage check failed: {type(error).__name__}")

    async def run(
        self, settings: Settings, *, telegram_ready: bool = False
    ) -> dict[str, dict[str, Any]]:
        postgresql, migrations = await self.check_database(settings)
        tdlib, storage = await asyncio.gather(
            self.check_tdlib(settings), self.check_storage(settings)
        )
        telegram = result(
            telegram_ready,
            "ready" if telegram_ready else "action_required",
            "Telegram session is authorized"
            if telegram_ready
            else "Complete Telegram authorization in the admin page",
        )
        return {
            "postgresql": postgresql,
            "migrations": migrations,
            "tdlib": tdlib,
            "storage": storage,
            "telegram": telegram,
        }
