"""Normalize MCP SDK result fields across the legacy and 2.x Python models."""

from __future__ import annotations

from typing import Any


def is_tool_error(result: Any) -> bool:
    """Return the tool error flag from either SDK attribute spelling."""
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def structured_tool_content(result: Any) -> Any:
    """Return structured content from either SDK attribute spelling."""
    return getattr(
        result,
        "structured_content",
        getattr(result, "structuredContent", None),
    )
