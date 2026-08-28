import asyncio
from dataclasses import dataclass

from app.settings.bootstrap import ensure_bootstrap
from app.settings.control import ControlChannel
from app.settings.store import SettingsStore
from app.supervisor import CollectorSupervisor
from tests.factories import settings


@dataclass
class Service:
    stopped: bool = False
    dynamic: tuple[bool, int] | None = None

    def stop(self) -> None:
        self.stopped = True

    def apply_dynamic(
        self, *, download_media: bool, download_priority: int, tdlib_log_verbosity: int
    ) -> None:
        self.dynamic = (download_media, download_priority)


class Runtime:
    def __init__(self, *, on_persisted_update, on_started) -> None:  # type: ignore[no-untyped-def]
        self.service = Service()
        self.on_persisted_update = on_persisted_update
        self.on_started = on_started
        self.finished = asyncio.Event()
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        await self.on_started()
        await self.on_persisted_update()
        await self.stop_event.wait()
        self.finished.set()

    def stop(self) -> None:
        self.service.stop()
        self.stop_event.set()


def checks(store: SettingsStore, revision: int) -> None:
    store.save_checks(
        revision,
        {name: {"ok": True, "status": "ready", "message": "ok"} for name in store.REQUIRED_CHECKS},
    )


async def test_supervisor_lifecycle_last_update_dynamic_restart_and_idempotency(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)
    created: list[Runtime] = []
    migrations: list[str] = []

    def factory(settings, **callbacks):  # type: ignore[no-untyped-def]
        callbacks.pop("authorization_input", None)
        runtime = Runtime(**callbacks)
        created.append(runtime)
        return runtime

    supervisor = CollectorSupervisor(
        store,
        control,
        runtime_factory=factory,  # type: ignore[arg-type]
        migration_runner=migrations.append,
        poll_seconds=0.01,
    )
    first = store.save_draft(settings())
    checks(store, first)
    store.activate_draft()
    await supervisor.process_request({"request_id": 1, "action": "start", "revision": first})
    await asyncio.sleep(0)
    status = control.read_status()
    assert status["state"] == "running"
    assert status["last_update_at"] is not None
    assert store.manifest()["applied_revision"] == first
    assert len(created) == 1 and len(migrations) == 1

    await supervisor.process_request({"request_id": 1, "action": "start", "revision": first})
    assert len(created) == 1

    dynamic = store.save_draft(settings(telegram_download_media=True, log_level="DEBUG"))
    checks(store, dynamic)
    store.activate_draft()
    await supervisor.process_request({"request_id": 2, "action": "start", "revision": dynamic})
    assert len(created) == 1
    assert created[0].service.dynamic == (True, 16)
    assert store.manifest()["applied_revision"] == dynamic

    restarted = store.save_draft(settings(telegram_api_id=67890))
    checks(store, restarted)
    store.activate_draft()
    await supervisor.process_request({"request_id": 3, "action": "start", "revision": restarted})
    assert len(created) == 2
    assert created[0].finished.is_set()
    assert len(migrations) == 2

    await supervisor.process_request({"request_id": 4, "action": "stop", "revision": None})
    assert control.read_status()["state"] == "stopped"
    assert created[1].finished.is_set()


async def test_supervisor_reports_worker_failure_during_startup(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)

    class FailedRuntime:
        service = Service()

        async def run(self) -> None:
            raise RuntimeError("TDLib startup failed")

        def stop(self) -> None:
            self.service.stop()

    def factory(settings, **callbacks):  # type: ignore[no-untyped-def]
        return FailedRuntime()

    revision = store.save_draft(settings())
    checks(store, revision)
    store.activate_draft()
    supervisor = CollectorSupervisor(
        store,
        control,
        runtime_factory=factory,  # type: ignore[arg-type]
        migration_runner=lambda _: None,
    )

    await supervisor.process_request({"request_id": 1, "action": "start", "revision": revision})

    status = control.read_status()
    assert status["state"] == "error"
    assert status["desired_state"] == "stopped"
    assert "RuntimeError" in status["error"]
