from __future__ import annotations

import asyncio
import getpass
import logging
import secrets
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.config import Settings
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


class AuthorizationInput(Protocol):
    async def request(self, kind: str, state: dict[str, Any]) -> dict[str, str]: ...

    async def notify(self, kind: str, state: dict[str, Any]) -> None: ...


class TerminalAuthorizationInput:
    async def request(self, kind: str, state: dict[str, Any]) -> dict[str, str]:
        if kind == "code":
            value = await asyncio.to_thread(input, "Telegram authentication code: ")
            return {"code": value.strip()}
        if kind == "password":
            value = await asyncio.to_thread(getpass.getpass, "Telegram 2FA password: ")
            return {"password": value}
        if kind == "email":
            value = await asyncio.to_thread(input, "Telegram authentication email: ")
            return {"email": value.strip()}
        if kind == "email_code":
            value = await asyncio.to_thread(input, "Telegram email authentication code: ")
            return {"code": value.strip()}
        if kind == "registration":
            terms = state.get("terms_of_service")
            if terms:
                print(
                    terms.get("text", {}).get(
                        "text", "Telegram Terms of Service acceptance required."
                    )
                )
            first_name = await asyncio.to_thread(input, "Telegram first name: ")
            last_name = await asyncio.to_thread(input, "Telegram last name (optional): ")
            return {"first_name": first_name.strip(), "last_name": last_name.strip()}
        raise ValueError(f"unsupported authorization input kind: {kind}")

    async def notify(self, kind: str, state: dict[str, Any]) -> None:
        if kind == "other_device":
            logger.warning(
                "confirm authorization from another device",
                extra={"confirmation_link": state.get("link")},
            )


class NonInteractiveAuthorizationInput:
    async def request(self, kind: str, state: dict[str, Any]) -> dict[str, str]:
        raise RuntimeError(
            f"Telegram authorization state {kind} requires the VPN administration page"
        )

    async def notify(self, kind: str, state: dict[str, Any]) -> None:
        logger.warning("Telegram authorization requires confirmation in the administration page")


@dataclass(frozen=True)
class AuthorizationChallenge:
    correlation_id: str
    kind: str
    presentation: dict[str, Any]


class AuthorizationBroker:
    """One outstanding, one-shot authorization challenge for the admin UI."""

    def __init__(self, timeout_seconds: float = 600.0) -> None:
        self.timeout_seconds = timeout_seconds
        self.challenge: AuthorizationChallenge | None = None
        self._future: asyncio.Future[dict[str, str]] | None = None

    @staticmethod
    def _presentation(kind: str, state: dict[str, Any]) -> dict[str, Any]:
        if kind == "other_device":
            return {"link": state.get("link")}
        if kind == "registration":
            return {"terms": state.get("terms_of_service", {}).get("text", {}).get("text")}
        return {}

    async def request(self, kind: str, state: dict[str, Any]) -> dict[str, str]:
        if self._future is not None and not self._future.done():
            raise RuntimeError("an authorization challenge is already pending")
        loop = asyncio.get_running_loop()
        correlation_id = secrets.token_urlsafe(24)
        self.challenge = AuthorizationChallenge(
            correlation_id, kind, self._presentation(kind, state)
        )
        self._future = loop.create_future()
        try:
            return await asyncio.wait_for(self._future, timeout=self.timeout_seconds)
        finally:
            self.challenge = None
            self._future = None

    async def notify(self, kind: str, state: dict[str, Any]) -> None:
        self.challenge = AuthorizationChallenge(
            secrets.token_urlsafe(24), kind, self._presentation(kind, state)
        )

    def current(self) -> dict[str, Any] | None:
        return asdict(self.challenge) if self.challenge else None

    def respond(self, correlation_id: str, values: dict[str, str]) -> None:
        if (
            self.challenge is None
            or self._future is None
            or self._future.done()
            or not secrets.compare_digest(self.challenge.correlation_id, correlation_id)
        ):
            raise ValueError("authorization challenge is stale or invalid")
        required = {
            "code": {"code"},
            "password": {"password"},
            "email": {"email"},
            "email_code": {"code"},
            "registration": {"first_name", "last_name"},
        }.get(self.challenge.kind)
        if required is None or not required.issubset(values):
            raise ValueError("authorization response does not match the current challenge")
        self._future.set_result({name: values[name] for name in required})

    def clear_notification(self, correlation_id: str) -> None:
        if self.challenge and secrets.compare_digest(self.challenge.correlation_id, correlation_id):
            self.challenge = None


