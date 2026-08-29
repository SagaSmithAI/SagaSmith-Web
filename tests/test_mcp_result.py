from __future__ import annotations

from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

from sagasmith_service.mcp_result import is_tool_error, structured_tool_content


def test_mcp_2_call_tool_result_uses_snake_case_attributes() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="retry with a fresh revision")],
        structuredContent={"code": "stale_revision", "retryable": True},
        isError=True,
    )

    assert is_tool_error(result) is True
    assert structured_tool_content(result) == {
        "code": "stale_revision",
        "retryable": True,
    }


def test_legacy_call_tool_result_aliases_remain_supported() -> None:
    result = SimpleNamespace(
        content=[],
        structuredContent={"result": {"revision": 7}},
        isError=False,
    )

    assert is_tool_error(result) is False
    assert structured_tool_content(result) == {"result": {"revision": 7}}
