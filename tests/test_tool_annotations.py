from __future__ import annotations

import asyncio

from canvas_mcp.server import mcp

# Tools that change something in Canvas. Chat clients ask before running these; everything else is read-only.
WRITE_TOOLS = {"confirm_assignment_submission", "cancel_scheduled_submission"}


def _tools():
    return asyncio.run(mcp.list_tools())


def test_only_canvas_writes_need_approval():
    tools = _tools()
    assert len(tools) > 30
    for tool in tools:
        hints = tool.annotations
        assert hints is not None, tool.name
        if tool.name in WRITE_TOOLS:
            assert hints.readOnlyHint is False and hints.destructiveHint is True, tool.name
        else:
            assert hints.readOnlyHint is True and hints.destructiveHint is False, tool.name


def test_every_write_tool_exists():
    assert WRITE_TOOLS <= {tool.name for tool in _tools()}
