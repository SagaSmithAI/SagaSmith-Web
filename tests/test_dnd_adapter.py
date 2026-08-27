import asyncio
import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import sagasmith_service.integrations.dnd_mcp as dnd_mcp
from sagasmith_service.integrations.dnd_mcp import (
    DndCombatRender,
    StreamableHttpDndRuntime,
    _combat_render_payload,
    _runtime_error,
)


def test_mcp_exception_group_maps_authoritative_tool_error() -> None:
    grouped = ExceptionGroup(
        "transport cleanup",
        [ValueError("irrelevant"), ExceptionGroup("tool", [RuntimeError("access denied")])],
    )
    mapped = _runtime_error(grouped)
    assert str(mapped) == "access denied"


def test_unknown_mcp_exception_is_not_leaked() -> None:
    mapped = _runtime_error(ValueError("internal transport detail"))
    assert str(mapped) == "D&D MCP request failed"


def combat_render_result(content: bytes, *, checksum: str | None = None):
    digest = checksum or hashlib.sha256(content).hexdigest()
    return SimpleNamespace(
        isError=False,
        structuredContent={
            "audience_projection": "party_public",
            "image_checksum": digest,
            "mime_type": "image/png",
        },
        content=[
            SimpleNamespace(text='{"audience_projection":"party_public"}', meta=None),
            SimpleNamespace(
                data=base64.b64encode(content).decode("ascii"),
                mimeType="image/png",
                meta=None,
            ),
        ],
    )


def test_combat_render_payload_decodes_verified_party_public_png() -> None:
    content = b"\x89PNG\r\n\x1a\nverified-party-public"
    result = combat_render_result(content)
    result.structuredContent["alt_text"] = "  Party\nmap  "
    result.structuredContent["suggested_caption"] = " Public caption "

    rendered = _combat_render_payload(result)

    assert rendered.content == content
    assert rendered.media_type == "image/png"
    assert rendered.metadata["audience_projection"] == "party_public"
    assert rendered.metadata["image_checksum"] == hashlib.sha256(content).hexdigest()
    assert rendered.metadata["alt_text"] == "Party map"
    assert rendered.metadata["suggested_caption"] == "Public caption"


def test_combat_render_payload_rejects_oversized_base64_before_decode(monkeypatch) -> None:
    content = b"\x89PNG\r\n\x1a\nverified-party-public"
    result = combat_render_result(content)
    monkeypatch.setattr(dnd_mcp, "_MAX_COMBAT_RENDER_BASE64_CHARS", 8)

    with pytest.raises(RuntimeError, match="oversized"):
        _combat_render_payload(result)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("audience_projection", "caller", "non-public"),
        ("image_checksum", "0" * 64, "checksum mismatch"),
    ],
)
def test_combat_render_payload_rejects_unsafe_or_corrupt_image(
    field: str, value: str, message: str
) -> None:
    content = b"\x89PNG\r\n\x1a\nverified-party-public"
    result = combat_render_result(content)
    result.structuredContent[field] = value

    with pytest.raises(RuntimeError, match=message):
        _combat_render_payload(result)


def test_runtime_requests_only_party_public_combat_render() -> None:
    expected = DndCombatRender(
        metadata={"audience_projection": "party_public", "image_checksum": "a" * 64},
        content=b"\x89PNG\r\n\x1a\nrender",
        media_type="image/png",
    )
    runtime = StreamableHttpDndRuntime("https://mcp.invalid", http_client=object())  # type: ignore[arg-type]
    runtime._call = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    rendered = asyncio.run(
        runtime.render_public_combat(campaign_id="campaign-1", principal_id="user:1")
    )

    assert rendered is expected
    arguments = runtime._call.await_args.args[1]
    assert arguments["payload"] == {"audience_projection": "party_public"}
    assert arguments["principal_id"] == "user:1"
