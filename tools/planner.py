from __future__ import annotations

from datetime import date, datetime
from typing import Any

from tools.common import (
    canvas_client,
    clamp,
    invalid_argument,
    looks_like_canvas_id,
    missing_argument,
    truncate_html,
)

# Kinds of To Do item Canvas lets you mark done or dismiss (its "plannable" types).
PLANNABLE_TYPES = ("assignment", "quiz", "discussion_topic", "wiki_page", "planner_note", "calendar_event",
                   "assessment_request")
TITLE_LIMIT = 255
DETAILS_LIMIT = 10000


def _flag(value: bool) -> str:
    return "true" if value else "false"   # Canvas reads the strings; Python's "False" isn't one it knows


def _valid_date(value: Any) -> str | None:
    """A date (YYYY-MM-DD) or ISO date-time, as given; raises ValueError otherwise."""
    text = str(value).strip()
    try:
        date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not a date: {text!r}") from None
    return text


def _map_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": str(note.get("id", "")),
        "title": note.get("title"),
        "details": truncate_html(note.get("description") or note.get("details"), limit=DETAILS_LIMIT),
        "todo_date": note.get("todo_date"),
        "course_id": str(note["course_id"]) if note.get("course_id") else None,
        "state": note.get("workflow_state"),
    }


def _map_item(item: dict[str, Any]) -> dict[str, Any]:
    plannable = item.get("plannable") or {}
    override = item.get("planner_override") or {}
    submissions = item.get("submissions") if isinstance(item.get("submissions"), dict) else {}
    return {
        "plannable_type": item.get("plannable_type"),
        "plannable_id": str(item.get("plannable_id", "")),
        "title": plannable.get("title") or plannable.get("name"),
        "date": item.get("plannable_date"),
        "course_id": str(item["course_id"]) if item.get("course_id") else None,
        "context_name": item.get("context_name"),
        "done": bool(override.get("marked_complete")),
        "dismissed": bool(override.get("dismissed")),
        "submitted": submissions.get("submitted") if submissions else None,
        "graded": submissions.get("graded") if submissions else None,
        "details": truncate_html(plannable.get("details"), limit=2000) if item.get("plannable_type") == "planner_note"
        else None,
        "html_url": item.get("html_url"),
    }


def list_planner_items(args: dict[str, Any]) -> dict[str, Any]:
    try:
        start = _valid_date(args["start_date"]) if args.get("start_date") else None
        end = _valid_date(args["end_date"]) if args.get("end_date") else None
    except ValueError as exc:
        return invalid_argument(f"start_date and end_date must be dates (YYYY-MM-DD): {exc}")
    items = canvas_client().list_planner_items(
        start_date=start, end_date=end, incomplete_only=bool(args.get("incomplete_only")),
        limit=clamp(args.get("limit"), 100),
    )
    mapped = [_map_item(i) for i in items]
    return {"start_date": start, "end_date": end, "count": len(mapped), "items": mapped}


def _note_fields(args: dict[str, Any], *, creating: bool) -> dict[str, Any] | tuple[str, str]:
    """Canvas planner-note fields from the arguments, or (field, problem) for the first bad one."""
    fields: dict[str, Any] = {}
    if "title" in args or creating:
        title = str(args.get("title") or "").strip()
        if not title:
            return ("title", "is required")
        if len(title) > TITLE_LIMIT:
            return ("title", f"must be at most {TITLE_LIMIT} characters")
        fields["title"] = title
    if args.get("details") is not None:
        details = str(args["details"])
        if len(details) > DETAILS_LIMIT:
            return ("details", f"must be at most {DETAILS_LIMIT} characters")
        fields["details"] = details
    if args.get("todo_date"):
        try:
            fields["todo_date"] = _valid_date(args["todo_date"])
        except ValueError as exc:
            return ("todo_date", str(exc))
    if args.get("course_id"):
        course_id = str(args["course_id"]).strip()
        if not looks_like_canvas_id(course_id):
            return ("course_id", "must be a Canvas course ID")
        fields["course_id"] = course_id
    return fields


def create_todo_note(args: dict[str, Any]) -> dict[str, Any]:
    fields = _note_fields(args, creating=True)
    if isinstance(fields, tuple):
        return missing_argument("title") if fields == ("title", "is required") else invalid_argument(
            f"{fields[0]} {fields[1]}")
    note = canvas_client().create_planner_note(fields=fields)
    return {"created": True, "note": _map_note(note)}


def update_todo_note(args: dict[str, Any]) -> dict[str, Any]:
    note_id = str(args.get("note_id") or "").strip()
    if not note_id:
        return missing_argument("note_id")
    if not looks_like_canvas_id(note_id):
        return invalid_argument("note_id must be a Canvas ID")
    fields = _note_fields(args, creating=False)
    if isinstance(fields, tuple):
        return invalid_argument(f"{fields[0]} {fields[1]}")
    if not fields:
        return invalid_argument("give at least one of title, details, todo_date, or course_id to change")
    note = canvas_client().update_planner_note(note_id=note_id, fields=fields)
    return {"updated": True, "note": _map_note(note)}


def delete_todo_note(args: dict[str, Any]) -> dict[str, Any]:
    note_id = str(args.get("note_id") or "").strip()
    if not note_id:
        return missing_argument("note_id")
    if not looks_like_canvas_id(note_id):
        return invalid_argument("note_id must be a Canvas ID")
    client = canvas_client()
    note = client.get_planner_note(note_id=note_id)   # confirms it's your own note before deleting it
    client.delete_planner_note(note_id=note_id)
    return {"deleted": True, "note": _map_note(note)}


def set_todo_item_state(args: dict[str, Any]) -> dict[str, Any]:
    plannable_type = str(args.get("plannable_type") or "").strip()
    plannable_id = str(args.get("plannable_id") or "").strip()
    if not plannable_type:
        return missing_argument("plannable_type")
    if plannable_type not in PLANNABLE_TYPES:
        return invalid_argument(f"plannable_type must be one of: {', '.join(PLANNABLE_TYPES)}")
    if not plannable_id:
        return missing_argument("plannable_id")
    if not looks_like_canvas_id(plannable_id):
        return invalid_argument("plannable_id must be a Canvas ID")
    state: dict[str, str] = {}
    for name, field in (("done", "marked_complete"), ("dismissed", "dismissed")):
        if args.get(name) is not None:
            if not isinstance(args[name], bool):
                return invalid_argument(f"{name} must be true or false")
            state[field] = _flag(args[name])
    if not state:
        return invalid_argument("give done and/or dismissed")

    client = canvas_client()
    existing = next((o for o in client.list_planner_overrides()
                     if o.get("plannable_type") == plannable_type and str(o.get("plannable_id")) == plannable_id), None)
    if existing:
        override = client.update_planner_override(override_id=str(existing["id"]), state=state)
    else:
        override = client.create_planner_override(plannable_type=plannable_type, plannable_id=plannable_id, state=state)
    return {
        "plannable_type": plannable_type,
        "plannable_id": plannable_id,
        "done": bool(override.get("marked_complete")),
        "dismissed": bool(override.get("dismissed")),
        "note": "Only changes your To Do list; nothing is submitted.",
    }
