"""Closed-loop capture: turn real production failures into new eval cases.

When a run is flagged in production - the Accessibility Reviewer requests a
revision, or the plan yields no usable stops - we append the traveller's message
and what went wrong to `eval/captured.jsonl`. `run_evals.py` folds these captured
cases back into the eval set, so the set grows from real failures and later
versions are measured against the exact inputs that previously broke.

This file deliberately imports nothing from `app` so the pipeline can import it
without any risk of a cycle.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CAPTURED = Path(__file__).resolve().parent / "captured.jsonl"


def capture_case(
    message: str,
    reason: str,
    destination: str | None = None,
    spec: dict | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one captured failure case. Never raises - capture must not break a run."""
    try:
        record = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "message": message,
            "destination": destination,
            "personas": (spec or {}).get("personas"),
            "needs_accessibility_review": bool(spec)
            and _implies_mobility_limits(spec),
            "extra": extra or {},
        }
        with CAPTURED.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("captured eval case (%s): %s", reason, message[:80])
    except OSError as exc:
        logger.warning("could not capture eval case: %s", exc)


def _implies_mobility_limits(spec: dict) -> bool:
    group = spec.get("group") or {}
    if group.get("seniors") or group.get("kids"):
        return True
    if spec.get("accessibility"):
        return True
    limited = {"seniors_low_mobility", "accessibility_first", "family_with_kids"}
    return bool(limited.intersection(spec.get("personas") or []))


def load_captured() -> list[dict]:
    """Read captured cases (newest first). Returns [] when none exist."""
    if not CAPTURED.exists():
        return []
    rows: list[dict] = []
    for line in CAPTURED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    rows.reverse()
    return rows
