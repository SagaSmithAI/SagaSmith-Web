from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import httpx
from e2e.mock_openai import Handler


def completion(content: str) -> dict:
    return request_completion(
        {
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ]
        }
    )


def request_completion(payload: dict) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            json=payload,
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


def test_modern_room_turn_ignores_tool_calls_from_the_previous_turn() -> None:
    character_tool = "mcp_sagasmith-dnd-mcp_character_query"
    submit_tool = "submit_room_turn"
    tools = [
        {
            "type": "function",
            "function": {
                "name": submit_tool,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string", "const": "run-current"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": character_tool,
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    messages = [
        {"role": "user", "content": "Query the current character list."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-previous",
                    "type": "function",
                    "function": {"name": character_tool, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-previous", "name": character_tool, "content": "{}"},
        {"role": "assistant", "content": "Previous turn completed."},
        {
            "role": "user",
            "content": "Inspect the scene as a player while the hosted DM responds.",
        },
    ]

    first = request_completion({"messages": messages, "tools": tools})
    first_call = first["choices"][0]["message"]["tool_calls"][0]
    assert first_call["function"]["name"] == character_tool

    messages.extend(
        [
            first["choices"][0]["message"],
            {
                "role": "tool",
                "tool_call_id": first_call["id"],
                "name": character_tool,
                "content": "{}",
            },
        ]
    )
    second = request_completion({"messages": messages, "tools": tools})
    assert "tool_calls" in second["choices"][0]["message"], second
    second_call = second["choices"][0]["message"]["tool_calls"][0]
    assert second_call["function"]["name"] == submit_tool
    assert json.loads(second_call["function"]["arguments"])["run_id"] == "run-current"
