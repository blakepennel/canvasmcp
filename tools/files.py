from __future__ import annotations

import base64
import os
import re
from collections import OrderedDict
from typing import Any
from urllib.parse import quote

from fastmcp.tools import ToolResult
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    TextContent,
)

from tools import file_parts
from tools.common import (
    canvas_client,
    download_dir,
    download_file_path,
)


def list_course_files(args: dict[str, Any]) -> dict[str, Any]:
    course_id = str(args.get("course_id", "")).strip()

    limit = int(args.get("limit", 100))
    files = canvas_client().list_files(
        course_id=course_id,
        search=str(args.get("search")) if args.get("search") else None,
        sort=str(args.get("sort")) if args.get("sort") else None,
        order=str(args.get("order")) if args.get("order") else None,
        limit=limit,
    )
    items = [
        {
            "id": str(file.get("id", "")),
            "display_name": file.get("display_name"),
            "filename": file.get("filename"),
            "content_type": file.get("content_type"),
            "size": file.get("size"),
            "updated_at": file.get("updated_at"),
            "url": file.get("url"),
            "folder_id": str(file["folder_id"])
            if file.get("folder_id") is not None
            else None,
        }
        for file in files
    ]
    return {"course_id": course_id, "count": len(items), "files": items}


def download_course_file(args: dict[str, Any]) -> dict[str, Any]:
    course_id = str(args.get("course_id", "")).strip()
    file_id = str(args.get("file_id", "")).strip()

    force_refresh = bool(args.get("force_refresh", False))
    root = download_dir()
    root.mkdir(parents=True, exist_ok=True)

    file_info = canvas_client().get_file(course_id=course_id, file_id=file_id)
    display_name = str(
        file_info.get("display_name") or file_info.get("filename") or f"file_{file_id}"
    )
    target_path = download_file_path(root, course_id, file_id, display_name)

    if target_path.exists() and force_refresh:
        target_path.unlink()

    if target_path.exists():
        return {
            "course_id": course_id,
            "file_id": file_id,
            "display_name": display_name,
            "local_path": str(target_path),
            "size_bytes": target_path.stat().st_size,
            "content_type": file_info.get("content_type"),
            "already_present": True,
        }

    downloaded = canvas_client().download_file(
        course_id=course_id,
        file_id=file_id,
        destination_path=str(target_path),
    )
    return {
        "course_id": course_id,
        "file_id": file_id,
        "display_name": display_name,
        "local_path": str(target_path),
        "size_bytes": target_path.stat().st_size,
        "content_type": downloaded.get("content_type"),
        "already_present": False,
    }


def _inline_ceiling_mb() -> int:
    """The largest file read_course_file sends inline: 50 MB, or less where CANVAS_MCP_MAX_INLINE_MB says so.

    Some MCP transports cap message size (ChatGPT's tunnel rejects anything over 10 MB, and base64 makes a file
    a third bigger), and an oversized response there drops the whole connection rather than failing one call.
    """
    try:
        return max(1, min(int(os.environ.get("CANVAS_MCP_MAX_INLINE_MB", "50")), 50))
    except ValueError:
        return 50


MB = 1024 * 1024
# The biggest file read_course_file downloads to send in parts or read as text; beyond this, use download_course_file.
SOURCE_MAX_BYTES = 150 * 1024 * 1024
# Characters per part of extracted text (about 25k tokens), so text stays the light option.
TEXT_PART_CHARS = 100_000
TEXT_ONLY_NOTE = (
    "Text only: images, diagrams, equations, and layout are missing, so read the file itself (without "
    "text_only) when there's room for it."
)
_recent_files: OrderedDict[tuple[str, ...], bytes] = OrderedDict()
_recent_plans: OrderedDict[tuple[Any, ...], list[Any]] = OrderedDict()
_RECENT_BYTES = 200 * 1024 * 1024


def _remember(key: tuple[str, ...], data: bytes) -> bytes:
    """Keep the last few downloads so reading part 2, 3, ... doesn't download the file again."""
    _recent_files[key] = data
    _recent_files.move_to_end(key)
    while len(_recent_files) > 1 and sum(len(v) for v in _recent_files.values()) > _RECENT_BYTES:
        _recent_files.popitem(last=False)
    return data


def _file_key(course_id: str, file_id: str, info: dict[str, Any]) -> tuple[str, ...]:
    return (course_id, file_id, str(info.get("updated_at") or ""), str(info.get("size") or ""))


def _mb(size: Any) -> str:
    return f"{size / MB:.1f} MB" if isinstance(size, int) else "an unknown size"


def _ranges(unit: str, first: int, last: int) -> str:
    return f"{unit} {first}" if first == last else f"{unit}s {first}-{last}"


def _resource(course_id: str, file_id: str, name: str, content_type: str, data: bytes) -> EmbeddedResource:
    uri = f"canvas-file://courses/{quote(course_id)}/files/{quote(file_id)}/{quote(name)}"
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(uri=uri, mimeType=content_type, blob=base64.b64encode(data).decode("ascii")),
    )


