from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/v1/models":
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "sagasmith-e2e", "object": "model"}],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages") or []
        context = next(
            (
                str(message.get("content", ""))
                for message in messages
                if isinstance(message, dict)
                and "[SagaSmith Service authenticated context]" in str(message.get("content", ""))
            ),
            "",
        )
        authenticated = bool(context)
        campaign_match = re.search(r"^campaign_id=(.+)$", context, re.MULTILINE)
        principal_match = re.search(r"^principal_id=(.+)$", context, re.MULTILINE)
        module_result = None
        if "Task: Design a playable outline" in context:
            module_result = {
                "outline": {
                    "premise": "A lantern gate is failing.",
                    "acts": ["arrival", "repair", "choice"],
                    "scenes": ["Lantern Gate", "Broken Tower", "Final Choice"],
                    "endings": ["repair", "release"],
                    "risks": [],
                },
                "summary": "Deterministic Module Studio outline.",
            }
        elif "Task: Generate the complete canonical D&D module source" in context:
            module_result = {
                "canonical_source": (
                    "# Lantern Gate\n\n"
                    "## Premise\nThe lantern gate is failing and shadows threaten the valley.\n\n"
                    "## Scene 1: Arrival\nThe party receives clear evidence and two routes.\n\n"
                    "## Scene 2: Broken Tower\n"
                    "A complete exploration challenge reveals the cause.\n\n"
                    "## Scene 3: Final Choice\nThe heroes repair or release the gate through "
                    "negotiation, skill checks, or a legal combat ending.\n\n"
                    "## Endings\nBoth outcomes settle the threat and campaign continuity.\n"
                ),
                "package_decisions": {"version": "1.0.0"},
                "summary": "Deterministic complete module source.",
            }
        elif "Task: Review the mechanically imported draft" in context:
            source_key_match = re.search(r'"source_key"\s*:\s*"([^"]+)"', context)
            chunk_hash_match = re.search(r'"chunk_hash"\s*:\s*"([a-f0-9]+)"', context)
            source_ref = {
                "source_key": source_key_match.group(1) if source_key_match else "module-source",
                "page": None,
                "chunk_hash": chunk_hash_match.group(1) if chunk_hash_match else "0" * 64,
                "note": "Agent-reviewed generated source evidence.",
            }
            module_result = {
                "approved": True,
                "summary": "Evidence-grounded and playable.",
                "findings": [],
                "package_decisions": {
                    "version": "1.0.0",
                    "manifest": {
                        "title": "Lantern Gate",
                        "classification": "adventure",
                        "compatibility": {
                            "editions": ["2024"],
                            "required_capabilities": ["module_pack_v2"],
                        },
                        "activation": {"mode": "campaign_attach", "default_active": False},
                        "continuity": {
                            "series_id": None,
                            "order": None,
                            "continues_from": None,
                            "state_policy": {},
                        },
                        "content_summary": {},
                        "play_profile": {
                            "starting_level": {"value": 1, "source_refs": [source_ref]},
                            "expected_end_level": {"value": 2, "source_refs": [source_ref]},
                            "advancement": {
                                "modes": ["milestone"],
                                "recommended": "milestone",
                                "source_refs": [source_ref],
                            },
                            "pregenerated_characters": {
                                "available": False,
                                "applicability": "Reviewed; none are included.",
                                "source_refs": [source_ref],
                            },
                        },
                    },
                },
            }
        elif "Task: Confirm finalization" in context:
            module_result = {
                "confirmed": True,
                "note": "Deterministic evidence review passed before finalization.",
            }
        if module_result is not None:
            self._json(
                200,
                {
                    "id": "chatcmpl-module-studio",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "sagasmith-e2e",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(module_result),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 19,
                        "completion_tokens": 11,
                        "total_tokens": 30,
                    },
                },
            )
            return
        if campaign_match and campaign_match.group(1) == "community":
            self._json(
                200,
                {
                    "id": "chatcmpl-artifact-review",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "sagasmith-e2e",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "approved": True,
                                        "summary": "Synthetic original fixture is publishable.",
                                        "findings": [],
                                    }
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 5,
                        "total_tokens": 16,
                    },
                },
            )
            return
        tool_names = {
            str((tool.get("function") or tool).get("name") or "")
            for tool in request.get("tools") or []
            if isinstance(tool, dict)
        }
        tool_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        exposure_name = next((name for name in tool_names if name.endswith("_exposure")), "")
        character_query_name = next(
            (name for name in tool_names if name.endswith("_character_query")), ""
        )
        if authenticated and campaign_match and principal_match and not tool_messages:
            self._tool_call(
                exposure_name,
                {
                    "action": "open",
                    "campaign_id": campaign_match.group(1),
                    "principal_id": principal_match.group(1),
                },
                "exposure-open",
            )
            return
        if authenticated and campaign_match and principal_match and character_query_name:
            if not any(
                str(message.get("name", "")).endswith("_character_query")
                for message in tool_messages
            ):
                self._tool_call(
                    character_query_name,
                    {
                        "view": "list",
                        "payload": {"campaign_id": campaign_match.group(1)},
                        "principal_id": principal_match.group(1),
                    },
                    "character-query",
                )
                return
        if authenticated and campaign_match and principal_match and exposure_name:
            exposure_calls = sum(
                str(message.get("name", "")).endswith("_exposure") for message in tool_messages
            )
            if exposure_calls == 1:
                self._tool_call(
                    exposure_name,
                    {
                        "action": "search",
                        "campaign_id": campaign_match.group(1),
                        "principal_id": principal_match.group(1),
                        "query": "character_query",
                    },
                    "exposure-search",
                )
                return
            if exposure_calls == 2:
                self._tool_call(
                    exposure_name,
                    {
                        "action": "set",
                        "campaign_id": campaign_match.group(1),
                        "principal_id": principal_match.group(1),
                        "add_tool_ids": ["character_query"],
                    },
                    "exposure-set",
                )
                return
        native_call_completed = any(
            str(message.get("name", "")).endswith("_character_query") for message in tool_messages
        )
        content = (
            "SagaSmith container E2E dynamic MCP call completed."
            if authenticated and native_call_completed
            else "SagaSmith mock provider reached."
        )
        self._json(
            200,
            {
                "id": "chatcmpl-sagasmith-e2e",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "sagasmith-e2e",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 7,
                    "total_tokens": 24,
                },
            },
        )

    def _tool_call(self, name: str, arguments: dict, call_id: str) -> None:
        if not name:
            self._json(400, {"error": {"message": "required MCP tool was not exposed"}})
            return
        self._json(
            200,
            {
                "id": f"chatcmpl-{call_id}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "sagasmith-e2e",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{call_id}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                },
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
