from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from app.config import Settings
from app.settings.redaction import redact
from app.tdlib.auth import AuthorizationBroker, AuthorizationController
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)

AUTHORIZATION_ERRORS = {
    "code": "Код неверный или устарел. Введите актуальный код из Telegram.",
    "password": "Облачный пароль неверный. Попробуйте ещё раз.",
    "email": "Telegram не принял этот email. Проверьте адрес и попробуйте ещё раз.",
    "email_code": "Код из письма неверный или устарел. Введите актуальный код.",
    "registration": "Telegram не принял регистрационные данные. Проверьте их и попробуйте ещё раз.",
}


class TelegramAuthorizationSession:
    def __init__(self) -> None:
        self.broker = AuthorizationBroker()
        self.client: TdJsonClient | None = None
        self.controller: AuthorizationController | None = None
        self.task: asyncio.Task[None] | None = None
        self.draft_hash: str | None = None
        self.last_error: dict[str, Any] | None = None

    async def start(self, settings: Settings, draft_hash: str) -> None:
        await self.stop()
        self.broker = AuthorizationBroker()
        self.client = TdJsonClient(settings.tdlib_library_path)
        self.client.execute(
            {
                "@type": "setLogVerbosityLevel",
                "new_verbosity_level": settings.tdlib_log_verbosity,
            }
        )
        self.controller = AuthorizationController(self.client, settings, self.broker)
        self.draft_hash = draft_hash
        self.last_error = None
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        assert self.client is not None and self.controller is not None
        try:
            while not self.controller.closed.is_set():
                update = await self.client.receive(wait_seconds=1.0)
                if update is None:
                    continue
                if update.get("@type") == "error":
                    extra = update.get("@extra")
                    action = extra.get("authorization_action") if isinstance(extra, dict) else None
                    message = AUTHORIZATION_ERRORS.get(
                        action,
                        "Telegram отклонил вход. Проверьте сохранённые настройки "
                        "и запросите новый код.",
                    )
                    self.last_error = redact({"code": update.get("code"), "message": message})
                    logger.warning(
                        "Telegram authorization request rejected",
                        extra={
                            "authorization_action": action,
                            "tdlib_error_code": update.get("code"),
                        },
                    )
                    if action and await self.controller.retry_challenge(action):
                        continue
                    return
                await self.controller.handle(update)
                if self.controller.ready.is_set():
                    if self.broker.challenge:
                        self.broker.clear_notification(self.broker.challenge.correlation_id)
                    logger.info("Telegram authorization completed; releasing TDLib client")
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram authorization session failed")
            self.last_error = {
                "message": "Сеанс входа прерван внутренней ошибкой. Запросите новый код."
            }
        finally:
            if self.client is not None:
                self.client.close()

    @property
    def ready(self) -> bool:
        return bool(self.controller and self.controller.ready.is_set())

    def state(self) -> dict[str, Any]:
        return {
            "running": bool(self.task and not self.task.done()),
            "ready": self.ready,
            "state": self.controller.state_type if self.controller else "not_started",
            "challenge": self.broker.current(),
            "error": self.last_error,
            "draft_hash": self.draft_hash,
        }

    def respond(self, correlation_id: str, values: dict[str, str]) -> None:
        self.last_error = None
        self.broker.respond(correlation_id, values)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.client is not None:
            self.client.close()
        self.task = None
        self.client = None
        self.controller = None
        self.draft_hash = None
