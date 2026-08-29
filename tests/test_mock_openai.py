from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
from e2e.mock_openai import Handler


def test_modern_module_task_returns_strict_json_without_legacy_auth_prompt() -> None:
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
                        "content": (
                            "Follow the installed sagasmith-modulegen Skill.\n"
                            "Task: Design a playable outline grounded in evidence."
                        ),
                    }
                ]
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    decision = json.loads(content)
    assert decision["outline"]["premise"] == "A lantern gate is failing."
    assert decision["summary"] == "Deterministic Module Studio outline."
