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

    async def chat(self, database_url, chat_id):  # type: ignore[no-untyped-def]
        attachment = {
            "file_id": 501,
            "size": 100,
            "expected_size": 100,
            "downloaded_size": 0,
            "can_be_downloaded": True,
            "is_downloading_completed": False,
            "is_downloading_active": False,
            "download_requested_at": None,
        }
        return {
            "chat": {
                "chat_id": chat_id,
                "title": "News chat",
                "chat_type": "channel",
                "message_count": 1,
                "first_collected_at": "then",
                "last_collected_at": "now",
                "raw_chat": {},
                "last_update": {},
            },
            "messages": [
                {
                    "chat_id": chat_id,
                    "message_id": 20,
                    "text": "A message",
                    "content_type": "messageDocument",
                    "published_at": "now",
                    "last_collected_at": "now",
                    "current_version": 1,
                    "is_deleted": False,
                    "attachments": [attachment],
                }
            ],
        }

    async def file(self, database_url, file_id):  # type: ignore[no-untyped-def]
        return {
            "file_id": file_id,
            "can_be_downloaded": True,
            "is_downloading_completed": False,
        }

    async def message(self, database_url, chat_id, message_id):  # type: ignore[no-untyped-def]
        attachment = await self.file(database_url, 501)
        attachment.update(
            {
                "size": 100,
                "expected_size": 100,
                "downloaded_size": 0,
                "is_downloading_active": False,
                "download_requested_at": None,
                "local_path": None,
            }
        )
        return {
            "message": {
                "chat_id": chat_id,
                "message_id": message_id,
                "chat_title": "News chat",
                "text": "A message",
                "content_type": "messageDocument",
                "published_at": "now",
                "first_collected_at": "now",
                "last_collected_at": "now",
                "current_version": 1,
                "current_event_id": 1,
                "is_deleted": False,
                "edited_at": None,
                "deleted_at": None,
                "content": {},
                "sender": {},
                "forward_info": None,
                "reply_to": None,
                "media": {},
                "interaction_info": {},
            },
            "versions": [],
            "attachments": [attachment],
        }

    async def close(self) -> None:
        return None


async def test_data_browser_requires_login_escapes_json_and_queues_download(
    tmp_path, monkeypatch
) -> None:
    bootstrap = ensure_bootstrap(tmp_path / "secrets")
    store = SettingsStore(tmp_path / "settings", bootstrap.settings_key_file)
    store.save_draft(settings())
    password_store = AdminPasswordStore(tmp_path / "settings" / "admin.json")
    password_store.set_password("a sufficiently long password")
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)
    control.write_status({"state": "running", "last_request_id": 0, "error": None})
    app = create_admin_app(
        store=store,
        control=control,
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

        chat = await client.get("/browser/chats/10")
        assert chat.status_code == 200
        assert "Чат → сообщение → прикреплённые файлы" in chat.text
        assert "Скачать" in chat.text

        message = await client.get("/browser/messages/10/20")
        assert message.status_code == 200
        assert "Прикреплённые файлы" in message.text
        assert "Скачать в кэш" in message.text

        event = await client.get("/browser/events/1")
        assert event.status_code == 200
        assert "Оригинальный TDLib JSON" in event.text
        assert "<script>" not in event.text
        assert "&lt;script&gt;" in event.text

        async def no_wait(_: float) -> None:
            return None

        monkeypatch.setattr("app.admin.app.asyncio.sleep", no_wait)
        dashboard = await client.get("/")
        download = await client.post(
            "/browser/files/501/download",
            data={
                "csrf_token": csrf(dashboard.text),
                "return_to": "/browser/files",
            },
        )
        assert download.status_code == 303
        assert download.headers["location"] == "/browser/files?notice=download-pending"
        assert control.read_request()["action"] == "download_file"  # type: ignore[index]
        assert control.read_request()["file_id"] == 501  # type: ignore[index]