def _pick(parts: list[Any], part: int | None, name: str) -> tuple[int, Any]:
    number = part or 1
    if number > len(parts):
        raise ValueError(f"{name} has {len(parts)} part{'s' if len(parts) != 1 else ''}; part {number} doesn't exist.")
    return number, parts[number - 1]


def _text_result(name: str, kind: str, texts: list[str], part: int | None, intro: str, note: str) -> ToolResult:
    unit = file_parts.UNIT[kind]
    parts = file_parts.text_parts(texts, TEXT_PART_CHARS)
    if not parts:
        return ToolResult(content=[TextContent(type="text", text=f"{intro} {name} has no text to read.")])
    number, chosen = _pick(parts, part, name)
    header = f"{intro} {name}: {_ranges(unit, chosen.first, chosen.last)} of {len(texts)}"
    if len(parts) > 1:
        header += f", part {number} of {len(parts)}"
        if number < len(parts):
            header += f" (next: part={number + 1})"
    header += f". {note}".rstrip(". ") + "."
    body = file_parts.join_texts(kind, texts, chosen.first, chosen.last)
    return ToolResult(content=[TextContent(type="text", text=f"{header}\n\n{body}")])


def _captions(client: Any, file_id: str, info: dict[str, Any]) -> list[str]:
    tracks = client.list_media_tracks(file_id=file_id) if info.get("media_entry_id") else []
    tracks = [t for t in tracks if (t.get("content") or "").strip()]
    if not tracks:
        return []
    english = [t for t in tracks if str(t.get("locale") or "").lower().startswith("en")]
    return file_parts.caption_lines(str((english or tracks)[0]["content"]))


def read_course_file(
    course_id: str,
    file_id: str,
    max_size_mb: int = 25,
    part: int | None = None,
    text_only: bool = False,
) -> ToolResult:
    """Return a Canvas file inline; files too big for one response come in parts, and text is available too."""
    course_id = str(course_id).strip()
    file_id = str(file_id).strip()
    if not course_id:
        raise ValueError("course_id is required")
    if not file_id:
        raise ValueError("file_id is required")
    if part is not None and int(part) < 1:
        raise ValueError("part starts at 1")
    part = int(part) if part is not None else None

    try:
        max_size_mb = max(1, min(int(max_size_mb), _inline_ceiling_mb()))
    except (TypeError, ValueError):
        max_size_mb = min(25, _inline_ceiling_mb())
    max_bytes = max_size_mb * MB
    client = canvas_client()
    file_info, data = client.read_file_bytes(course_id=course_id, file_id=file_id, max_bytes=max_bytes)
    name = str(file_info.get("display_name") or file_info.get("filename") or f"file_{file_id}")
    content_type = str(
        file_info.get("content-type") or file_info.get("content_type") or "application/octet-stream"
    )
    size = file_info.get("size") if isinstance(file_info.get("size"), int) else (len(data) if data else None)
    kind = file_parts.file_kind(content_type, name)

    if kind == file_parts.MEDIA:
        # Video and audio are never split or transcribed here; Canvas's own captions come along when it has them.
        captions = _captions(client, file_id, file_info)
        if data is not None and not text_only:
            content: list[Any] = [
                TextContent(type="text", text=f"Canvas file: {name} ({content_type}, {len(data)} bytes)."),
                _resource(course_id, file_id, name, content_type, data),
            ]
            if captions:
                transcript = _text_result(name, kind, captions, None, "Canvas captions for", "")
                content += transcript.content
            return ToolResult(content=content)
        if captions:
            intro = "Canvas captions for" if text_only else f"{name} is {_mb(size)}, too big to send, so here are Canvas's captions for"
            return _text_result(name, kind, captions, part, intro, "")
        raise ValueError(
            f"{name} is {_mb(size)}, above the {max_size_mb} MB inline limit, and Canvas has no captions for it. "
            "Use download_course_file instead."
            if data is None
            else f"Canvas has no captions for {name}."
        )

    if data is not None and not text_only and part in (None, 1):
        return ToolResult(
            content=[
                TextContent(type="text", text=f"Canvas file: {name} ({content_type}, {len(data)} bytes)."),
                _resource(course_id, file_id, name, content_type, data),
            ]
        )

    can_split = kind in file_parts.SPLITTABLE
    can_read = kind in file_parts.EXTRACTABLE
    if data is None and not (can_read if text_only else can_split):
        hint = " Its text can be read with text_only=true." if can_read else ""
        raise ValueError(
            f"File is {f'{size} bytes, ' if isinstance(size, int) else ''}above the {max_size_mb} MB inline limit, "
            f"and {content_type} files can't be sent in parts.{hint} Use download_course_file instead."
        )
    if not text_only and not can_split:
        raise ValueError(f"{name} fits in one response, so it has only 1 part.")
    if text_only and not can_read:
        raise ValueError(f"Can't read text from {content_type} files; read the file itself instead.")
    if data is None:
        key = _file_key(course_id, file_id, file_info)
        data = _recent_files.get(key)
        if data is None:
            _, data = client.read_file_bytes(course_id=course_id, file_id=file_id, max_bytes=SOURCE_MAX_BYTES)
            if data is None:
                raise ValueError(
                    f"{name} is {_mb(size)}, too big to read in parts here. Use download_course_file instead."
                )
            _remember(key, data)

    if text_only:
        return _text_result(name, kind, file_parts.unit_texts(kind, data), part, "Text of", TEXT_ONLY_NOTE)

    document = file_parts.open_file(kind, data)
    unit = file_parts.UNIT[kind]
    plan_key = (*_file_key(course_id, file_id, file_info), kind, max_bytes)
    parts = _recent_plans.get(plan_key)
    if parts is None:
        parts = file_parts.plan(document.sizes(), max_bytes, document.build)
        _recent_plans[plan_key] = parts
        while len(_recent_plans) > 20:
            _recent_plans.popitem(last=False)
    number, chosen = _pick(parts, part, name)
    listing = ", ".join(
        f"part {i}: {_ranges(unit, p.first, p.last)}{'' if p.fits else ' (text only: too big to send)'}"
        for i, p in enumerate(parts, start=1)
    )
    header = (
        f"{name} is {_mb(len(data))}, over the {max_size_mb} MB limit for one response, so it comes in "
        f"{len(parts)} parts that keep whole {unit}s together ({listing}). This is part {number} "
        f"({_ranges(unit, chosen.first, chosen.last)})."
        if len(parts) > 1
        else f"Canvas file: {name} ({content_type}, {len(data)} bytes)."
    )
    if number < len(parts):
        header += f" Read the next with part={number + 1}."
    if not chosen.fits:
        # One page or slide that is over the limit by itself: send its text rather than nothing.
        texts = file_parts.unit_texts(kind, data)
        body = file_parts.join_texts(kind, texts, chosen.first, chosen.last)
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"{header} {unit.capitalize()} {chosen.first} alone is {_mb(chosen.size)}, over the "
                    f"limit, so only its text is shown. {TEXT_ONLY_NOTE}\n\n{body}",
                )
            ]
        )
    piece = document.build(chosen.first, chosen.last) if len(parts) > 1 else data
    piece_name = name if len(parts) == 1 else re.sub(r"(\.[^.]+)?$", rf" (part {number})\1", name, count=1)
    return ToolResult(
        content=[
            TextContent(type="text", text=header),
            _resource(course_id, file_id, piece_name, content_type, piece),
        ]
    )


