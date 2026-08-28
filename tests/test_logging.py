import io
import logging

from app.logging import RedactionFilter


def test_logging_filter_redacts_secrets_in_messages_and_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactionFilter())
    logger = logging.Logger("redaction-test")
    logger.addHandler(handler)
    logger.error(
        "failed for +12345678901 at postgresql+asyncpg://news:database-secret@postgres/news",
        extra={"password": "two-factor", "code": "12345"},
    )
    output = stream.getvalue()
    assert "+12345678901" not in output
    assert "database-secret" not in output
    assert "two-factor" not in output
    assert "12345" not in output
