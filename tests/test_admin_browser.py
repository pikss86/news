import ipaddress
import re

from httpx import ASGITransport, AsyncClient

from app.admin.app import create_admin_app
from app.admin.network import AdminNetwork
from app.admin.security import AdminPasswordStore
from app.settings.bootstrap import ensure_bootstrap
from app.settings.control import ControlChannel
from app.settings.store import SettingsStore
from tests.factories import settings


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


class Browser:
    async def overview(self, database_url):  # type: ignore[no-untyped-def]
        return {
            "stats": {
                "events": 12,
                "chats": 2,
                "messages": 1,
                "versions": 2,
                "deleted": 0,
                "files": 0,
                "files_ready": 0,
                "last_event_at": "now",
            },
            "recent_messages": [],
            "recent_events": [
                {
                    "id": 1,
                    "event_type": "updateNewMessage",
                    "received_at": "now",
                    "chat_id": 10,
                    "message_id": 20,
                }
            ],
        }

    async def event(self, database_url, event_id):  # type: ignore[no-untyped-def]
        return {
            "id": event_id,
            "event_type": "updateNewMessage",
            "received_at": "now",
            "chat_id": 10,
            "message_id": 20,
            "event_fingerprint": "abc",
            "payload": {"text": "<script>alert(1)</script>"},
        }

    async def close(self) -> None:
        return None


async def test_data_browser_requires_login_and_escapes_raw_json(tmp_path) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    store.save_draft(settings())
    password_store = AdminPasswordStore(tmp_path / "settings" / "admin.json")
    password_store.set_password("a sufficiently long password")
    app = create_admin_app(
        store=store,
        control=ControlChannel(tmp_path / "settings", bootstrap.control_key_file),
        password_store=password_store,
        network=AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),)),
        secrets_directory=tmp_path / "secrets",
        data_browser=Browser(),  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(
        transport=transport, base_url="http://service", follow_redirects=False
    ) as client:
        denied = await client.get("/browser")
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login"

        login_page = await client.get("/login")
        response = await client.post(
            "/login",
            data={
                "csrf_token": csrf(login_page.text),
                "password": "a sufficiently long password",
            },
        )
        assert response.status_code == 303

        overview = await client.get("/browser")
        assert overview.status_code == 200
        assert "Собранные данные" in overview.text
        assert "updateNewMessage" in overview.text

        event = await client.get("/browser/events/1")
        assert event.status_code == 200
        assert "Оригинальный TDLib JSON" in event.text
        assert "<script>" not in event.text
        assert "&lt;script&gt;" in event.text
