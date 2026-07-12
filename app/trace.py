"""Per-run agent tracing: who called whom, tokens, cost and latency per step.

Every agent step opens a `span`; spans nest through a contextvar parent stack so
the saved trace reconstructs the call tree (manager -> specialists). The trace is
persisted to `data/trips/{trip_id}/trace.json` after every update, so the live
run viewer can stream it step by step while a judge watches the demo.

Token counts are estimated (`hermes -z` does not expose usage): ~4 chars/token,
priced with the blended rates in settings. Latency is real wall-clock time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_current_trace: ContextVar[Optional["Trace"]] = ContextVar("hermes_trace", default=None)
_current_parent: ContextVar[Optional[str]] = ContextVar("hermes_parent", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str | None) -> int:
    """~4 chars per token; good enough for a per-step cost estimate."""
    return max(0, round(len(text or "") / 4))


class Trace:
    """Ordered list of spans for one trip, persisted as JSON on every change."""

    def __init__(self, trip_id: str) -> None:
        self.trip_id = trip_id
        self.started_at = _now()
        self.spans: list[dict[str, Any]] = []
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"s{self._counter}"

    def append(self, span: dict[str, Any]) -> None:
        self.spans.append(span)
        self.save()

    def totals(self) -> dict[str, Any]:
        tokens = sum(s.get("tokens") or 0 for s in self.spans)
        cost = sum(s.get("cost_usd") or 0.0 for s in self.spans)
        llm_calls = sum(1 for s in self.spans if s.get("kind") == "llm")
        # top-level spans (parent_id is None) bound the wall-clock of the run
        duration_ms = sum(
            s.get("duration_ms") or 0 for s in self.spans if s.get("parent_id") is None
        )
        return {
            "tokens": tokens,
            "cost_usd": round(cost, 6),
            "llm_calls": llm_calls,
            "duration_ms": duration_ms,
            "agents": sorted({s["agent"] for s in self.spans}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "trip_id": self.trip_id,
            "started_at": self.started_at,
            "updated_at": _now(),
            "totals": self.totals(),
            "spans": self.spans,
        }

    def save(self) -> None:
        try:
            path = settings.trips_dir / self.trip_id / "trace.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:  # tracing must never break the pipeline
            logger.warning("could not persist trace for %s: %s", self.trip_id, exc)


class SpanHandle:
    """Handle passed to `async with span(...)` for enriching a step live."""

    def __init__(self, rec: dict[str, Any] | None, trace: "Trace | None") -> None:
        self._rec = rec
        self._trace = trace

    def set_llm(self, prompt: str, output: str, model: str | None = None) -> None:
        if self._rec is None:
            return
        pt, ot = estimate_tokens(prompt), estimate_tokens(output)
        self._rec.update(
            model=model or self._rec.get("model"),
            prompt_tokens=pt,
            output_tokens=ot,
            tokens=pt + ot,
            cost_usd=round(
                pt / 1e6 * settings.cost_input_per_mtok
                + ot / 1e6 * settings.cost_output_per_mtok,
                6,
            ),
            input_preview=(prompt or "")[:600],
            output_preview=(output or "")[:900],
        )
        if self._trace:
            self._trace.save()

    def set_output(self, preview: str) -> None:
        if self._rec is None:
            return
        self._rec["output_preview"] = (preview or "")[:900]
        if self._trace:
            self._trace.save()

    def note(self, **meta: Any) -> None:
        if self._rec is None:
            return
        self._rec.setdefault("meta", {}).update(meta)
        if self._trace:
            self._trace.save()


def start_trace(trip_id: str) -> Trace:
    """Begin (or reset) the trace for the current async context."""
    tr = Trace(trip_id)
    _current_trace.set(tr)
    _current_parent.set(None)
    tr.save()
    return tr


def current_trace() -> Optional[Trace]:
    return _current_trace.get()


@contextlib.asynccontextmanager
async def span(agent: str, task: str, kind: str = "llm", model: str | None = None):
    """Open a traced step. Nests under the enclosing span automatically.

    `kind` is one of "group" (the manager wrapping the run), "llm" (a model
    call) or "tool" (a deterministic action like search or rendering).
    """
    tr = _current_trace.get()
    if tr is None:  # tracing disabled (e.g. offline eval) - become a no-op
        yield SpanHandle(None, None)
        return

    sid = tr._new_id()
    rec: dict[str, Any] = {
        "id": sid,
        "parent_id": _current_parent.get(),
        "agent": agent,
        "task": task,
        "kind": kind,
        "model": model,
        "status": "running",
        "started_at": _now(),
        "ended_at": None,
        "duration_ms": None,
    }
    tr.append(rec)
    token = _current_parent.set(sid)
    start = time.monotonic()
    handle = SpanHandle(rec, tr)
    try:
        yield handle
        if rec.get("status") == "running":
            rec["status"] = "ok"
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        rec["status"] = "error"
        rec["error"] = str(exc)[:500]
        raise
    finally:
        rec["duration_ms"] = int((time.monotonic() - start) * 1000)
        rec["ended_at"] = _now()
        _current_parent.reset(token)
        tr.save()
