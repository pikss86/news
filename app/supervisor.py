from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config

from app.config import Settings
from app.logging import configure_logging
from app.main import CollectorRuntime, create_collector
from app.settings.bootstrap import load_bootstrap
from app.settings.control import ControlChannel
from app.settings.redaction import redact
from app.settings.store import SettingsStore, SettingsStoreError
from app.tdlib.auth import NonInteractiveAuthorizationInput

logger = logging.getLogger(__name__)


def run_migrations(database_url: str, alembic_ini: Path = Path("alembic.ini")) -> None:
    config = Config(str(alembic_ini))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


class CollectorSupervisor:
    def __init__(
        self,
        store: SettingsStore,
        control: ControlChannel,
        *,
        runtime_factory: Callable[..., CollectorRuntime] = create_collector,
        migration_runner: Callable[[str], None] = run_migrations,
        poll_seconds: float = 0.5,
    ) -> None:
        self.store = store
        self.control = control
        self.runtime_factory = runtime_factory
        self.migration_runner = migration_runner
        self.poll_seconds = poll_seconds
        self.runtime: CollectorRuntime | None = None
        self.worker: asyncio.Task[None] | None = None
        self.running_revision: int | None = None
        self.running_settings: Settings | None = None
        self.last_request_id = 0
        self.desired_state = "stopped"
        self.stopping = asyncio.Event()

    def _write_status(self, **changes: Any) -> None:
        previous = self.control.read_status()
        status = {
            "state": previous.get("state", "stopped"),
            "desired_state": self.desired_state,
            "last_request_id": self.last_request_id,
            "active_revision": self.store.manifest().get("active_revision"),
            "applied_revision": self.store.manifest().get("applied_revision"),
            "last_update_at": previous.get("last_update_at"),
            "error": previous.get("error"),
            **changes,
        }
        self.control.write_status(redact(status))

    async def _on_update(self) -> None:
        self._write_status(last_update_at=datetime.now(UTC).isoformat())

    async def _worker_done(self, task: asyncio.Task[None]) -> None:
        if self.worker is not task:
            return
        error: str | None = None
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as problem:
            logger.exception("collector worker failed")
            error = f"{type(problem).__name__}: collector worker failed"
        self.worker = None
        self.runtime = None
        self.running_revision = None
        self.running_settings = None
        if error:
            self.desired_state = "stopped"
            self._write_status(state="error", error=error)
        elif not self.stopping.is_set():
            self._write_status(state="stopped", error=None)

    async def start(self, revision: int | None = None) -> None:
        manifest = self.store.manifest()
        target = revision if revision is not None else manifest.get("active_revision")
        if target is None or manifest.get("active_revision") != target:
            raise SettingsStoreError("there is no matching active revision")
        settings = self.store.load_revision(target)

        if self.worker and not self.worker.done():
            if target == self.running_revision:
                self.desired_state = "running"
                self._write_status(state="running", error=None)
                return
            assert self.running_settings is not None and self.runtime is not None
            changed = {
                name
                for name in Settings.model_fields
                if getattr(settings, name) != getattr(self.running_settings, name)
            }
            dynamic_only = changed and all(
                Settings.FIELD_METADATA[name]["apply"] == "dynamic" for name in changed
            )
            if dynamic_only:
                try:
                    self.runtime.service.apply_dynamic(
                        download_media=settings.telegram_download_media,
                        download_priority=settings.telegram_media_download_priority,
                        tdlib_log_verbosity=settings.tdlib_log_verbosity,
                    )
                    configure_logging(settings.log_level)
                    self.running_revision = target
                    self.running_settings = settings
                    self.store.mark_applied(target)
                    self.desired_state = "running"
                    self._write_status(state="running", applied_revision=target, error=None)
                    return
                except Exception:
                    logger.exception("dynamic settings apply failed; restarting collector")
            await self.stop(preserve_desired=True)

        self.desired_state = "running"
        self._write_status(state="starting", error=None)
        await asyncio.to_thread(self.migration_runner, settings.database_url)
        started = asyncio.Event()

        async def on_started() -> None:
            started.set()

        runtime = self.runtime_factory(
            settings,
            authorization_input=NonInteractiveAuthorizationInput(),
            on_persisted_update=self._on_update,
            on_started=on_started,
        )
        worker = asyncio.create_task(runtime.run(), name="telegram-ingestion-worker")
        self.runtime = runtime
        self.worker = worker
        self.running_revision = target
        self.running_settings = settings
        started_waiter = asyncio.create_task(started.wait())
        try:
            done, _ = await asyncio.wait(
                {started_waiter, worker}, timeout=60, return_when=asyncio.FIRST_COMPLETED
            )
            if worker in done:
                await worker
                raise RuntimeError("collector stopped before startup completed")
            if started_waiter not in done:
                raise TimeoutError("collector startup timed out")
        except Exception:
            runtime.stop()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            self.runtime = None
            self.worker = None
            raise
        finally:
            started_waiter.cancel()
        self.store.mark_applied(target)
        self._write_status(state="running", applied_revision=target, error=None)
        asyncio.create_task(self._worker_done(worker))

    async def stop(self, *, preserve_desired: bool = False) -> None:
        if not preserve_desired:
            self.desired_state = "stopped"
        if self.runtime is not None:
            self._write_status(state="stopping")
            self.runtime.stop()
        if self.worker is not None:
            await asyncio.gather(self.worker, return_exceptions=True)
        self.runtime = None
        self.worker = None
        self.running_revision = None
        self.running_settings = None
        self._write_status(state="stopped", error=None)

    async def process_request(self, request: dict[str, Any]) -> None:
        request_id = int(request["request_id"])
        if request_id <= self.last_request_id:
            return
        self.last_request_id = request_id
        action = request["action"]
        try:
            if action == "stop":
                await self.stop()
            elif action == "restart":
                await self.stop(preserve_desired=True)
                await self.start(request.get("revision"))
            else:
                await self.start(request.get("revision"))
        except Exception as error:
            logger.exception("supervisor command failed", extra={"request_id": request_id})
            self.desired_state = "stopped"
            self._write_status(
                state="error",
                error=f"{type(error).__name__}: command failed; inspect collector logs",
            )

    async def run(self) -> None:
        previous = self.control.read_status()
        self.last_request_id = int(previous.get("last_request_id", 0))
        self.desired_state = str(previous.get("desired_state", "stopped"))
        if self.desired_state == "running" and self.store.manifest().get("active_revision"):
            try:
                await self.start()
            except Exception as error:
                logger.exception("collector resume failed")
                self.desired_state = "stopped"
                self._write_status(
                    state="error",
                    error=f"{type(error).__name__}: resume failed; inspect collector logs",
                )
        else:
            self.desired_state = "stopped"
            self._write_status(state="stopped", error=None)
        while not self.stopping.is_set():
            request = self.control.read_request()
            if request:
                await self.process_request(request)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
        await self.stop()

    def request_shutdown(self) -> None:
        self.stopping.set()


async def async_main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    secrets_directory = Path(os.environ.get("NEWS_SECRETS_DIR", "/run/news-secrets"))
    settings_directory = Path(os.environ.get("NEWS_SETTINGS_DIR", "/var/lib/news-settings"))
    bootstrap = load_bootstrap(secrets_directory)
    supervisor = CollectorSupervisor(
        SettingsStore(settings_directory, bootstrap.settings_key_file),
        ControlChannel(settings_directory, bootstrap.control_key_file),
    )
    loop = asyncio.get_running_loop()
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(selected_signal, supervisor.request_shutdown)
    await supervisor.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
