import ipaddress
import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.admin.app import create_admin_app
from app.admin.network import AdminNetwork
from app.admin.security import AdminPasswordStore
from app.settings.bootstrap import ensure_bootstrap
from app.settings.control import ControlChannel
from app.settings.store import SettingsStore


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def form_values(**overrides: str) -> dict[str, str]:
    result = {
        "database_url": "postgresql+asyncpg://news:database-secret@postgres:5432/news",
        "telegram_api_id": "12345",
        "telegram_api_hash": "api-secret",
        "telegram_phone_number": "+10000000000",
        "telegram_database_encryption_key": "tdlib-secret",
        "tdlib_data_dir": "/tmp/news-tdlib",
        "tdlib_library_path": "/usr/local/lib/libtdjson.so",
        "tdlib_log_verbosity": "2",
        "telegram_media_download_priority": "16",
        "log_level": "INFO",
        "database_retry_initial_seconds": "1",
        "database_retry_max_seconds": "30",
    }
    result.update(overrides)
    return result


class Checks:
    async def run(self, settings, *, telegram_ready=False):  # type: ignore[no-untyped-def]
        return {
            name: {"ok": True, "status": "ready", "message": "safe"}
            for name in {"postgresql", "migrations", "tdlib", "storage", "telegram"}
        }


class Telegram:
    def __init__(self) -> None:
        self.ready = False
        self.draft_hash = None
        self.challenge = None

    async def start(self, settings, draft_hash):  # type: ignore[no-untyped-def]
        self.ready = True
        self.draft_hash = draft_hash

    async def stop(self) -> None:
        return None

    def state(self):  # type: ignore[no-untyped-def]
        return {
            "running": self.ready or self.challenge is not None,
            "ready": self.ready,
            "state": "authorizationStateReady" if self.ready else "not_started",
            "challenge": self.challenge,
            "error": None,
            "draft_hash": self.draft_hash,
        }

    def respond(self, correlation_id, values):  # type: ignore[no-untyped-def]
        return None


async def test_first_run_draft_checks_start_and_vpn_boundary(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)
    network = AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),))
    app = create_admin_app(
        store=store,
        control=control,
        password_store=AdminPasswordStore(tmp_path / "settings" / "admin.json"),
        network=network,
        secrets_directory=tmp_path / "secrets",
        preflight=Checks(),  # type: ignore[arg-type]
        telegram=Telegram(),  # type: ignore[arg-type]
        cookie_secure=True,
    )

    denied_transport = ASGITransport(app=app, client=("192.168.1.5", 1234))
    async with AsyncClient(transport=denied_transport, base_url="http://service") as denied:
        response = await denied.get("/", headers={"X-Forwarded-For": "10.8.0.2"})
        assert response.status_code == 403

    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(
        transport=transport, base_url="https://service", follow_redirects=False
    ) as client:
        response = await client.get("/")
        assert response.headers["location"] == "/setup"
        setup = await client.get("/setup")
        response = await client.post(
            "/setup",
            data={
                "csrf_token": csrf(setup.text),
                "password": "a sufficiently long password",
                "confirmation": "a sufficiently long password",
            },
        )
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
        dashboard = await client.get("/")
        token = csrf(dashboard.text)

        invalid = form_values(telegram_api_id="0")
        invalid["csrf_token"] = token
        response = await client.post("/settings", data=invalid)
        assert response.status_code == 200
        assert store.manifest()["draft_revision"] is None

        dashboard = await client.get("/")
        valid = form_values()
        valid["csrf_token"] = csrf(dashboard.text)
        response = await client.post("/settings", data=valid)
        assert response.status_code == 303
        assert store.manifest()["draft_revision"] == 1

        dashboard = await client.get("/")
        assert "api-secret" not in dashboard.text
        assert "Сохранено безопасно" in dashboard.text
        response = await client.post("/telegram/start", data={"csrf_token": csrf(dashboard.text)})
        assert response.status_code == 303
        assert response.headers["location"] == "/telegram"
        telegram_page = await client.get("/telegram")
        assert "Telegram подключён" in telegram_page.text

        dashboard = await client.get("/")
        response = await client.post("/checks", data={"csrf_token": csrf(dashboard.text)})
        assert response.status_code == 303

        dashboard = await client.get("/")
        response = await client.post("/control/start", data={"csrf_token": csrf(dashboard.text)})
        assert response.status_code == 303
        assert store.manifest()["active_revision"] == 1
        assert control.read_request()["action"] == "start"  # type: ignore[index]

        dashboard = await client.get("/")
        response = await client.post("/control/stop", data={"csrf_token": csrf(dashboard.text)})
        assert response.status_code == 303
        assert control.read_request()["action"] == "stop"  # type: ignore[index]

        dashboard = await client.get("/")
        response = await client.post(
            "/revisions/1/rollback", data={"csrf_token": csrf(dashboard.text)}
        )
        assert response.status_code == 303
        assert store.manifest()["draft_revision"] == 2
        assert store.manifest()["active_revision"] == 1
        assert control.read_request()["action"] == "stop"  # rollback does not start

        dashboard = await client.get("/")
        response = await client.post(
            "/password",
            data={
                "csrf_token": csrf(dashboard.text),
                "current_password": "a sufficiently long password",
                "new_password": "a different long password",
                "confirmation": "a different long password",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert "different" not in (tmp_path / "settings" / "admin.json").read_text()


async def test_csrf_token_is_required_and_one_time(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    network = AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),))
    app = create_admin_app(
        store=SettingsStore(tmp_path / "settings", bootstrap.settings_key_file),
        control=ControlChannel(tmp_path / "settings", bootstrap.control_key_file),
        password_store=AdminPasswordStore(tmp_path / "settings" / "admin.json"),
        network=network,
        secrets_directory=tmp_path / "secrets",
    )
    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(transport=transport, base_url="http://service") as client:
        page = await client.get("/setup")
        token = csrf(page.text)
        assert (await client.post("/setup", data={})).status_code == 403
        assert (
            await client.post(
                "/setup",
                data={"csrf_token": "wrong", "password": "x", "confirmation": "x"},
            )
        ).status_code == 403
        data = {
            "csrf_token": token,
            "password": "a sufficiently long password",
            "confirmation": "mismatch password value",
        }
        assert (await client.post("/setup", data=data)).status_code == 422
        assert (await client.post("/setup", data=data)).status_code == 403


