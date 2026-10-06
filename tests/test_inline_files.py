from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from unittest import mock

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp.types import BlobResourceContents, EmbeddedResource
import pytest

from canvas_mcp.server import mcp
from tools.files import download_course_file, read_course_file


def _download_bytes(payload: bytes):
    def download_file(*, destination_path: str, **_kwargs):
        Path(destination_path).write_bytes(payload)
        return {"content_type": "application/pdf"}

    return download_file


@pytest.mark.parametrize("mime_key", ["content-type", "content_type"])
def test_read_course_file_embeds_downloaded_bytes(mock_client, mime_key):
    payload = b"%PDF-1.4\nexample"
    mock_client.get_file.return_value = {
        "display_name": "lecture notes.pdf",
        mime_key: "application/pdf",
        "size": len(payload),
    }
    mock_client.download_file.side_effect = _download_bytes(payload)

    result = read_course_file("12", "34")

    assert len(result.content) == 2
    resource = result.content[1]
    assert isinstance(resource, EmbeddedResource)
    assert isinstance(resource.resource, BlobResourceContents)
    assert resource.resource.mimeType == "application/pdf"
    assert str(resource.resource.uri).endswith("/lecture%20notes.pdf")
    assert base64.b64decode(resource.resource.blob) == payload
    mock_client.download_file.assert_called_once()


def test_read_course_file_rejects_known_oversize_without_downloading(mock_client):
    mock_client.get_file.return_value = {"size": 2 * 1024 * 1024}

    with pytest.raises(ValueError, match="above the 1 MB inline limit"):
        read_course_file("12", "34", max_size_mb=1)
    mock_client.download_file.assert_not_called()


@pytest.mark.parametrize("metadata", [{"size": 1}, {}])
def test_read_course_file_rejects_actual_oversize(mock_client, metadata):
    mock_client.get_file.return_value = metadata
    mock_client.download_file.side_effect = _download_bytes(b"x" * (1024 * 1024 + 1))

    with (
        mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Unbounded read")),
        pytest.raises(ValueError, match="above the 1 MB inline limit"),
    ):
        read_course_file("12", "34", max_size_mb=1)


def test_inline_read_bounds_memory_and_cleans_up(mock_client):
    mock_client.get_file.return_value = {}
    stream = mock.MagicMock()
    stream.__enter__.return_value.read.return_value = b"x" * (1024 * 1024 + 1)
    with (
        mock.patch.object(Path, "open", return_value=stream),
        pytest.raises(ValueError, match="above the 1 MB inline limit"),
    ):
        read_course_file("12", "34", max_size_mb=1)
    stream.__enter__.return_value.read.assert_called_once_with(1024 * 1024 + 1)
    destination = Path(mock_client.download_file.call_args.kwargs["destination_path"])
    assert not destination.parent.exists()


def test_existing_download_still_returns_local_path(mock_client, tmp_path):
    payload = b"existing download method"
    mock_client.get_file.return_value = {
        "display_name": "lesson.txt",
        "content_type": "text/plain",
    }
    mock_client.download_file.side_effect = _download_bytes(payload)

    with mock.patch("tools.files.download_dir", return_value=tmp_path):
        result = download_course_file({"course_id": "12", "file_id": "34"})

    assert Path(result["local_path"]).read_bytes() == payload
    assert result["already_present"] is False


def test_mcp_exposes_inline_file_tool(mock_client):
    payload = b"hello from Canvas"
    mock_client.get_file.return_value = {
        "display_name": "lesson.txt",
        "content_type": "text/plain",
        "size": len(payload),
    }
    mock_client.download_file.side_effect = _download_bytes(payload)

    async def call_mcp():
        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            result = await client.call_tool_mcp(
                "read_course_file", {"course_id": "12", "file_id": "34"}
            )
            return names, result

    names, result = asyncio.run(call_mcp())

    assert "read_course_file" in names
    assert "download_course_file" in names
    assert result.isError is not True
    assert any(
        isinstance(item, EmbeddedResource)
        and base64.b64decode(item.resource.blob) == payload
        for item in result.content
    ), repr(result)


def test_mcp_inline_limit_reports_existing_download_option(mock_client):
    mock_client.get_file.return_value = {"size": 2 * 1024 * 1024}

    async def call_mcp():
        async with Client(mcp) as client:
            return await client.call_tool_mcp(
                "read_course_file",
                {"course_id": "12", "file_id": "34", "max_size_mb": 1},
            )

    result = asyncio.run(call_mcp())
    assert result.isError is True
    assert "download_course_file" in result.content[0].text
    mock_client.download_file.assert_not_called()


def test_installed_launcher_over_stdio():
    executable = os.getenv("CANVAS_MCP_EXECUTABLE")
    if not executable:
        pytest.skip("Set CANVAS_MCP_EXECUTABLE to test an installed launcher")

    env = {
        **os.environ,
        "CANVAS_BASE_URL": "https://canvas.example.test",
        "CANVAS_SESSION_COOKIE": "local-smoke-test",
        "CANVAS_CSRF_TOKEN": "local-smoke-test",
    }

    async def list_remote_tools():
        transport = StdioTransport(
            executable,
            ["--transport", "stdio"],
            env=env,
            keep_alive=False,
        )
        async with Client(transport) as client:
            return {tool.name for tool in await client.list_tools()}

    names = asyncio.run(list_remote_tools())
    assert "download_course_file" in names
    assert "read_course_file" in names
