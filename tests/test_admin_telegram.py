from app.admin.telegram import TelegramAuthorizationSession
from tests.factories import settings


async def test_successful_authorization_releases_tdlib_client(monkeypatch, tmp_path) -> None:
    class Client:
        instances = []

        def __init__(self, library_path) -> None:  # type: ignore[no-untyped-def]
            self.closed = False
            self.requests = []
            self.updates = [
                {
                    "@type": "updateAuthorizationState",
                    "authorization_state": {"@type": "authorizationStateWaitTdlibParameters"},
                },
                {
                    "@type": "updateAuthorizationState",
                    "authorization_state": {"@type": "authorizationStateReady"},
                },
            ]
            self.instances.append(self)

        def execute(self, request):  # type: ignore[no-untyped-def]
            return None

        def send(self, request):  # type: ignore[no-untyped-def]
            self.requests.append(request)

        async def receive(self, wait_seconds=1.0):  # type: ignore[no-untyped-def]
            return self.updates.pop(0)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("app.admin.telegram.TdJsonClient", Client)
    session = TelegramAuthorizationSession()
    await session.start(settings(tdlib_data_dir=tmp_path / "tdlib"), "draft-hash")
    assert session.task is not None
    await session.task

    assert session.ready is True
    assert session.state()["running"] is False
    assert session.state()["draft_hash"] == "draft-hash"
    assert Client.instances[0].closed is True

    await session.stop()
