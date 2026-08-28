from __future__ import annotations

import asyncio
import zipfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


class FileHandlerError(ValueError):
    """A cached file can't be processed safely by the selected handler."""


@dataclass
class ArchiveItem:
    name: str
    member_path: str
    is_directory: bool
    size: int | None
    url: str = ""


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    stored_name: str
    size: int


class StreamingFileHandler(Protocol):
    handler_id: str
    label: str

    def matches(self, path: Path) -> bool: ...

    def list_directory(self, path: Path, inside: str) -> list[ArchiveItem]: ...

    def member(self, path: Path, member_path: str) -> ArchiveMember: ...

    def stream(self, path: Path, member: ArchiveMember) -> AsyncIterator[bytes]: ...


def _safe_archive_path(value: str, *, allow_empty: bool = False) -> str | None:
    raw = value.replace("\\", "/")
    if "\x00" in raw or raw.startswith("/"):
        return None
    normalized = raw.strip("/")
    if not normalized:
        return "" if allow_empty else None
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or (path.parts and path.parts[0].endswith(":"))
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None
    return path.as_posix()


class ZipStreamHandler:
    handler_id = "zip"
    label = "ZIP-архив"

    def __init__(
        self,
        *,
        max_member_size: int = 512 * 1024 * 1024,
        max_compression_ratio: float = 1000.0,
        chunk_size: int = 256 * 1024,
        max_entries: int = 10_000,
    ) -> None:
        self.max_member_size = max_member_size
        self.max_compression_ratio = max_compression_ratio
        self.chunk_size = chunk_size
        self.max_entries = max_entries

    def matches(self, path: Path) -> bool:
        if path.suffix.casefold() == ".zip":
            return zipfile.is_zipfile(path)
        try:
            with path.open("rb") as source:
                signature = source.read(4)
        except OSError:
            return False
        return signature.startswith(
            (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
        ) and zipfile.is_zipfile(path)

    @staticmethod
    def _safe_infos(archive: zipfile.ZipFile) -> Iterable[tuple[str, zipfile.ZipInfo]]:
        for info in archive.infolist():
            normalized = _safe_archive_path(info.filename)
            if normalized is not None:
                yield normalized, info

    def list_directory(self, path: Path, inside: str) -> list[ArchiveItem]:
        prefix = _safe_archive_path(inside, allow_empty=True)
        if prefix is None:
            raise FileHandlerError("Некорректный путь внутри архива.")
        prefix_parts = PurePosixPath(prefix).parts if prefix else ()
        entries: dict[str, ArchiveItem] = {}
        try:
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist()) > self.max_entries:
                    raise FileHandlerError("В ZIP-архиве слишком много записей.")
                for normalized, info in self._safe_infos(archive):
                    parts = PurePosixPath(normalized).parts
                    if parts[: len(prefix_parts)] != prefix_parts:
                        continue
                    remaining = parts[len(prefix_parts) :]
                    if not remaining:
                        continue
                    name = remaining[0]
                    member_path = "/".join((*prefix_parts, name))
                    is_directory = len(remaining) > 1 or info.is_dir()
                    existing = entries.get(name)
                    if existing is None or (is_directory and not existing.is_directory):
                        entries[name] = ArchiveItem(
                            name=name,
                            member_path=member_path,
                            is_directory=is_directory,
                            size=None if is_directory else info.file_size,
                        )
        except (OSError, zipfile.BadZipFile) as error:
            raise FileHandlerError("ZIP-архив повреждён или недоступен.") from error
        return sorted(
            entries.values(), key=lambda item: (not item.is_directory, item.name.casefold())
        )

    def member(self, path: Path, member_path: str) -> ArchiveMember:
        normalized_member = _safe_archive_path(member_path)
        if normalized_member is None:
            raise FileHandlerError("Некорректный путь внутри архива.")
        try:
            with zipfile.ZipFile(path) as archive:
                if len(archive.infolist()) > self.max_entries:
                    raise FileHandlerError("В ZIP-архиве слишком много записей.")
                selected: zipfile.ZipInfo | None = None
                for normalized, info in self._safe_infos(archive):
                    if normalized == normalized_member and not info.is_dir():
                        selected = info
                        break
                if selected is None:
                    raise FileHandlerError("Файл внутри архива не найден.")
                if selected.flag_bits & 0x1:
                    raise FileHandlerError("Зашифрованные ZIP-файлы не поддерживаются.")
                if selected.file_size > self.max_member_size:
                    raise FileHandlerError("Файл внутри архива превышает безопасный размер.")
                ratio = selected.file_size / max(selected.compress_size, 1)
                if ratio > self.max_compression_ratio:
                    raise FileHandlerError("Подозрительно высокая степень сжатия ZIP-файла.")
                return ArchiveMember(
                    name=PurePosixPath(normalized_member).name,
                    stored_name=selected.filename,
                    size=selected.file_size,
                )
        except (OSError, zipfile.BadZipFile) as error:
            raise FileHandlerError("ZIP-архив повреждён или недоступен.") from error

    async def stream(self, path: Path, member: ArchiveMember) -> AsyncIterator[bytes]:
        with zipfile.ZipFile(path) as archive, archive.open(member.stored_name) as source:
            while True:
                chunk = await asyncio.to_thread(source.read, self.chunk_size)
                if not chunk:
                    break
                yield chunk


class FileHandlerRegistry:
    def __init__(self, handlers: Iterable[StreamingFileHandler] = ()) -> None:
        configured = list(handlers)
        self._handlers = {handler.handler_id: handler for handler in configured}
        if len(self._handlers) != len(configured):
            raise ValueError("file handler identifiers must be unique")

    @classmethod
    def defaults(cls) -> FileHandlerRegistry:
        return cls([ZipStreamHandler()])

    def matching(self, path: Path) -> StreamingFileHandler | None:
        return next((handler for handler in self._handlers.values() if handler.matches(path)), None)

    def get(self, handler_id: str) -> StreamingFileHandler | None:
        return self._handlers.get(handler_id)
