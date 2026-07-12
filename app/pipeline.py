"""Persona-aware Goa pipeline: intake -> pick spots -> render a 1-min map video."""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app import linkup, store, video
from app.config import settings
from app.hermes_runner import run_hermes_json

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]


@dataclass
class TripHandle:
    """Returned immediately when a trip is created, before the build runs."""

    trip_id: str
    url: str


@dataclass
class TripResult:
    trip_id: str
    video_path: str
    url: str
    summary: str


INTAKE_PROMPT = """You are the intake step of a Goa travel-planner. Every trip is in Goa, India.
Today's date is {today}.

Stored trip spec from earlier messages in this chat (null if none):
{stored_spec}

New message from the user:
"{message}"

Decide whether the message starts a fresh trip or refines the stored spec, then
merge accordingly. Infer the traveler persona(s) from this set when implied:
pilgrimage, sunset, trek, photography, family_with_kids, seniors_low_mobility,
accessibility_first, food, slow_traveler, beaches, nature.

Respond with ONLY a JSON object (no prose, no fences), using null/[] when unknown:
{{
  "personas": ["one or more of the persona keys above"],
  "days": <int, default 2 if unstated>,
  "group": {{"kids": <bool>, "seniors": <bool>}},
  "pace": "relaxed | balanced | packed",
  "accessibility": ["constraints, e.g. 'no long walks', 'avoid stairs and steep climbs'"],
  "interests": ["free-form extra wishes, e.g. 'seafood', 'quiet cafes'"],
  "summary": "one short line describing this traveler's Goa trip"
}}

Keep everything from the stored spec that the new message does not change."""


PLAN_PROMPT = """You are the personalization step of a Goa travel-planner. You design a
1-minute map video itinerary tailored to the traveler.

Traveler spec:
{spec}

Candidate Goa places (real Google Maps data with ratings and review snippets).
Choose ONLY from these and copy each chosen "name" EXACTLY:
{places}

Select {n_stops} places that best fit this persona and route them in a sensible
order (roughly geographic / by time of day; put sunset spots late). Use the
ratings and review snippets to justify picks and to AVOID poor fits for the
stated accessibility needs (flag stairs, steep climbs, long walks for
seniors/low-mobility/families with kids).

Write warm, vivid narration meant to be SPOKEN aloud. Keep each stop's narration
to ONE short punchy sentence (about 5-8 seconds). Keep it snappy - shorter is better.

Respond with ONLY a JSON object (no prose, no fences):
{{
  "title": "punchy 3-6 word title, e.g. 'Goa for Sunset Chasers'",
  "subtitle": "one short line, e.g. '2 days | golden hours & photo stops'",
  "intro": "one short spoken opening sentence naming who this trip is for",
  "stops": [
    {{
      "name": "EXACT place name from the candidates",
      "time_label": "e.g. 'Day 1 - Morning' or 'Sunset'",
      "dwell": "how long to spend here, e.g. '1-2 hrs', '45 min', '30 min'",
      "blurb": "<=7 word on-screen caption",
      "narration": "ONE short spoken sentence about this stop for this persona"
    }}
  ],
  "closing": "one short spoken closing sentence"
}}"""


def _compact_places(places: list[dict]) -> list[dict]:
    """Trim the place records to what the model needs (keeps the prompt small)."""
    compact = []
    for p in places:
        compact.append(
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "type": p.get("type"),
                "rating": p.get("rating"),
                "reviews_count": p.get("reviews_count"),
                "about": p.get("description"),
                "reviews": (p.get("reviews") or [])[:2],
            }
        )
    return compact


async def _noop_progress(_: str) -> None:
    return None


# Stages shown on the live trip page, in order.
BUILD_STAGES: list[tuple[str, str]] = [
    ("understanding", "Understanding who this trip is for"),
    ("selecting", "Picking the right Goa spots from real reviews"),
    ("rendering", "Rendering your personalized map video"),
]


def _status_path(trip_id: str):
    return settings.trips_dir / trip_id / "status.json"


def set_status(trip_id: str, **fields) -> None:
    """Merge fields into the trip's status.json (read by the live page)."""
    path = _status_path(trip_id)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    status.update(fields)
    status["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")


