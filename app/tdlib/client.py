import asyncio
import ctypes
import ctypes.util
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TdJsonError(RuntimeError):
    pass


class TdJsonClient:
    """Minimal ctypes binding for the stable TDLib JSON C interface."""

    def __init__(self, library_path: Path | None = None) -> None:
        path = str(library_path) if library_path else ctypes.util.find_library("tdjson")
        if not path:
            for candidate in ("/usr/local/lib/libtdjson.so", "/usr/lib/libtdjson.so"):
                if Path(candidate).exists():
                    path = candidate
                    break
        if not path:
            raise TdJsonError(
                "libtdjson was not found; set TDLIB_LIBRARY_PATH or use the Docker image"
            )

        self._library = ctypes.CDLL(path)
        self._configure_signatures()
        self._client = self._library.td_json_client_create()
        if not self._client:
            raise TdJsonError("td_json_client_create returned a null pointer")
        self._closed = False

    def _configure_signatures(self) -> None:
        self._library.td_json_client_create.restype = ctypes.c_void_p
        self._library.td_json_client_send.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_receive.argtypes = [ctypes.c_void_p, ctypes.c_double]
        self._library.td_json_client_receive.restype = ctypes.c_char_p
        self._library.td_json_client_execute.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._library.td_json_client_execute.restype = ctypes.c_char_p
        self._library.td_json_client_destroy.argtypes = [ctypes.c_void_p]

    def send(self, request: dict[str, Any]) -> None:
        if self._closed:
            raise TdJsonError("cannot send through a closed TDLib client")
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        self._library.td_json_client_send(self._client, payload)

    def execute(self, request: dict[str, Any]) -> dict[str, Any] | None:
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        result = self._library.td_json_client_execute(self._client, payload)
        return json.loads(result.decode()) if result else None

    def _receive_sync(self, wait_seconds: float) -> dict[str, Any] | None:
        result = self._library.td_json_client_receive(self._client, wait_seconds)
        return json.loads(result.decode()) if result else None

    async def receive(self, wait_seconds: float = 1.0) -> dict[str, Any] | None:
        if self._closed:
            return None
        return await asyncio.to_thread(self._receive_sync, wait_seconds)

    def close(self) -> None:
        if self._closed:
            return
        self._library.td_json_client_destroy(self._client)
        self._closed = True
        logger.info("TDLib client closed")
