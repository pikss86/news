import asyncio

import pytest

from app.tdlib.auth import AuthorizationBroker, AuthorizationController, TerminalAuthorizationInput
from tests.factories import settings


async def test_broker_is_one_shot_and_rejects_stale_response() -> None:
    broker = AuthorizationBroker(timeout_seconds=1)
    pending = asyncio.create_task(broker.request("code", {}))
    await asyncio.sleep(0)
    challenge = broker.current()
    assert challenge is not None
    with pytest.raises(ValueError):
        broker.respond("wrong", {"code": "123"})
    broker.respond(challenge["correlation_id"], {"code": "123"})
    assert await pending == {"code": "123"}
    with pytest.raises(ValueError):
        broker.respond(challenge["correlation_id"], {"code": "123"})


async def test_terminal_authorization_adapter_keeps_interactive_flow(monkeypatch) -> None:
    answers = iter([" 12345 ", "two-factor", " mail@example.test "])

    async def fake_to_thread(function, *args):  # type: ignore[no-untyped-def]
        return next(answers)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    adapter = TerminalAuthorizationInput()
    assert await adapter.request("code", {}) == {"code": "12345"}
    assert await adapter.request("password", {}) == {"password": "two-factor"}
    assert await adapter.request("email", {}) == {"email": "mail@example.test"}


async def test_all_browser_authorization_states_send_expected_requests() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request):  # type: ignore[no-untyped-def]
            self.requests.append(request)

    class Input:
        async def request(self, kind, state):  # type: ignore[no-untyped-def]
            return {
                "code": {"code": "code"},
                "password": {"password": "password"},
                "email": {"email": "mail@example.test"},
                "email_code": {"code": "email-code"},
                "registration": {"first_name": "A", "last_name": "B"},
            }[kind]

        async def notify(self, kind, state):  # type: ignore[no-untyped-def]
            return None

    client = Client()
    controller = AuthorizationController(client, settings(), Input())  # type: ignore[arg-type]
    states = [
        "authorizationStateWaitCode",
        "authorizationStateWaitPassword",
        "authorizationStateWaitEmailAddress",
        "authorizationStateWaitEmailCode",
        "authorizationStateWaitRegistration",
        "authorizationStateWaitOtherDeviceConfirmation",
        "authorizationStateReady",
    ]
    for state in states:
        await controller.handle(
            {"@type": "updateAuthorizationState", "authorization_state": {"@type": state}}
        )
    request_types = {request["@type"] for request in client.requests}
    assert {
        "checkAuthenticationCode",
        "checkAuthenticationPassword",
        "setAuthenticationEmailAddress",
        "checkAuthenticationEmailCode",
        "registerUser",
        "loadChats",
    }.issubset(request_types)
    assert controller.ready.is_set()


async def test_rejected_code_can_be_requested_again() -> None:
    class Client:
        def __init__(self) -> None:
            self.requests: list[dict] = []

        def send(self, request):  # type: ignore[no-untyped-def]
            self.requests.append(request)

    broker = AuthorizationBroker(timeout_seconds=1)
    client = Client()
    controller = AuthorizationController(client, settings(), broker)  # type: ignore[arg-type]
    controller.state_type = "authorizationStateWaitCode"
    controller.last_state = {"@type": "authorizationStateWaitCode"}

    retry = asyncio.create_task(controller.retry_challenge("code"))
    await asyncio.sleep(0)
    challenge = broker.current()
    assert challenge is not None
    assert challenge["kind"] == "code"
    broker.respond(challenge["correlation_id"], {"code": "654321"})

    assert await retry is True
    assert client.requests[-1] == {
        "@type": "checkAuthenticationCode",
        "code": "654321",
        "@extra": {"authorization_action": "code"},
    }