def test_templates_are_responsive_and_use_only_local_assets() -> None:
    css = (Path(__file__).parents[1] / "app/admin/static/admin.css").read_text()
    base = (Path(__file__).parents[1] / "app/admin/templates/base.html").read_text()
    assert "@media(max-width:720px)" in css
    assert "grid-template-columns: 1fr" in css
    assert "cdn" not in base.lower()
    assert 'name="viewport"' in base
    dashboard = (Path(__file__).parents[1] / "app/admin/templates/dashboard.html").read_text()
    assert "my.telegram.org/apps" in dashboard
    assert "Что нужно подготовить" in dashboard
    assert "metadata.help" in dashboard


async def test_login_rejects_external_return_url(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    password_store = AdminPasswordStore(tmp_path / "settings" / "admin.json")
    password_store.set_password("a sufficiently long password")
    app = create_admin_app(
        store=SettingsStore(tmp_path / "settings", bootstrap.settings_key_file),
        control=ControlChannel(tmp_path / "settings", bootstrap.control_key_file),
        password_store=password_store,
        network=AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),)),
        secrets_directory=tmp_path / "secrets",
    )
    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(
        transport=transport, base_url="http://service", follow_redirects=False
    ) as client:
        login_page = await client.get("/login?next=https%3A%2F%2Fexample.com")
        response = await client.post(
            "/login",
            data={
                "csrf_token": csrf(login_page.text),
                "password": "a sufficiently long password",
                "next": "https://example.com",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


async def test_telegram_code_has_a_simple_dedicated_form(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    password_store = AdminPasswordStore(tmp_path / "settings" / "admin.json")
    password_store.set_password("a sufficiently long password")
    telegram = Telegram()
    telegram.challenge = {
        "correlation_id": "challenge-id",
        "kind": "code",
        "presentation": {},
    }
    app = create_admin_app(
        store=SettingsStore(tmp_path / "settings", bootstrap.settings_key_file),
        control=ControlChannel(tmp_path / "settings", bootstrap.control_key_file),
        password_store=password_store,
        network=AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),)),
        secrets_directory=tmp_path / "secrets",
        telegram=telegram,  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(transport=transport, base_url="http://service") as client:
        login_page = await client.get("/login")
        logged_in = await client.post(
            "/login",
            data={
                "csrf_token": csrf(login_page.text),
                "password": "a sufficiently long password",
            },
        )
        assert logged_in.status_code == 303

        page = await client.get("/telegram")
        assert page.status_code == 200
        assert "Введите код из Telegram" in page.text
        assert 'name="code"' in page.text
        assert 'name="password"' not in page.text
        assert 'http-equiv="refresh"' not in page.text
