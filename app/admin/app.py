from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.admin.browser import DataBrowser
from app.admin.checks import PreflightRunner
from app.admin.file_handlers import FileHandlerError, FileHandlerRegistry
from app.admin.network import AdminNetwork, VPNAccessMiddleware
from app.admin.security import AdminPasswordStore, LoginRateLimiter, SessionManager
from app.admin.telegram import TelegramAuthorizationSession
from app.config import Settings
from app.settings.bootstrap import bundled_database_url
from app.settings.control import ControlChannel
from app.settings.redaction import redact
from app.settings.store import SettingsStore, SettingsStoreError

logger = logging.getLogger(__name__)
COOKIE_NAME = "news_admin_session"
SAFE_INLINE_MEDIA_TYPES = {"application/pdf", "text/plain"}
NOTICES = {
    "draft-saved": "Черновик сохранён. Секретные поля защищены и поэтому не показываются повторно.",
    "checks-complete": "Проверки завершены. Результаты показаны выше.",
    "telegram-started": "Авторизация Telegram запущена.",
    "telegram-response-sent": "Ответ отправлен Telegram.",
    "start-requested": "Команда запуска отправлена collector-у.",
    "stop-requested": "Команда остановки отправлена collector-у.",
    "restart-requested": "Команда перезапуска отправлена collector-у.",
    "rollback-created": "Из выбранной ревизии создан новый черновик; он не запущен.",
}


def _safe_return_path(value: str | None) -> str:
    if not value or len(value) > 2048 or any(ord(character) < 32 for character in value):
        return "/"
    decoded = unquote(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
    ):
        return "/"
    return value


def _default_values(secrets_directory: Path) -> dict[str, Any]:
    return {
        "database_url": bundled_database_url(secrets_directory),
        "telegram_api_id": "",
        "telegram_api_hash": "",
        "telegram_phone_number": "",
        "telegram_database_encryption_key": "",
        "tdlib_data_dir": "/var/lib/tdlib",
        "tdlib_library_path": "/usr/local/lib/libtdjson.so",
        "tdlib_log_verbosity": 2,
        "telegram_download_media": False,
        "telegram_media_download_priority": 16,
        "log_level": "INFO",
        "database_retry_initial_seconds": 1.0,
        "database_retry_max_seconds": 30.0,
    }


def _display_values(settings: Settings | None, secrets_directory: Path) -> dict[str, Any]:
    values = settings.plain_dict() if settings else _default_values(secrets_directory)
    for name in Settings.secret_field_names():
        values[name] = ""
    return values


def _field_errors(error: ValidationError) -> dict[str, str]:
    return {
        str(item["loc"][0]): item["msg"].removeprefix("Value error, ")
        for item in error.errors()
        if item["loc"]
    }


def _cached_media_response(path: Path) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0]
    if media_type is None:
        with path.open("rb") as cached_file:
            header = cached_file.read(16)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            media_type = "image/png"
        elif header.startswith(b"\xff\xd8\xff"):
            media_type = "image/jpeg"
        elif header.startswith((b"GIF87a", b"GIF89a")):
            media_type = "image/gif"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            media_type = "image/webp"
        elif header.startswith(b"%PDF-"):
            media_type = "application/pdf"
        elif len(header) >= 8 and header[4:8] == b"ftyp":
            media_type = "video/mp4"
        elif header.startswith(b"OggS"):
            media_type = "audio/ogg"
        elif header.startswith(b"ID3"):
            media_type = "audio/mpeg"

    inline = (
        bool(
            media_type in SAFE_INLINE_MEDIA_TYPES
            or (media_type and media_type.startswith(("image/", "audio/", "video/")))
        )
        and media_type != "image/svg+xml"
    )
    return FileResponse(
        path,
        media_type=media_type if inline else "application/octet-stream",
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
    )


def _stream_delivery(name: str) -> tuple[str, str]:
    media_type = mimetypes.guess_type(name)[0]
    inline = (
        bool(
            media_type in SAFE_INLINE_MEDIA_TYPES
            or (media_type and media_type.startswith(("image/", "audio/", "video/")))
        )
        and media_type != "image/svg+xml"
    )
    return (
        media_type if inline else "application/octet-stream",
        "inline" if inline else "attachment",
    )


