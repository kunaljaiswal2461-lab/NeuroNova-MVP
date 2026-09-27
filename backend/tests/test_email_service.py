from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings
from app.services import email_service


def _settings(port: int) -> Settings:
    return Settings(
        api_key="test-api-key",
        database_url="postgresql+asyncpg://x:x@localhost/x",
        smtp_host="smtp.gmail.com",
        smtp_port=port,
        smtp_username="mailer@example.com",
        smtp_password="test-app-password",
        smtp_from_email="mailer@example.com",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("port", "expected_use_tls", "expected_start_tls"),
    [(587, False, True), (465, True, False)],
)
async def test_send_uses_the_correct_encrypted_smtp_transport(
    port: int, expected_use_tls: bool, expected_start_tls: bool
) -> None:
    send = AsyncMock()

    with patch("aiosmtplib.send", send):
        await email_service._send(_settings(port), "recipient@example.com", "Subject", "<p>Body</p>")

    assert send.await_args.kwargs["use_tls"] is expected_use_tls
    assert send.await_args.kwargs["start_tls"] is expected_start_tls
