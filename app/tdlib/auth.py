import asyncio
import getpass
import logging
from typing import Any

from app.config import Settings
from app.tdlib.client import TdJsonClient

logger = logging.getLogger(__name__)


class AuthorizationController:
    def __init__(self, client: TdJsonClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()

    async def handle(self, update: dict[str, Any]) -> None:
        if update.get("@type") != "updateAuthorizationState":
            return
        state = update.get("authorization_state", {})
        state_type = state.get("@type", "unknown")
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
                "application_version": "0.1.0",
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

    async def _check_code(self, _: dict[str, Any]) -> None:
        code = await asyncio.to_thread(input, "Telegram authentication code: ")
        self.client.send({"@type": "checkAuthenticationCode", "code": code.strip()})

    async def _check_password(self, _: dict[str, Any]) -> None:
        password = await asyncio.to_thread(getpass.getpass, "Telegram 2FA password: ")
        self.client.send({"@type": "checkAuthenticationPassword", "password": password})

    async def _set_email_address(self, _: dict[str, Any]) -> None:
        email = await asyncio.to_thread(input, "Telegram authentication email: ")
        self.client.send({"@type": "setAuthenticationEmailAddress", "email_address": email.strip()})

    async def _check_email_code(self, _: dict[str, Any]) -> None:
        code = await asyncio.to_thread(input, "Telegram email authentication code: ")
        self.client.send(
            {
                "@type": "checkAuthenticationEmailCode",
                "code": {"@type": "emailAddressAuthenticationCode", "code": code.strip()},
            }
        )

    async def _register_user(self, state: dict[str, Any]) -> None:
        terms = state.get("terms_of_service")
        if terms:
            print(
                terms.get("text", {}).get("text", "Telegram Terms of Service acceptance required.")
            )
        first_name = await asyncio.to_thread(input, "Telegram first name: ")
        last_name = await asyncio.to_thread(input, "Telegram last name (optional): ")
        self.client.send(
            {
                "@type": "registerUser",
                "first_name": first_name.strip(),
                "last_name": last_name.strip(),
                "disable_notification": False,
            }
        )

    async def _other_device_confirmation(self, state: dict[str, Any]) -> None:
        logger.warning(
            "confirm authorization from another device",
            extra={"confirmation_link": state.get("link")},
        )

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
