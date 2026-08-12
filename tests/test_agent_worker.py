from fastapi.testclient import TestClient

from sagasmith_service.agent_worker import create_worker_app


class FakeLoop:
    def __init__(self) -> None:
        self.calls = []
        self._last_usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    async def _connect_mcp(self) -> None:
        return None

    async def close_mcp(self) -> None:
        return None

    async def process_direct(self, **arguments):
        self.calls.append(arguments)
        return type("Response", (), {"content": "ok"})()


def test_hosted_worker_injects_authenticated_principal_as_sender() -> None:
    loop = FakeLoop()
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": "campaign:user:conversation",
                "principal_id": "user:account-id",
            },
        )
    assert response.status_code == 200
    assert loop.calls[0]["channel"] == "user"
    assert loop.calls[0]["sender_id"] == "account-id"
    assert response.json()["usage"]["total_tokens"] == 5


def test_hosted_worker_rejects_untrusted_principal_shape() -> None:
    loop = FakeLoop()
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": "conversation",
                "principal_id": "service:spoofed",
            },
        )
    assert response.status_code == 422