def _resolve_cached_file(data_directory: Path, local_path: str) -> Path | None:
    try:
        cache_directory = (data_directory / "files").resolve(strict=True)
        cached_file = Path(local_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not cached_file.is_file() or not cached_file.is_relative_to(cache_directory):
        return None
    return cached_file


def _resolve_cache_entry(data_directory: Path, relative_path: str) -> Path | None:
    try:
        cache_directory = (data_directory / "files").resolve(strict=True)
        requested = Path(relative_path)
        if requested.is_absolute():
            return None
        entry = (cache_directory / requested).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not entry.is_relative_to(cache_directory):
        return None
    return entry


def _resolve_cache_file(data_directory: Path, relative_path: str) -> Path | None:
    entry = _resolve_cache_entry(data_directory, relative_path)
    return entry if entry is not None and entry.is_file() else None


def _scan_cache_directory(
    data_directory: Path,
    relative_path: str,
    handlers: FileHandlerRegistry | None = None,
) -> tuple[str, list[dict[str, Any]]] | None:
    directory = _resolve_cache_entry(data_directory, relative_path)
    if directory is None or not directory.is_dir():
        return None
    cache_directory = (data_directory / "files").resolve(strict=True)
    current_path = directory.relative_to(cache_directory).as_posix()
    if current_path == ".":
        current_path = ""
    entries: list[dict[str, Any]] = []
    for child in directory.iterdir():
        try:
            resolved = child.resolve(strict=True)
            if not resolved.is_relative_to(cache_directory):
                continue
            stat_result = resolved.stat()
        except (OSError, RuntimeError):
            continue
        is_directory = resolved.is_dir()
        handler = handlers.matching(resolved) if handlers and not is_directory else None
        entries.append(
            {
                "name": child.name,
                "relative_path": resolved.relative_to(cache_directory).as_posix(),
                "is_directory": is_directory,
                "size": None if is_directory else stat_result.st_size,
                "modified_at": datetime.fromtimestamp(stat_result.st_mtime, UTC),
                "handler_id": handler.handler_id if handler else None,
                "handler_label": handler.label if handler else None,
            }
        )
    entries.sort(key=lambda item: (not item["is_directory"], item["name"].casefold()))
    return current_path, entries


def _scan_handled_cache_files(
    data_directory: Path,
    handlers: FileHandlerRegistry,
    *,
    max_files: int = 20_000,
) -> list[dict[str, Any]]:
    try:
        cache_directory = (data_directory / "files").resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    matches: list[dict[str, Any]] = []
    scanned = 0
    for candidate in cache_directory.rglob("*"):
        if scanned >= max_files:
            break
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(cache_directory) or not resolved.is_file():
                continue
            stat_result = resolved.stat()
        except (OSError, RuntimeError):
            continue
        scanned += 1
        handler = handlers.matching(resolved)
        if handler is None:
            continue
        matches.append(
            {
                "name": resolved.name,
                "relative_path": resolved.relative_to(cache_directory).as_posix(),
                "size": stat_result.st_size,
                "modified_at": datetime.fromtimestamp(stat_result.st_mtime, UTC),
                "handler_id": handler.handler_id,
                "handler_label": handler.label,
            }
        )
    matches.sort(key=lambda item: (item["handler_label"], item["relative_path"].casefold()))
    return matches


def _settings_from_form(form: Any, current: Settings | None, secrets_directory: Path) -> Settings:
    values = current.plain_dict() if current else _default_values(secrets_directory)
    for name in Settings.model_fields:
        metadata = Settings.FIELD_METADATA[name]
        if name == "telegram_download_media":
            values[name] = form.get(name) == "on"
            continue
        submitted = form.get(name)
        if metadata["secret"] and (submitted is None or not str(submitted).strip()):
            if name == "telegram_database_encryption_key" and form.get(f"delete_{name}") == "on":
                values[name] = ""
            continue
        if submitted is not None:
            values[name] = str(submitted).strip()
    return Settings.model_validate(values)


def create_admin_app(
    *,
    store: SettingsStore,
    control: ControlChannel,
    password_store: AdminPasswordStore,
    network: AdminNetwork,
    secrets_directory: Path,
    templates_directory: Path | None = None,
    static_directory: Path | None = None,
    cookie_secure: bool = False,
    preflight: PreflightRunner | None = None,
    telegram: TelegramAuthorizationSession | None = None,
    data_browser: DataBrowser | None = None,
    file_handlers: FileHandlerRegistry | None = None,
) -> FastAPI:
    sessions = SessionManager()
    limiter = LoginRateLimiter()
    runner = preflight or PreflightRunner()
    telegram_session = telegram or TelegramAuthorizationSession()
    browser = data_browser or DataBrowser()
    handler_registry = file_handlers or FileHandlerRegistry.defaults()
    base = Path(__file__).parent
    templates = Jinja2Templates(directory=str(templates_directory or base / "templates"))
    templates.env.filters["prettyjson"] = lambda value: json.dumps(
        value, ensure_ascii=False, indent=2, default=str
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await telegram_session.stop()
        await browser.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(VPNAccessMiddleware, allowed_networks=network.allowed_networks)
    app.mount(
        "/static",
        StaticFiles(directory=str(static_directory or base / "static")),
        name="static",
    )
    app.state.sessions = sessions
    app.state.telegram = telegram_session

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    def peer(request: Request) -> str:
        return str(request.scope.get("vpn_peer", "unknown"))

    def session_token(request: Request) -> str | None:
        return request.cookies.get(COOKIE_NAME)

    def require_session(request: Request) -> str:
        token = session_token(request)
        if not sessions.validate(token):
            location = "/login"
            if request.method in {"GET", "HEAD"}:
                return_path = request.url.path
                if request.url.query:
                    return_path = f"{return_path}?{request.url.query}"
                location = f"/login?{urlencode({'next': return_path})}"
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location}
            )
        assert token is not None
        return token

    async def valid_csrf(request: Request, principal: str, *, anonymous: bool = False) -> Any:
        form = await request.form()
        csrf_token = str(form.get("csrf_token", ""))
        if not sessions.consume_csrf(principal, csrf_token, anonymous=anonymous):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        return form

    def render_dashboard(
        request: Request,
        session: str,
        *,
        errors: dict[str, str] | None = None,
        notice: str | None = None,
    ) -> HTMLResponse:
        manifest = store.manifest()
        draft = store.load_draft()
        collector_state = redact(control.read_status())
        telegram_state = redact(telegram_session.state())
        if collector_state.get("state") == "running" and not collector_state.get("error"):
            telegram_state["ready"] = True
            telegram_state["state"] = "authorizationStateReady"
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "csrf_token": sessions.issue_csrf(session),
                "settings": _display_values(draft, secrets_directory),
                "configured_secrets": {
                    name: bool(draft and draft.plain_dict().get(name))
                    for name in Settings.secret_field_names()
                },
                "field_metadata": Settings.FIELD_METADATA,
                "errors": errors or {},
                "notice": notice
                or NOTICES.get(
                    request.query_params.get("notice", ""), request.query_params.get("notice")
                ),
                "manifest": manifest,
                "checks": redact(manifest.get("checks")),
                "collector": collector_state,
                "telegram": telegram_state,
                "revisions": store.list_revisions(),
            },
        )

    def render_telegram(
        request: Request,
        session: str,
        *,
        status_code: int = 200,
        error: str | None = None,
    ) -> HTMLResponse:
        telegram_state = redact(telegram_session.state())
        collector_state = redact(control.read_status())
        if collector_state.get("state") == "running" and not collector_state.get("error"):
            telegram_state["ready"] = True
            telegram_state["state"] = "authorizationStateReady"
        if error:
            telegram_state["error"] = {"message": error}
        challenge = telegram_state.get("challenge")
        return templates.TemplateResponse(
            request,
            "telegram.html",
            {
                "csrf_token": sessions.issue_csrf(session),
                "telegram": telegram_state,
                "challenge": challenge,
                "refresh": bool(
                    telegram_state.get("running")
                    and not telegram_state.get("ready")
                    and challenge is None
                ),
            },
            status_code=status_code,
        )

    def browser_settings() -> Settings | None:
        manifest = store.manifest()
        active_revision = manifest.get("active_revision")
        return (
            store.load_revision(active_revision)
            if active_revision is not None
            else store.load_draft()
        )

    def browser_database_url() -> str | None:
        settings = browser_settings()
        return settings.database_url if settings else None

    async def browser_query(
        operation: Callable[[str], Awaitable[Any]],
    ) -> tuple[Any | None, str | None]:
        database_url = browser_database_url()
        if database_url is None:
            return None, "Сначала сохраните настройки подключения к PostgreSQL."
        try:
            return await operation(database_url), None
        except Exception as error:
            logger.exception(
                "data browser query failed", extra={"error_type": type(error).__name__}
            )
            return (
                None,
                "Не удалось прочитать PostgreSQL. Проверьте подключение на странице настроек.",
            )

    def browser_error(request: Request, message: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "browser_error.html",
            {"message": message},
            status_code=503,
        )

    def pagination(
        path: str, page: int, total: int, per_page: int, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        def link(target: int) -> str:
            values = {key: value for key, value in parameters.items() if value not in (None, "")}
            values["page"] = target
            return f"{path}?{urlencode(values)}"

        return {
            "page": page,
            "total": total,
            "per_page": per_page,
            "prev_url": link(page - 1) if page > 1 else None,
            "next_url": link(page + 1) if page * per_page < total else None,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        if not password_store.configured:
            return RedirectResponse("/setup", status_code=303)
        token = session_token(request)
        if not sessions.validate(token):
            return RedirectResponse("/login", status_code=303)
        assert token is not None
        return render_dashboard(request, token)

    @app.get("/browser", response_class=HTMLResponse)
    async def browser_overview(request: Request) -> Any:
        require_session(request)
        data, error = await browser_query(browser.overview)
        if error:
            return browser_error(request, error)
        return templates.TemplateResponse(request, "browser_overview.html", data)

    @app.get("/browser/chats", response_class=HTMLResponse)
    async def browser_chats(request: Request, page: int = 1, q: str = "") -> Any:
        require_session(request)
        page = max(1, page)
        q = q.strip()[:200]
        per_page = 50
        data, error = await browser_query(
            lambda url: browser.chats(url, page=page, per_page=per_page, query=q)
        )
        if error:
            return browser_error(request, error)
        assert data is not None
        context = {
            **data,
            "q": q,
            "pagination": pagination("/browser/chats", page, data["total"], per_page, {"q": q}),
        }
        return templates.TemplateResponse(request, "browser_chats.html", context)

    @app.get("/browser/chats/{chat_id}", response_class=HTMLResponse)
    async def browser_chat(request: Request, chat_id: int) -> Any:
        token = require_session(request)
        data, error = await browser_query(lambda url: browser.chat(url, chat_id))
        if error:
            return browser_error(request, error)
        if data is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        return templates.TemplateResponse(
            request,
            "browser_chat.html",
            {**data, "csrf_token": sessions.issue_csrf(token)},
        )

    @app.post("/browser/chats/{chat_id}/history")
    async def browser_chat_history(request: Request, chat_id: int) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        return_to = f"/browser/chats/{chat_id}"
        collector_status = control.read_status()
        if collector_status.get("state") != "running":
            return RedirectResponse(f"{return_to}?notice=collector-stopped", status_code=303)
        data, error = await browser_query(lambda url: browser.chat(url, chat_id))
        if error or data is None:
            return RedirectResponse(f"{return_to}?notice=chat-missing", status_code=303)
        from_message_id = int(data["chat"].get("oldest_message_id") or 0)
        request_id = control.request_chat_history(chat_id, from_message_id, 100)
        for _ in range(100):
            await asyncio.sleep(0.1)
            current_status = control.read_status()
            if int(current_status.get("last_history_request_id", 0)) != request_id:
                continue
            if current_status.get("last_history_error"):
                notice = "history-failed"
            elif int(current_status.get("last_history_count", 0)) == 0:
                notice = "history-empty"
            else:
                notice = "history-loaded"
            count = int(current_status.get("last_history_count", 0))
            return RedirectResponse(
                f"{return_to}?{urlencode({'notice': notice, 'count': count})}",
                status_code=303,
            )
        return RedirectResponse(f"{return_to}?notice=history-pending", status_code=303)

    @app.get("/browser/messages", response_class=HTMLResponse)
    async def browser_messages(
        request: Request,
        page: int = 1,
        chat_id: int | None = None,
        q: str = "",
        deleted: str = "",
    ) -> Any:
        require_session(request)
        page = max(1, page)
        q = q.strip()[:200]
        deleted_value = {"yes": True, "no": False}.get(deleted)
        per_page = 50
        data, error = await browser_query(
            lambda url: browser.messages(
                url,
                page=page,
                per_page=per_page,
                chat_id=chat_id,
                query=q,
                deleted=deleted_value,
            )
        )
        if error:
            return browser_error(request, error)
        assert data is not None
        context = {
            **data,
            "q": q,
            "chat_id": chat_id,
            "deleted": deleted,
            "pagination": pagination(
                "/browser/messages",
                page,
                data["total"],
                per_page,
                {"q": q, "chat_id": chat_id, "deleted": deleted},
            ),
        }
        return templates.TemplateResponse(request, "browser_messages.html", context)

    @app.get("/browser/messages/{chat_id}/{message_id}", response_class=HTMLResponse)
    async def browser_message(request: Request, chat_id: int, message_id: int) -> Any:
        token = require_session(request)
        data, error = await browser_query(lambda url: browser.message(url, chat_id, message_id))
        if error:
            return browser_error(request, error)
        if data is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return templates.TemplateResponse(
            request,
            "browser_message.html",
            {**data, "csrf_token": sessions.issue_csrf(token)},
        )

    @app.get("/browser/events", response_class=HTMLResponse)
    async def browser_events(
        request: Request,
        page: int = 1,
        event_type: str = "",
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> Any:
        require_session(request)
        page = max(1, page)
        event_type = event_type.strip()[:128]
        per_page = 50
        data, error = await browser_query(
            lambda url: browser.events(
                url,
                page=page,
                per_page=per_page,
                event_type=event_type,
                chat_id=chat_id,
                message_id=message_id,
            )
        )
        if error:
            return browser_error(request, error)
        assert data is not None
        context = {
            **data,
            "selected_event_type": event_type,
            "chat_id": chat_id,
            "message_id": message_id,
            "pagination": pagination(
                "/browser/events",
                page,
                data["total"],
                per_page,
                {"event_type": event_type, "chat_id": chat_id, "message_id": message_id},
            ),
        }
        return templates.TemplateResponse(request, "browser_events.html", context)

    @app.get("/browser/events/{event_id}", response_class=HTMLResponse)
    async def browser_event(request: Request, event_id: int) -> Any:
        require_session(request)
        data, error = await browser_query(lambda url: browser.event(url, event_id))
        if error:
            return browser_error(request, error)
        if data is None:
            raise HTTPException(status_code=404, detail="Event not found")
        return templates.TemplateResponse(request, "browser_event.html", {"event": data})

    @app.get("/browser/files", response_class=HTMLResponse)
    async def browser_files(request: Request, page: int = 1, state: str = "") -> Any:
        token = require_session(request)
        page = max(1, page)
        state = state if state in {"", "ready", "pending", "available"} else ""
        per_page = 50
        data, error = await browser_query(
            lambda url: browser.files(url, page=page, per_page=per_page, state=state)
        )
        if error:
            return browser_error(request, error)
        assert data is not None
        context = {
            **data,
            "state": state,
            "pagination": pagination(
                "/browser/files", page, data["total"], per_page, {"state": state}
            ),
            "csrf_token": sessions.issue_csrf(token),
        }
        return templates.TemplateResponse(request, "browser_files.html", context)

    @app.post("/browser/files/{file_id}/download")
    async def browser_download_file(request: Request, file_id: int) -> Any:
        token = require_session(request)
        form = await valid_csrf(request, token)
        return_to = str(form.get("return_to", "/browser/files"))
        if not return_to.startswith("/browser") or return_to.startswith("//"):
            return_to = "/browser/files"
        separator = "&" if "?" in return_to else "?"
        collector_status = control.read_status()
        if collector_status.get("state") != "running":
            return RedirectResponse(
                f"{return_to}{separator}notice=collector-stopped", status_code=303
            )
        file, error = await browser_query(lambda url: browser.file(url, file_id))
        if error or file is None:
            return RedirectResponse(f"{return_to}{separator}notice=file-missing", status_code=303)
        if file.get("is_downloading_completed"):
            return RedirectResponse(
                f"{return_to}{separator}notice=already-downloaded", status_code=303
            )
        raw_file = file.get("raw_file") or {}
        remote = raw_file.get("remote") or {}
        remote_file_id = remote.get("id") if isinstance(remote.get("id"), str) else None
        request_id = control.request_download(file_id, remote_file_id)
        for _ in range(30):
            await asyncio.sleep(0.1)
            acknowledged = control.read_status()
            if int(acknowledged.get("last_request_id", 0)) >= request_id:
                notice = (
                    "download-failed"
                    if acknowledged.get("last_download_error")
                    else "download-requested"
                )
                return RedirectResponse(f"{return_to}{separator}notice={notice}", status_code=303)
        return RedirectResponse(f"{return_to}{separator}notice=download-pending", status_code=303)

    @app.get("/browser/cache", response_class=HTMLResponse)
    async def browser_cache(request: Request, path: str = "", page: int = 1) -> Any:
        require_session(request)
        settings = browser_settings()
        if settings is None or len(path) > 4096:
            raise HTTPException(status_code=404, detail="Cache directory not found")
        scanned = await asyncio.to_thread(
            _scan_cache_directory, settings.tdlib_data_dir, path, handler_registry
        )
        if scanned is None:
            raise HTTPException(status_code=404, detail="Cache directory not found")
        current_path, all_entries = scanned
        page = max(1, page)
        per_page = 200
        start = (page - 1) * per_page
        rows = all_entries[start : start + per_page]
        for row in rows:
            target = urlencode({"path": row["relative_path"]})
            if row["is_directory"]:
                row["url"] = f"/browser/cache?{target}"
            elif row["handler_id"]:
                archive_parameters = {
                    "path": row["relative_path"],
                    "handler": row["handler_id"],
                }
                row["url"] = f"/browser/cache/archive?{urlencode(archive_parameters)}"
            else:
                row["url"] = f"/browser/cache/content?{target}"
        breadcrumbs = [{"name": "files", "url": "/browser/cache"}]
        accumulated: list[str] = []
        for component in Path(current_path).parts if current_path else ():
            accumulated.append(component)
            breadcrumbs.append(
                {
                    "name": component,
                    "url": f"/browser/cache?{urlencode({'path': '/'.join(accumulated)})}",
                }
            )
        parent_path = Path(current_path).parent.as_posix() if current_path else None
        if parent_path == ".":
            parent_path = ""
        context = {
            "rows": rows,
            "current_path": current_path,
            "breadcrumbs": breadcrumbs,
            "active_handler_count": len(handler_registry.all()),
            "parent_url": (
                f"/browser/cache?{urlencode({'path': parent_path})}"
                if parent_path is not None
                else None
            ),
            "pagination": pagination(
                "/browser/cache",
                page,
                len(all_entries),
                per_page,
                {"path": current_path},
            ),
        }
        return templates.TemplateResponse(request, "browser_cache.html", context)

    @app.get("/browser/cache/handlers", response_class=HTMLResponse)
    async def browser_cache_handlers(request: Request) -> Any:
        require_session(request)
        settings = browser_settings()
        if settings is None:
            raise HTTPException(status_code=404, detail="Cache directory not found")
        rows = await asyncio.to_thread(
            _scan_handled_cache_files, settings.tdlib_data_dir, handler_registry
        )
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["handler_id"]] = counts.get(row["handler_id"], 0) + 1
            archive_parameters = {
                "path": row["relative_path"],
                "handler": row["handler_id"],
            }
            row["url"] = f"/browser/cache/archive?{urlencode(archive_parameters)}"
        configured_handlers = [
            {
                "handler_id": handler.handler_id,
                "label": handler.label,
                "description": handler.description,
                "detection": handler.detection,
                "configuration": handler.configuration(),
                "matched_count": counts.get(handler.handler_id, 0),
            }
            for handler in handler_registry.all()
        ]
        return templates.TemplateResponse(
            request,
            "browser_handlers.html",
            {"handlers": configured_handlers, "rows": rows},
        )

    @app.get("/browser/cache/archive", response_class=HTMLResponse)
    async def browser_cache_archive(
        request: Request,
        path: str = "",
        handler: str = "",
        inside: str = "",
        page: int = 1,
    ) -> Any:
        require_session(request)
        settings = browser_settings()
        selected_handler = handler_registry.get(handler)
        if (
            settings is None
            or selected_handler is None
            or not path
            or len(path) > 4096
            or len(inside) > 4096
        ):
            raise HTTPException(status_code=404, detail="Archive not found")
        archive = await asyncio.to_thread(_resolve_cache_file, settings.tdlib_data_dir, path)
        if archive is None or not await asyncio.to_thread(selected_handler.matches, archive):
            raise HTTPException(status_code=404, detail="Archive not found")
        try:
            all_entries = await asyncio.to_thread(selected_handler.list_directory, archive, inside)
        except FileHandlerError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        page = max(1, page)
        per_page = 200
        start = (page - 1) * per_page
        rows = all_entries[start : start + per_page]
        for row in rows:
            parameters = {
                "path": path,
                "handler": handler,
                "inside": row.member_path,
            }
            row.url = (
                f"/browser/cache/archive?{urlencode(parameters)}"
                if row.is_directory
                else f"/browser/cache/archive/content?{urlencode(parameters)}"
            )
        archive_url = f"/browser/cache/archive?{urlencode({'path': path, 'handler': handler})}"
        breadcrumbs = [{"name": archive.name, "url": archive_url}]
        accumulated: list[str] = []
        for component in Path(inside).parts if inside else ():
            accumulated.append(component)
            breadcrumb_parameters = {
                "path": path,
                "handler": handler,
                "inside": "/".join(accumulated),
            }
            breadcrumbs.append(
                {
                    "name": component,
                    "url": f"/browser/cache/archive?{urlencode(breadcrumb_parameters)}",
                }
            )
        parent_inside = Path(inside).parent.as_posix() if inside else None
        if parent_inside == ".":
            parent_inside = ""
        cache_parent = Path(path).parent.as_posix()
        if cache_parent == ".":
            cache_parent = ""
        parent_parameters = {"path": path, "handler": handler, "inside": parent_inside}
        context = {
            "archive_name": archive.name,
            "handler_label": selected_handler.label,
            "rows": rows,
            "breadcrumbs": breadcrumbs,
            "cache_parent_url": f"/browser/cache?{urlencode({'path': cache_parent})}",
            "parent_url": (
                f"/browser/cache/archive?{urlencode(parent_parameters)}"
                if parent_inside is not None
                else None
            ),
            "pagination": pagination(
                "/browser/cache/archive",
                page,
                len(all_entries),
                per_page,
                {"path": path, "handler": handler, "inside": inside},
            ),
        }
        return templates.TemplateResponse(request, "browser_archive.html", context)

    @app.get("/browser/cache/archive/content")
    async def browser_cache_archive_content(
        request: Request, path: str = "", handler: str = "", inside: str = ""
    ) -> Any:
        require_session(request)
        settings = browser_settings()
        selected_handler = handler_registry.get(handler)
        if (
            settings is None
            or selected_handler is None
            or not path
            or not inside
            or len(path) > 4096
            or len(inside) > 4096
        ):
            raise HTTPException(status_code=404, detail="Archived file not found")
        archive = await asyncio.to_thread(_resolve_cache_file, settings.tdlib_data_dir, path)
        if archive is None or not await asyncio.to_thread(selected_handler.matches, archive):
            raise HTTPException(status_code=404, detail="Archived file not found")
        try:
            member = await asyncio.to_thread(selected_handler.member, archive, inside)
        except FileHandlerError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        media_type, disposition = _stream_delivery(member.name)
        encoded_name = quote(member.name, safe="")
        return StreamingResponse(
            selected_handler.stream(archive, member),
            media_type=media_type,
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_name}",
                "Content-Length": str(member.size),
            },
        )

    @app.get("/browser/cache/content")
    async def browser_cache_content(request: Request, path: str = "") -> Any:
        require_session(request)
        settings = browser_settings()
        if settings is None or not path or len(path) > 4096:
            raise HTTPException(status_code=404, detail="Cached file not found")
        cached_file = await asyncio.to_thread(_resolve_cache_file, settings.tdlib_data_dir, path)
        if cached_file is None:
            raise HTTPException(status_code=404, detail="Cached file not found")
        return _cached_media_response(cached_file)

    @app.get("/browser/files/{file_id}/content")
    async def browser_file_content(request: Request, file_id: int) -> Any:
        require_session(request)
        settings = browser_settings()
        if settings is None:
            raise HTTPException(status_code=404, detail="File not found")
        file, error = await browser_query(lambda url: browser.file(url, file_id))
        if error or file is None or not file.get("is_downloading_completed"):
            raise HTTPException(status_code=404, detail="File not found")
        local_path = file.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise HTTPException(status_code=404, detail="File not found")
        cached_file = await asyncio.to_thread(
            _resolve_cached_file, settings.tdlib_data_dir, local_path
        )
        if cached_file is None:
            raise HTTPException(status_code=404, detail="File not found")
        return _cached_media_response(cached_file)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> Any:
        if password_store.configured:
            return RedirectResponse("/login", status_code=303)
        current_peer = peer(request)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"csrf_token": sessions.issue_csrf(current_peer, anonymous=True), "error": None},
        )

    @app.post("/setup", response_class=HTMLResponse)
    async def setup_password(request: Request) -> Any:
        if password_store.configured:
            return RedirectResponse("/login", status_code=303)
        current_peer = peer(request)
        form = await valid_csrf(request, current_peer, anonymous=True)
        password = str(form.get("password", ""))
        confirmation = str(form.get("confirmation", ""))
        error: str | None = None
        try:
            if password != confirmation:
                raise ValueError("Passwords do not match")
            password_store.set_password(password)
        except ValueError as problem:
            error = str(problem)
        if error:
            return templates.TemplateResponse(
                request,
                "setup.html",
                {
                    "csrf_token": sessions.issue_csrf(current_peer, anonymous=True),
                    "error": error,
                },
                status_code=422,
            )
        token = sessions.create()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            secure=cookie_secure,
            max_age=sessions.absolute_seconds,
        )
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "") -> Any:
        current_peer = peer(request)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": sessions.issue_csrf(current_peer, anonymous=True),
                "error": None,
                "next": _safe_return_path(next),
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Any:
        current_peer = peer(request)
        form = await valid_csrf(request, current_peer, anonymous=True)
        if not limiter.allowed(current_peer):
            error = "Too many failed attempts. Try again later."
        elif not password_store.verify(str(form.get("password", ""))):
            limiter.fail(current_peer)
            error = "Invalid password"
        else:
            limiter.success(current_peer)
            token = sessions.create()
            response = RedirectResponse(
                _safe_return_path(str(form.get("next", ""))), status_code=303
            )
            response.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                samesite="strict",
                secure=cookie_secure,
                max_age=sessions.absolute_seconds,
            )
            return response
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": sessions.issue_csrf(current_peer, anonymous=True),
                "error": error,
                "next": _safe_return_path(str(form.get("next", ""))),
            },
            status_code=429 if not limiter.allowed(current_peer) else 401,
        )

    @app.post("/logout")
    async def logout(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        sessions.destroy(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.post("/password", response_class=HTMLResponse)
    async def change_password(request: Request) -> Any:
        token = require_session(request)
        form = await valid_csrf(request, token)
        current = str(form.get("current_password", ""))
        new_password = str(form.get("new_password", ""))
        confirmation = str(form.get("confirmation", ""))
        if not password_store.verify(current):
            return render_dashboard(request, token, notice="Current password is invalid")
        if new_password != confirmation:
            return render_dashboard(request, token, notice="New passwords do not match")
        try:
            password_store.set_password(new_password)
        except ValueError as error:
            return render_dashboard(request, token, notice=str(error))
        sessions.invalidate_all()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> Any:
        token = require_session(request)
        form = await valid_csrf(request, token)
        current = store.load_draft()
        try:
            settings = _settings_from_form(form, current, secrets_directory)
            store.save_draft(settings)
        except ValidationError as error:
            return render_dashboard(request, token, errors=_field_errors(error))
        await telegram_session.stop()
        return RedirectResponse("/?notice=draft-saved", status_code=303)

    @app.post("/checks")
    async def run_checks(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        manifest = store.manifest()
        revision = manifest.get("draft_revision")
        settings = store.load_draft()
        if revision is None or settings is None:
            raise HTTPException(status_code=409, detail="Save a valid draft first")
        collector_status = control.read_status()
        telegram_ready = (
            telegram_session.ready
            and telegram_session.draft_hash == SettingsStore.settings_hash(settings)
        ) or (
            collector_status.get("state") == "running"
            and collector_status.get("applied_revision") == revision
            and not collector_status.get("error")
        )
        results = await runner.run(settings, telegram_ready=telegram_ready)
        store.save_checks(revision, results)
        return RedirectResponse("/?notice=checks-complete", status_code=303)

    @app.post("/telegram/start")
    async def start_telegram(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        settings = store.load_draft()
        if settings is None:
            raise HTTPException(status_code=409, detail="Save a valid draft first")
        if control.read_status().get("state") in {"starting", "running", "stopping"}:
            raise HTTPException(status_code=409, detail="Stop the collector before authorization")
        await telegram_session.start(settings, SettingsStore.settings_hash(settings))
        return RedirectResponse("/telegram", status_code=303)

    @app.get("/telegram", response_class=HTMLResponse)
    async def telegram_page(request: Request) -> Any:
        token = require_session(request)
        return render_telegram(request, token)

    @app.post("/telegram/respond")
    async def respond_telegram(request: Request) -> Any:
        token = require_session(request)
        form = await valid_csrf(request, token)
        correlation_id = str(form.get("correlation_id", ""))
        values = {
            name: str(form.get(name, ""))
            for name in ("code", "password", "email", "first_name", "last_name")
        }
        try:
            telegram_session.respond(correlation_id, values)
        except ValueError:
            return render_telegram(
                request,
                token,
                status_code=409,
                error="Форма устарела. Дождитесь нового поля ввода и повторите попытку.",
            )
        return RedirectResponse("/telegram", status_code=303)

    @app.post("/control/start")
    async def start_collector(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        try:
            revision = store.activate_draft()
        except SettingsStoreError as error:
            return render_dashboard(request, token, notice=str(error))
        control.request("start", revision)
        return RedirectResponse("/?notice=start-requested", status_code=303)

    @app.post("/control/stop")
    async def stop_collector(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        control.request("stop")
        return RedirectResponse("/?notice=stop-requested", status_code=303)

    @app.post("/control/restart")
    async def restart_collector(request: Request) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        revision = store.manifest().get("active_revision")
        control.request("restart", revision)
        return RedirectResponse("/?notice=restart-requested", status_code=303)

    @app.post("/revisions/{revision}/rollback")
    async def rollback(request: Request, revision: int) -> Any:
        token = require_session(request)
        await valid_csrf(request, token)
        try:
            store.rollback_to_draft(revision)
        except SettingsStoreError as error:
            raise HTTPException(status_code=404, detail="Revision not found") from error
        await telegram_session.stop()
        return RedirectResponse("/?notice=rollback-created", status_code=303)

    return app