class AuthorizationController:
    def __init__(
        self,
        client: TdJsonClient,
        settings: Settings,
        authorization_input: AuthorizationInput | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.authorization_input = authorization_input or TerminalAuthorizationInput()
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.state_type = "unknown"
        self.last_state: dict[str, Any] = {}

    async def handle(self, update: dict[str, Any]) -> None:
        if update.get("@type") != "updateAuthorizationState":
            return
        state = update.get("authorization_state", {})
        state_type = state.get("@type", "unknown")
        self.state_type = state_type
        self.last_state = state
        logger.info("authorization state changed", extra={"authorization_state": state_type})

        handlers = {
            "authorizationStateWaitTdlibParameters": self._set_parameters,
            "authorizationStateWaitPhoneNumber": self._set_phone_number,
            "authorizationStateWaitCode": self._check_code,
            "authorizationStateWaitPassword": self._check_password,
            "authorizationStateWaitEmailAddress": self._set_email_address,
            "authorizationStateWaitEmailCode": self._check_email_code,
            "authorizationStateWaitRegistration": self._register_user,
            "authorizationStateWaitDatabaseEncryptionKey": self._check_database_key,
            "authorizationStateWaitOtherDeviceConfirmation": self._other_device_confirmation,
            "authorizationStateReady": self._authorized,
            "authorizationStateLoggingOut": self._logging_out,
            "authorizationStateClosing": self._closing,
            "authorizationStateClosed": self._closed,
        }
        handler = handlers.get(state_type)
        if handler is not None:
            await handler(state)
        else:
            logger.warning("unsupported authorization state", extra={"state": state_type})

    async def _set_parameters(self, _: dict[str, Any]) -> None:
        data_dir = self.settings.tdlib_data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.client.send(
            {
                "@type": "setTdlibParameters",
                "database_directory": str(data_dir / "database"),
                "files_directory": str(data_dir / "files"),
                "use_file_database": True,
                "use_chat_info_database": True,
                "use_message_database": True,
                "use_secret_chats": False,
                "api_id": self.settings.telegram_api_id,
                "api_hash": self.settings.telegram_api_hash.get_secret_value(),
                "system_language_code": "en",
                "device_model": "news-collector",
                "application_version": "0.2.0",
                "enable_storage_optimizer": True,
                "ignore_file_names": False,
            }
        )

    async def _check_database_key(self, _: dict[str, Any]) -> None:
        self.client.send(
            {
                "@type": "checkDatabaseEncryptionKey",
                "encryption_key": self.settings.telegram_database_encryption_key.get_secret_value(),
            }
        )

    async def _set_phone_number(self, _: dict[str, Any]) -> None:
        self.client.send(
            {
                "@type": "setAuthenticationPhoneNumber",
                "phone_number": self.settings.telegram_phone_number,
            }
        )

    async def _check_code(self, state: dict[str, Any]) -> None:
        response = await self.authorization_input.request("code", state)
        self.client.send(
            {
                "@type": "checkAuthenticationCode",
                "code": response["code"],
                "@extra": {"authorization_action": "code"},
            }
        )

    async def _check_password(self, state: dict[str, Any]) -> None:
        response = await self.authorization_input.request("password", state)
        self.client.send(
            {
                "@type": "checkAuthenticationPassword",
                "password": response["password"],
                "@extra": {"authorization_action": "password"},
            }
        )

    async def _set_email_address(self, state: dict[str, Any]) -> None:
        response = await self.authorization_input.request("email", state)
        self.client.send(
            {
                "@type": "setAuthenticationEmailAddress",
                "email_address": response["email"],
                "@extra": {"authorization_action": "email"},
            }
        )

    async def _check_email_code(self, state: dict[str, Any]) -> None:
        response = await self.authorization_input.request("email_code", state)
        self.client.send(
            {
                "@type": "checkAuthenticationEmailCode",
                "code": {
                    "@type": "emailAddressAuthenticationCode",
                    "code": response["code"],
                },
                "@extra": {"authorization_action": "email_code"},
            }
        )

    async def _register_user(self, state: dict[str, Any]) -> None:
        response = await self.authorization_input.request("registration", state)
        self.client.send(
            {
                "@type": "registerUser",
                "first_name": response["first_name"],
                "last_name": response["last_name"],
                "disable_notification": False,
                "@extra": {"authorization_action": "registration"},
            }
        )

    async def retry_challenge(self, kind: str) -> bool:
        """Present the same input again after TDLib rejects a submitted value."""
        handlers = {
            "code": ("authorizationStateWaitCode", self._check_code),
            "password": ("authorizationStateWaitPassword", self._check_password),
            "email": ("authorizationStateWaitEmailAddress", self._set_email_address),
            "email_code": ("authorizationStateWaitEmailCode", self._check_email_code),
            "registration": ("authorizationStateWaitRegistration", self._register_user),
        }
        retry = handlers.get(kind)
        if retry is None or self.state_type != retry[0]:
            return False
        await retry[1](self.last_state)
        return True

    async def _other_device_confirmation(self, state: dict[str, Any]) -> None:
        await self.authorization_input.notify("other_device", state)

    async def _authorized(self, _: dict[str, Any]) -> None:
        if not self.ready.is_set():
            logger.info("TDLib connected")
            self.ready.set()
            self.client.send(
                {"@type": "loadChats", "chat_list": {"@type": "chatListMain"}, "limit": 1000}
            )
            self.client.send(
                {
                    "@type": "loadChats",
                    "chat_list": {"@type": "chatListArchive"},
                    "limit": 1000,
                }
            )

    async def _logging_out(self, _: dict[str, Any]) -> None:
        self.ready.clear()
        logger.info("TDLib logging out")

    async def _closing(self, _: dict[str, Any]) -> None:
        self.ready.clear()
        logger.info("TDLib closing")

    async def _closed(self, _: dict[str, Any]) -> None:
        self.ready.clear()
        self.closed.set()
        logger.info("TDLib closed")
