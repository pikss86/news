import ipaddress
import re
import zipfile
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.admin.app import create_admin_app
from app.admin.browser import telegram_message_url
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


def test_telegram_message_url_for_public_and_private_supergroups() -> None:
    message_id = 54_388 << 20

    assert (
        telegram_message_url(message_id, 1_254_661_214, "ostorozhno_novosti")
        == "https://t.me/ostorozhno_novosti/54388"
    )
    assert (
        telegram_message_url(message_id, 1_254_661_214, None)
        == "https://t.me/c/1254661214/54388"
    )
    assert telegram_message_url(message_id + 1, 1_254_661_214, "channel") is None
    assert telegram_message_url(message_id, None, "channel") is None


class Browser:
    def __init__(self) -> None:
        self.cached_path: Path | None = None

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
                "oldest_message_id": 20,
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
            "is_downloading_completed": self.cached_path is not None,
            "local_path": str(self.cached_path) if self.cached_path else None,
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
                "telegram_url": "https://t.me/news_chat/20",
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
    tdlib_data_dir = tmp_path / "tdlib"
    store.save_draft(settings(tdlib_data_dir=tdlib_data_dir))
    password_store = AdminPasswordStore(tmp_path / "settings" / "admin.json")
    password_store.set_password("a sufficiently long password")
    control = ControlChannel(tmp_path / "settings", bootstrap.control_key_file)
    control.write_status({"state": "running", "last_request_id": 0, "error": None})
    browser = Browser()
    app = create_admin_app(
        store=store,
        control=control,
        password_store=password_store,
        network=AdminNetwork("awg0", "10.8.0.1", (ipaddress.ip_network("10.8.0.0/24"),)),
        secrets_directory=tmp_path / "secrets",
        data_browser=browser,  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app, client=("10.8.0.2", 1234))
    async with AsyncClient(
        transport=transport, base_url="http://service", follow_redirects=False
    ) as client:
        denied = await client.get("/browser")
        assert denied.status_code == 303
        assert denied.headers["location"] == "/login?next=%2Fbrowser"
        denied_file = await client.get("/browser/files/501/content")
        assert denied_file.status_code == 303
        assert denied_file.headers["location"] == "/login?next=%2Fbrowser%2Ffiles%2F501%2Fcontent"
        denied_cache = await client.get("/browser/cache")
        assert denied_cache.status_code == 303
        assert denied_cache.headers["location"] == "/login?next=%2Fbrowser%2Fcache"
        denied_handlers = await client.get("/browser/cache/handlers")
        assert denied_handlers.status_code == 303
        assert denied_handlers.headers["location"] == "/login?next=%2Fbrowser%2Fcache%2Fhandlers"

        direct_message = await client.get("/browser/messages/10/20")
        login_location = direct_message.headers["location"]
        assert login_location == "/login?next=%2Fbrowser%2Fmessages%2F10%2F20"
        login_page = await client.get(login_location)
        assert 'name="next" value="/browser/messages/10/20"' in login_page.text
        response = await client.post(
            "/login",
            data={
                "csrf_token": csrf(login_page.text),
                "password": "a sufficiently long password",
                "next": "/browser/messages/10/20",
            },
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/browser/messages/10/20"

        overview = await client.get("/browser")
        assert overview.status_code == 200
        assert "Собранные данные" in overview.text
        assert "updateNewMessage" in overview.text

        chat = await client.get("/browser/chats/10")
        assert chat.status_code == 200
        assert "Чат → сообщение → прикреплённые файлы" in chat.text
        assert "Скачать" in chat.text
        assert "Загрузить ещё 100 из Telegram" in chat.text

        message = await client.get("/browser/messages/10/20")
        assert message.status_code == 200
        assert "Прикреплённые файлы" in message.text
        assert "Скачать в кэш" in message.text
        assert 'href="https://t.me/news_chat/20"' in message.text
        assert "Открыть в Telegram" in message.text

        event = await client.get("/browser/events/1")
        assert event.status_code == 200
        assert "Оригинальный TDLib JSON" in event.text
        assert "<script>" not in event.text
        assert "&lt;script&gt;" in event.text

        async def no_wait(_: float) -> None:
            return None

        monkeypatch.setattr("app.admin.app.asyncio.sleep", no_wait)
        history = await client.post(
            "/browser/chats/10/history",
            data={"csrf_token": csrf(chat.text)},
        )
        assert history.status_code == 303
        assert history.headers["location"] == "/browser/chats/10?notice=history-pending"
        assert control.read_request()["action"] == "load_chat_history"  # type: ignore[index]
        assert control.read_request()["chat_id"] == 10  # type: ignore[index]
        assert control.read_request()["from_message_id"] == 20  # type: ignore[index]

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

        missing = await client.get("/browser/files/501/content")
        assert missing.status_code == 404

        cache_directory = tdlib_data_dir / "files"
        cache_directory.mkdir(parents=True)
        image = cache_directory / "telegram-image"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage-data")
        browser.cached_path = image

        cached = await client.get("/browser/files/501/content")
        assert cached.status_code == 200
        assert cached.content == b"\x89PNG\r\n\x1a\nimage-data"
        assert cached.headers["content-type"] == "image/png"
        assert cached.headers["content-disposition"].startswith("inline;")

        documents = cache_directory / "documents"
        documents.mkdir()
        document = documents / "example file.pdf"
        document.write_bytes(b"%PDF-1.4 cached-document")
        archive = cache_directory / "telegram-archive"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("reports/daily report.pdf", b"%PDF-1.7 clean-pdf-content")
            bundle.writestr("reports/notes.txt", b"archive notes")
            bundle.writestr("../outside-from-archive.pdf", b"must not be visible")
        cache_root = await client.get("/browser/cache")
        assert cache_root.status_code == 200
        assert "Кэш TDLib" in cache_root.text
        assert "documents" in cache_root.text
        assert "telegram-image" in cache_root.text
        assert "telegram-archive" in cache_root.text
        assert "Открыть архив" in cache_root.text
        assert "Обработчики и найденные архивы" in cache_root.text

        handlers_page = await client.get("/browser/cache/handlers")
        assert handlers_page.status_code == 200
        assert "Потоковые обработчики" in handlers_page.text
        assert "ZIP-архив" in handlers_page.text
        assert "Активен · найдено 1" in handlers_page.text
        assert "telegram-archive" in handlers_page.text
        assert "Расширение .zip или сигнатура PK" in handlers_page.text

        cache_folder = await client.get("/browser/cache", params={"path": "documents"})
        assert cache_folder.status_code == 200
        assert "example file.pdf" in cache_folder.text
        cached_document = await client.get(
            "/browser/cache/content", params={"path": "documents/example file.pdf"}
        )
        assert cached_document.status_code == 200
        assert cached_document.content == b"%PDF-1.4 cached-document"
        assert cached_document.headers["content-type"] == "application/pdf"
        assert cached_document.headers["content-disposition"].startswith("inline;")

        archive_root = await client.get(
            "/browser/cache/archive",
            params={"path": "telegram-archive", "handler": "zip"},
        )
        assert archive_root.status_code == 200
        assert "reports" in archive_root.text
        assert "outside-from-archive.pdf" not in archive_root.text
        archive_folder = await client.get(
            "/browser/cache/archive",
            params={
                "path": "telegram-archive",
                "handler": "zip",
                "inside": "reports",
            },
        )
        assert archive_folder.status_code == 200
        assert "daily report.pdf" in archive_folder.text
        extracted_pdf = await client.get(
            "/browser/cache/archive/content",
            params={
                "path": "telegram-archive",
                "handler": "zip",
                "inside": "reports/daily report.pdf",
            },
        )
        assert extracted_pdf.status_code == 200
        assert extracted_pdf.content == b"%PDF-1.7 clean-pdf-content"
        assert extracted_pdf.headers["content-type"] == "application/pdf"
        assert extracted_pdf.headers["content-disposition"].startswith("inline;")
        unsafe_member = await client.get(
            "/browser/cache/archive/content",
            params={
                "path": "telegram-archive",
                "handler": "zip",
                "inside": "../outside-from-archive.pdf",
            },
        )
        assert unsafe_member.status_code == 422

        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
        traversal = await client.get("/browser/cache/content", params={"path": "../outside.png"})
        assert traversal.status_code == 404

        unsafe = cache_directory / "active.html"
        unsafe.write_text("<script>alert(1)</script>")
        browser.cached_path = unsafe
        unsafe_response = await client.get("/browser/files/501/content")
        assert unsafe_response.status_code == 200
        assert unsafe_response.headers["content-type"] == "application/octet-stream"
        assert unsafe_response.headers["content-disposition"].startswith("attachment;")

        browser.cached_path = outside
        escaped = await client.get("/browser/files/501/content")
        assert escaped.status_code == 404
