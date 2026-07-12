"""Per-chat context store: data/sessions/{chat_id}.json."""

import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

MAX_HISTORY = 10


def _session_path(chat_id: int) -> Path:
    return settings.sessions_dir / f"{chat_id}.json"


def load_session(chat_id: int) -> dict:
    path = _session_path(chat_id)
    if not path.exists():
        return {"spec": None, "history": [], "last_trip_id": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"spec": None, "history": [], "last_trip_id": None}


def save_session(chat_id: int, spec: dict | None, message: str, trip_id: str | None) -> None:
    session = load_session(chat_id)
    session["spec"] = spec
    session["history"] = (session.get("history") or [])[-(MAX_HISTORY - 1) :] + [message]
    if trip_id:
        session["last_trip_id"] = trip_id
    session["updated_at"] = datetime.now(timezone.utc).isoformat()
    _session_path(chat_id).write_text(
        json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def reset_session(chat_id: int) -> None:
    _session_path(chat_id).unlink(missing_ok=True)
