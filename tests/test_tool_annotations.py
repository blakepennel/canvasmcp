from __future__ import annotations

import asyncio

from canvas_mcp.server import mcp

# Tools that change something in Canvas. Chat clients ask before running these; everything else is read-only.
WRITE_TOOLS = {"confirm_assignment_submission", "cancel_scheduled_submission"}


def _tools():
    return asyncio.run(mcp.list_tools())


def _hints(tool):
    # Newer MCP SDKs renamed readOnlyHint/destructiveHint to snake_case and deprecate the old names.
    hints = tool.annotations
    assert hints is not None, tool.name
    if hasattr(type(hints), "read_only_hint") or "read_only_hint" in getattr(type(hints), "model_fields", {}):
        return hints.read_only_hint, hints.destructive_hint
    return hints.readOnlyHint, hints.destructiveHint


def test_only_canvas_writes_need_approval():
    tools = _tools()
    assert len(tools) > 30
    for tool in tools:
        read_only, destructive = _hints(tool)
        if tool.name in WRITE_TOOLS:
            assert read_only is False and destructive is True, tool.name
        else:
            assert read_only is True and destructive is False, tool.name


def test_every_write_tool_exists():
    assert WRITE_TOOLS <= {tool.name for tool in _tools()}