def create_trip(chat_id: int) -> TripHandle:
    """Allocate a trip id and its live page immediately, before building."""
    trip_id = uuid.uuid4().hex[:12]
    trip_dir = settings.trips_dir / trip_id
    trip_dir.mkdir(parents=True, exist_ok=True)
    (trip_dir / "meta.json").write_text(
        json.dumps(
            {
                "trip_id": trip_id,
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    set_status(
        trip_id,
        stage="understanding",
        stages=[{"key": k, "label": l} for k, l in BUILD_STAGES],
        done=False,
        error=None,
        title=None,
    )
    url = f"{settings.public_base_url.rstrip('/')}/trip/{trip_id}"
    return TripHandle(trip_id=trip_id, url=url)


PLAYER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:#111827; color:#f5f5f5; min-height:100vh;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; padding:24px; box-sizing:border-box; }
  h1 { margin:0 0 4px; font-size:1.5rem; text-align:center; }
  p.sub { margin:0 0 20px; color:#9ca3af; text-align:center; }
  video { width:min(92vw,420px); aspect-ratio:9/16; border-radius:16px;
          background:#000; box-shadow:0 12px 40px rgba(0,0,0,.5); }
  a.dl { margin-top:18px; color:#ff6f3c; text-decoration:none; font-weight:600; }
</style>
</head>
<body>
  <h1>__TITLE__</h1>
  <p class="sub">__SUBTITLE__</p>
  <video controls autoplay muted playsinline src="__VIDEO_URL__"></video>
  <a class="dl" href="__VIDEO_URL__" download>Download video</a>
</body>
</html>
"""


def _write_player_page(trip_id: str, plan: dict) -> None:
    html = (
        PLAYER_PAGE.replace("__TITLE__", plan.get("title") or "Your Goa Trip")
        .replace("__SUBTITLE__", plan.get("subtitle") or "")
        .replace("__VIDEO_URL__", f"/video/{trip_id}.mp4")
    )
    (settings.trips_dir / trip_id / "index.html").write_text(html, encoding="utf-8")


async def build_trip(
    trip_id: str,
    chat_id: int,
    message: str,
    progress: ProgressCallback = _noop_progress,
) -> TripResult:
    """Run the full build for an already-created trip, updating its live status."""
    try:
        return await _build_trip_inner(trip_id, chat_id, message, progress)
    except Exception as exc:
        set_status(trip_id, error=str(exc)[:300], done=False)
        raise


async def _build_trip_inner(
    trip_id: str,
    chat_id: int,
    message: str,
    progress: ProgressCallback,
) -> TripResult:
    session = store.load_session(chat_id)

    set_status(trip_id, stage="understanding")
    await progress("Understanding who this trip is for...")
    spec = await run_hermes_json(
        INTAKE_PROMPT.format(
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            stored_spec=json.dumps(session.get("spec"), ensure_ascii=False),
            message=message.replace('"', "'"),
        )
    )
    logger.info("intake spec: %s", spec)
    set_status(trip_id, stage="selecting", summary=spec.get("summary"))

    await progress("Finding the right Goa spots and reading reviews...")
    places = await linkup.fetch_goa_places()
    by_name = {p["name"]: p for p in places if p.get("name")}

    days = spec.get("days") or 2
    n_stops = max(4, min(7, days * 3))

    await progress("Designing your personalized itinerary...")
    plan = await run_hermes_json(
        PLAN_PROMPT.format(
            spec=json.dumps(spec, ensure_ascii=False),
            places=json.dumps(_compact_places(places), ensure_ascii=False),
            n_stops=n_stops,
        )
    )

    # Attach real coordinates from our dataset (never trust model-invented coords).
    resolved_stops = []
    for stop in plan.get("stops") or []:
        place = by_name.get(stop.get("name"))
        if not place:
            # tolerate minor name drift by case-insensitive match
            place = next(
                (v for k, v in by_name.items() if k.lower() == str(stop.get("name")).lower()),
                None,
            )
        if not place:
            continue
        resolved_stops.append(
            {
                "name": place["name"],
                "lat": place["lat"],
                "lng": place["lng"],
                "rating": place.get("rating"),
                "thumbnail": place.get("thumbnail"),
                "time_label": stop.get("time_label") or "",
                "dwell": stop.get("dwell") or "",
                "blurb": stop.get("blurb") or place.get("type") or "",
                "narration": stop.get("narration") or "",
            }
        )

    if not resolved_stops:
        raise RuntimeError("no valid stops resolved from the plan")

    plan["stops"] = resolved_stops
    logger.info("plan: %s (%d stops)", plan.get("title"), len(resolved_stops))
    set_status(trip_id, stage="rendering", title=plan.get("title"))

    await progress("Rendering your Goa video...")
    video_path = await video.render_trip_video(plan, trip_id)

    (settings.videos_dir / f"{trip_id}.json").write_text(
        json.dumps(
            {
                "trip_id": trip_id,
                "chat_id": chat_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "spec": spec,
                "plan": plan,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store.save_session(chat_id, spec, message, trip_id)

    _write_player_page(trip_id, plan)
    set_status(trip_id, stage="done", done=True, title=plan.get("title"))

    url = f"{settings.public_base_url.rstrip('/')}/trip/{trip_id}"
    summary = plan.get("title") or spec.get("summary") or "your Goa trip"
    return TripResult(trip_id=trip_id, video_path=str(video_path), url=url, summary=summary)
