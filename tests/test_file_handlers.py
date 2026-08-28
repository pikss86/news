import zipfile

import pytest

from app.admin.file_handlers import FileHandlerError, FileHandlerRegistry, ZipStreamHandler


async def test_zip_handler_streams_member_and_enforces_policy(tmp_path) -> None:
    archive = tmp_path / "archive-without-extension"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("folder/document.pdf", b"%PDF-streamed")
        bundle.writestr("../unsafe.txt", b"unsafe")

    handler = ZipStreamHandler()
    registry = FileHandlerRegistry([handler])
    assert registry.matching(archive) is handler
    assert [item.name for item in handler.list_directory(archive, "")] == ["folder"]
    assert [item.name for item in handler.list_directory(archive, "folder")] == ["document.pdf"]

    member = handler.member(archive, "folder/document.pdf")
    streamed = b"".join([chunk async for chunk in handler.stream(archive, member)])
    assert streamed == b"%PDF-streamed"

    with pytest.raises(FileHandlerError, match="Некорректный путь"):
        handler.member(archive, "../unsafe.txt")
    with pytest.raises(FileHandlerError, match="Некорректный путь"):
        handler.member(archive, "/folder/document.pdf")
    with pytest.raises(FileHandlerError, match="безопасный размер"):
        ZipStreamHandler(max_member_size=3).member(archive, "folder/document.pdf")
