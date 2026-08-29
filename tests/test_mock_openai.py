from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
from e2e.mock_openai import Handler


def completion(content: str) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ]
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    response.raise_for_status()
    return response.json()


def test_modern_module_task_returns_strict_json_without_legacy_auth_prompt() -> None:
    payload = completion(
        "Follow the installed sagasmith-modulegen Skill.\n"
        "Task: Design a playable outline grounded in evidence."
    )
    decision = json.loads(payload["choices"][0]["message"]["content"])
    assert decision["outline"]["premise"] == "A lantern gate is failing."
    assert decision["summary"] == "Deterministic Module Studio outline."


def test_modern_community_review_returns_strict_json_without_legacy_auth_prompt() -> None:
    payload = completion(
        "Review this SagaSmith community artifact release for semantic completeness."
    )
    decision = json.loads(payload["choices"][0]["message"]["content"])
    assert decision == {
        "approved": True,
        "summary": "Synthetic original fixture is publishable.",
        "findings": [],
    }