def list_course_folders(args: dict[str, Any]) -> dict[str, Any]:
    course_id = str(args.get("course_id", "")).strip()

    limit = int(args.get("limit", 150))
    folders = canvas_client().list_folders(course_id=course_id, limit=limit)
    items = [
        {
            "id": str(folder.get("id", "")),
            "name": folder.get("name", "Untitled folder"),
            "full_name": folder.get("full_name"),
            "parent_folder_id": str(folder["parent_folder_id"])
            if folder.get("parent_folder_id") is not None
            else None,
            "files_count": folder.get("files_count"),
            "folders_count": folder.get("folders_count"),
            "updated_at": folder.get("updated_at"),
        }
        for folder in folders
    ]
    return {"course_id": course_id, "count": len(items), "folders": items}


def list_modules(args: dict[str, Any]) -> dict[str, Any]:
    course_id = str(args.get("course_id", "")).strip()

    limit = int(args.get("limit", 100))
    items_limit = int(args.get("items_limit", 100))
    modules = canvas_client().list_modules(
        course_id=course_id,
        search=str(args.get("search")) if args.get("search") else None,
        include_items=bool(args.get("include_items", False)),
        include_content_details=bool(args.get("include_content_details", False)),
        limit=limit,
        items_limit=items_limit,
    )

    items = []
    for module in modules:
        module_items = []
        if isinstance(module.get("items"), list):
            module_items = [
                {
                    "id": str(item.get("id", "")),
                    "type": item.get("type"),
                    "title": item.get("title"),
                    "page_url": item.get("page_url"),
                    "content_id": str(item["content_id"])
                    if item.get("content_id") is not None
                    else None,
                    "html_url": item.get("html_url"),
                    "url": item.get("url"),
                    "published": item.get("published"),
                    "position": item.get("position"),
                    "completion_requirement": item.get("completion_requirement"),
                    "content_details": item.get("content_details"),
                }
                for item in module["items"]
                if isinstance(item, dict)
            ]

        items.append(
            {
                "id": str(module.get("id", "")),
                "name": module.get("name", "Untitled module"),
                "position": module.get("position"),
                "unlock_at": module.get("unlock_at"),
                "require_sequential_progress": module.get(
                    "require_sequential_progress"
                ),
                "publish_final_grade": module.get("publish_final_grade"),
                "published": module.get("published"),
                "state": module.get("state"),
                "prerequisite_module_ids": [
                    str(module_id)
                    for module_id in (module.get("prerequisite_module_ids") or [])
                ],
                "items_count": module.get("items_count"),
                "items_url": module.get("items_url"),
                "items": module_items,
            }
        )

    return {"course_id": course_id, "count": len(items), "modules": items}
