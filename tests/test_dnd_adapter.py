from sagasmith_service.integrations.dnd_mcp import _runtime_error


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
