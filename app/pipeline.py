"""Persona-aware travel pipeline: a manager-led agent crew that plans a trip
and renders a short map video, with every step recorded to a live run trace."""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app import agents, linkup, photos, store, trace, video
from app.config import settings
from eval import capture

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


class NotTripRequest(Exception):
    """Raised when the intake guardrail decides the message is off-topic."""


OFF_TOPIC_MESSAGE = (
    "I can only help with planning trips - tell me where you're going, "
    "who's travelling and what you love."
)


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


def _resolve_stops(plan: dict, by_name: dict[str, dict]) -> list[dict]:
    """Attach real coordinates from our dataset (never trust model coords)."""
    resolved: list[dict] = []
    for stop in plan.get("stops") or []:
        place = by_name.get(stop.get("name"))
        if not place:
            place = next(
                (
                    v
                    for k, v in by_name.items()
                    if k.lower() == str(stop.get("name")).lower()
                ),
                None,
            )
        if not place:
            continue
        resolved.append(
            {
                "name": place["name"],
                "lat": place["lat"],
                "lng": place["lng"],
                "rating": place.get("rating"),
                "reviews_count": place.get("reviews_count"),
                "type": place.get("type"),
                "address": place.get("address"),
                "description": place.get("description"),
                "reviews": (place.get("reviews") or [])[:2],
                "thumbnail": place.get("thumbnail"),
                "time_label": stop.get("time_label") or "",
                "dwell": stop.get("dwell") or "",
                "blurb": stop.get("blurb") or place.get("type") or "",
                "narration": stop.get("narration") or "",
            }
        )
    return resolved


async def _noop_progress(_: str) -> None:
    return None


# Stages shown on the live trip page, in order.
BUILD_STAGES: list[tuple[str, str]] = [
    ("understanding", "Understanding who this trip is for"),
    ("selecting", "Picking the right spots from real reviews"),
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


TRIP_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root { --bg:#111827; --card:#1f2937; --line:#374151; --muted:#9ca3af;
          --text:#f5f5f5; --accent:#ff6f3c; --star:#ffd65a; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--text); }
  .wrap { max-width:960px; margin:0 auto; padding:28px 18px 60px; }
  header h1 { margin:0 0 4px; font-size:1.7rem; }
  header p.sub { margin:0 0 10px; color:var(--muted); }
  .meta { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:22px; }
  .chip { background:var(--card); border:1px solid var(--line); border-radius:999px;
          padding:5px 14px; font-size:.82rem; color:var(--muted); }
  .chip b { color:var(--text); }
  .cols { display:grid; grid-template-columns:minmax(0,380px) minmax(0,1fr);
          gap:22px; align-items:start; }
  @media (max-width:760px) { .cols { grid-template-columns:1fr; } }
  video { width:100%; aspect-ratio:9/16; border-radius:16px; background:#000;
          box-shadow:0 12px 40px rgba(0,0,0,.5); }
  .links { margin-top:12px; display:flex; gap:18px; flex-wrap:wrap; }
  .links a { color:var(--accent); text-decoration:none; font-weight:600; font-size:.9rem; }
  #map { height:380px; border-radius:16px; border:1px solid var(--line); z-index:0; }
  h2 { font-size:1.15rem; margin:30px 0 14px; }
  .stop { display:flex; gap:14px; background:var(--card); border:1px solid var(--line);
          border-radius:14px; padding:14px; margin-bottom:12px; cursor:pointer; }
  .stop:hover { border-color:var(--accent); }
  .stop img.photo { width:104px; height:104px; object-fit:cover; border-radius:10px; flex:none; }
  .stop .noimg { width:104px; height:104px; border-radius:10px; flex:none;
                 background:linear-gradient(160deg,#ff8a4c,#c43e28); display:flex;
                 align-items:center; justify-content:center; font-weight:700; font-size:1.5rem; }
  .stop .body { min-width:0; flex:1; }
  .stop .top { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
  .stop .num { color:var(--accent); font-weight:700; font-size:.8rem; }
  .stop .when { color:var(--accent); font-size:.72rem; text-transform:uppercase;
                letter-spacing:.05em; font-weight:700; }
  .stop h3 { margin:2px 0 4px; font-size:1.02rem; }
  .stop .facts { color:var(--muted); font-size:.82rem; margin-bottom:6px; }
  .stop .facts b.star { color:var(--star); }
  .stop .desc { color:#d1d5db; font-size:.86rem; margin:0 0 6px; }
  .stop .quote { color:var(--muted); font-size:.8rem; font-style:italic;
                 border-left:3px solid var(--line); padding-left:10px; margin:6px 0 0; }
  .marker-pin { background:var(--accent); color:#fff; border:2px solid #fff;
                border-radius:50%; width:28px; height:28px; display:flex;
                align-items:center; justify-content:center; font-weight:700;
                font-size:13px; box-shadow:0 2px 6px rgba(0,0,0,.5); }
  .refine { margin-top:22px; background:var(--card); border:1px solid var(--line);
            border-radius:14px; padding:14px; }
  .refine h2 { margin:0 0 4px; font-size:1rem; }
  .refine p.hint { margin:0 0 10px; color:var(--muted); font-size:.82rem; }
  .refine textarea { width:100%; min-height:64px; resize:vertical; background:transparent;
                     border:1px solid var(--line); border-radius:10px; padding:10px;
                     outline:none; color:var(--text); font:inherit; font-size:.92rem; }
  .refine textarea:focus { border-color:var(--accent); }
  .refine .row { display:flex; align-items:center; gap:10px; margin-top:10px; }
  .refine .rstatus { color:var(--muted); font-size:.8rem; flex:1; min-height:1.1em; }
  .refine .rstatus.err { color:#f87171; }
  .refine button { background:var(--accent); color:#16100c; border:none;
                   border-radius:10px; padding:10px 20px; font-weight:700;
                   font-size:.92rem; cursor:pointer; }
  .refine button:disabled { opacity:.55; cursor:wait; }
  .suggest { display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }
  .suggest .chip { cursor:pointer; }
  .suggest .chip:hover { border-color:var(--accent); color:var(--text); }
  footer { margin-top:34px; color:var(--muted); font-size:.8rem; text-align:center; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>__TITLE__</h1>
    <p class="sub">__SUBTITLE__</p>
    <div class="meta" id="meta"></div>
  </header>

  <div class="cols">
    <div>
      <video controls autoplay muted playsinline src="__VIDEO_URL__"></video>
      <div class="links">
        <a href="__VIDEO_URL__" download>Download video</a>
        <a href="/runs/__TRIP_ID__">See how the agents built this &rsaquo;</a>
      </div>
      <div class="refine">
        <h2>Edit this trip</h2>
        <p class="hint">Tell the crew what to change - it remembers this trip.</p>
        <form id="rf">
          <textarea id="rmsg" maxlength="1000"
            placeholder="e.g. make it senior-friendly, add a beach day, slower pace"></textarea>
          <div class="row">
            <div class="rstatus" id="rstatus"></div>
            <button type="submit" id="rgo">Rebuild trip</button>
          </div>
        </form>
        <div class="suggest" id="suggest"></div>
      </div>
    </div>
    <div>
      <div id="map"></div>
      <h2>Your itinerary</h2>
      <div id="stops"></div>
    </div>
  </div>

  <footer>Planned by your AI travel desk · built on Hermes</footer>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const plan = __PLAN_JSON__;
const stops = plan.stops || [];

function esc(s){ return (s==null?"":String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// header chips
const meta = [];
if (plan.destination) meta.push(`<span class="chip"><b>${esc(plan.destination)}</b></span>`);
meta.push(`<span class="chip"><b>${stops.length}</b> stops</span>`);
const dwells = stops.filter(s=>s.dwell).length;
if (dwells) meta.push(`<span class="chip">time planned per stop</span>`);
document.getElementById("meta").innerHTML = meta.join("");

// itinerary cards
document.getElementById("stops").innerHTML = stops.map((s, i) => {
  const img = s.thumbnail
    ? `<img class="photo" src="${esc(s.thumbnail)}" alt="${esc(s.name)}" loading="lazy">`
    : `<div class="noimg">${i+1}</div>`;
  const facts = [
    s.rating ? `<b class="star">\u2605 ${esc(s.rating)}</b>` : "",
    s.reviews_count ? `${Number(s.reviews_count).toLocaleString()} reviews` : "",
    s.dwell ? `\u23F1 ${esc(s.dwell)}` : "",
    s.address ? esc(s.address) : "",
  ].filter(Boolean).join(" · ");
  const quote = (s.reviews && s.reviews[0]) ? `<p class="quote">\u201C${esc(s.reviews[0])}\u201D</p>` : "";
  return `<div class="stop" data-i="${i}">
    ${img}
    <div class="body">
      <div class="top"><span class="num">STOP ${i+1}</span>
        ${s.time_label ? `<span class="when">${esc(s.time_label)}</span>` : ""}</div>
      <h3>${esc(s.name)}</h3>
      ${facts ? `<div class="facts">${facts}</div>` : ""}
      <p class="desc">${esc(s.description || s.blurb || "")}</p>
      ${s.narration ? `<p class="desc" style="color:var(--muted)">${esc(s.narration)}</p>` : ""}
      ${quote}
    </div>
  </div>`;
}).join("");

// map with numbered markers + route line
const withCoords = stops.filter(s => s.lat != null && s.lng != null);
if (withCoords.length) {
  const map = L.map("map", { scrollWheelZoom: false });
  L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 19 }).addTo(map);
  const latlngs = withCoords.map(s => [s.lat, s.lng]);
  L.polyline(latlngs, { color: "#ff6f3c", weight: 4, opacity: .85 }).addTo(map);
  const markers = withCoords.map((s, i) => {
    const icon = L.divIcon({ className: "", html: `<div class="marker-pin">${i+1}</div>`,
      iconSize: [28, 28], iconAnchor: [14, 14] });
    return L.marker([s.lat, s.lng], { icon }).addTo(map)
      .bindPopup(`<b>${esc(s.name)}</b><br>${esc(s.time_label || "")}`);
  });
  map.fitBounds(L.latLngBounds(latlngs).pad(0.18));
  document.querySelectorAll(".stop").forEach(el => el.onclick = () => {
    const i = stops.indexOf(stops[el.dataset.i]);
    const j = withCoords.indexOf(stops[el.dataset.i]);
    if (j >= 0) { map.setView([withCoords[j].lat, withCoords[j].lng], 14); markers[j].openPopup(); }
    document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
  });
} else {
  document.getElementById("map").style.display = "none";
}

// edit box: a follow-up message rebuilds the trip using its stored memory
const tripId = "__TRIP_ID__";
const SUGGESTIONS = [
  "Make it senior-friendly", "Add one more day", "More food stops",
  "Slower pace, fewer stops", "Add sunset spots",
];
const sug = document.getElementById("suggest");
sug.innerHTML = SUGGESTIONS.map(s => `<span class="chip">${s}</span>`).join("");
sug.querySelectorAll(".chip").forEach((c, i) => c.onclick = () => {
  const t = document.getElementById("rmsg");
  t.value = SUGGESTIONS[i];
  t.focus();
});

const rform = document.getElementById("rf");
const rstatus = document.getElementById("rstatus");
rform.onsubmit = async (ev) => {
  ev.preventDefault();
  const message = document.getElementById("rmsg").value.trim();
  if (!message) { rstatus.textContent = "Tell me what to change first."; return; }
  const btn = document.getElementById("rgo");
  btn.disabled = true;
  rstatus.className = "rstatus";
  rstatus.textContent = "Rebuilding your trip\u2026";
  try {
    const res = await fetch(`/api/trip/${tripId}/refine`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(
      typeof data.detail === "string" ? data.detail : "Something went wrong");
    location.href = data.url;
  } catch (e) {
    rstatus.className = "rstatus err";
    rstatus.textContent = e.message;
    btn.disabled = false;
  }
};
document.getElementById("rmsg").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); rform.requestSubmit(); }
});
</script>
</body>
</html>
"""


def _write_player_page(trip_id: str, plan: dict) -> None:
    # </ -> <\/ keeps the inlined JSON from closing the <script> tag early.
    plan_json = json.dumps(plan, ensure_ascii=False).replace("</", "<\\/")
    html = (
        TRIP_PAGE.replace("__TITLE__", plan.get("title") or "Your Trip")
        .replace("__SUBTITLE__", plan.get("subtitle") or "")
        .replace("__VIDEO_URL__", f"/video/{trip_id}.mp4")
        .replace("__TRIP_ID__", trip_id)
        .replace("__PLAN_JSON__", plan_json)
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
    except NotTripRequest:
        set_status(trip_id, error=OFF_TOPIC_MESSAGE, done=False)
        raise
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
    stored_spec = session.get("spec")
    # Memory: the last few raw messages go into the intake prompt so wording
    # like "same trip but slower" resolves against what was actually said.
    history = (session.get("history") or [])[-5:]
    trace.start_trace(trip_id)

    # The Trip Director (manager) wraps the whole run; every specialist span
    # nests under it, so the trace reads as "manager -> specialists".
    async with trace.span("Trip Director", "Run the travel desk", kind="group") as root:
        set_status(trip_id, stage="understanding")
        await progress("Understanding who this trip is for...")
        spec = await agents.intake_analyst(
            message,
            stored_spec,
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            settings.default_destination,
            history=history,
        )
        logger.info("intake spec: %s", spec)

        # Guardrail: Hermes only plans trips. Anything else stops here, before
        # any place search, specialist crew or video render is spent on it.
        if spec.get("is_trip_request") is False:
            reason = spec.get("rejection_reason") or "not a trip-planning request"
            logger.info("rejected off-topic request for chat %s: %s", chat_id, reason)
            capture.capture_case(message, reason="off_topic", extra={"detail": reason})
            raise NotTripRequest(reason)

        destination = (spec.get("destination") or settings.default_destination).strip()
        set_status(
            trip_id,
            stage="selecting",
            summary=spec.get("summary"),
            destination=destination,
        )

        await progress(f"Researching real spots in {destination}...")
        async with trace.span(
            "Place Researcher", f"Live search for places in {destination}", kind="tool"
        ) as sp:
            places = await linkup.fetch_places(destination)
            sp.note(source="Linkup", places_found=len(places))
            sp.set_output(f"{len(places)} candidate places in {destination}")
        by_name = {p["name"]: p for p in places if p.get("name")}

        days = spec.get("days") or 2
        per_day = {"relaxed": 2, "packed": 4}.get(spec.get("pace"), 3)
        n_stops = max(3, min(7, days * per_day))
        compact = _compact_places(places)

        # Trip Director composes a request-specific crew of persona specialists
        # (invented roles + briefs), then runs them concurrently to advise the
        # planner. Which specialists exist depends entirely on this traveller.
        await progress("Trip Director is assembling your specialist crew...")
        decision = await agents.compose_crew(spec)
        crew = decision.get("crew") or []
        root.note(
            crew=[f"{c['role']} ({c['capability']})" for c in crew],
            needs_accessibility_review=decision.get("needs_accessibility_review"),
            reasoning=decision.get("reasoning"),
        )
        set_status(trip_id, manager_plan=decision)

        specialist_results = await agents.run_crew_specialists(crew, spec, compact)
        guidance = agents.format_specialist_guidance(specialist_results)

        # A specialist that could not satisfy its brief escalates; the manager
        # notes the blockers and tells the planner to work around them.
        blockers = [
            {"role": r.get("role"), "blocker": r.get("blocker")}
            for r in specialist_results
            if r.get("status") == "blocked" and r.get("blocker")
        ]
        if blockers:
            async with trace.span(
                "Trip Director", "Resolve specialist escalations", kind="group"
            ) as sp:
                sp.note(blockers=blockers, resolution="proceed best-effort")
            guidance += "\nKnown blockers to work around: " + "; ".join(
                f"{b['role']}: {b['blocker']}" for b in blockers
            )

        await progress("Designing your personalized itinerary...")
        plan = await agents.itinerary_planner(
            destination, spec, compact, n_stops, guidance=guidance
        )
        resolved_stops = _resolve_stops(plan, by_name)

        # Manager review step: dynamic - only when this request needs it. The
        # reviewer can bounce the itinerary back to the planner for one revision.
        needs_review = bool(
            decision.get("needs_accessibility_review")
        ) or agents.spec_has_mobility_limits(spec)
        if needs_review and resolved_stops:
            review = await agents.accessibility_reviewer(spec, resolved_stops)
            set_status(trip_id, review=review)
            if review.get("verdict") == "revise" and review.get("revision_notes"):
                await progress("Trip Director sent the plan back for a revision...")
                # Closed-loop eval: a real quality failure becomes a new eval case.
                capture.capture_case(
                    message,
                    reason="accessibility_revision",
                    destination=destination,
                    spec=spec,
                    extra={"issues": review.get("issues")},
                )
                plan = await agents.itinerary_planner(
                    destination,
                    spec,
                    compact,
                    n_stops,
                    guidance=guidance,
                    revision_notes=review["revision_notes"],
                )
                resolved_stops = _resolve_stops(plan, by_name) or resolved_stops

        if not resolved_stops:
            capture.capture_case(
                message, reason="no_stops_resolved",
                destination=destination, spec=spec,
            )
            raise RuntimeError("no valid stops resolved from the plan")

        # Fill in a real photo per chosen stop (SerpAPI Google Maps, Wikipedia
        # fallback), so the video is photo-led instead of map-only.
        async with trace.span(
            "Place Researcher", "Fetch a real photo per stop", kind="tool"
        ) as sp:
            await photos.attach_photos(resolved_stops, destination)
            # Write photos back into the places cache: rebuilds of the same
            # trip (or trips reusing these places) then skip SerpAPI entirely.
            linkup.save_thumbnails(destination, resolved_stops)
            sp.note(stops=len(resolved_stops))

        plan["stops"] = resolved_stops
        plan["destination"] = destination
        logger.info("plan: %s (%d stops)", plan.get("title"), len(resolved_stops))
        set_status(trip_id, stage="rendering", title=plan.get("title"))

        await progress("Rendering your video...")
        async with trace.span(
            "Video Producer", "Render narrated map video", kind="tool"
        ) as sp:
            video_path = await video.render_trip_video(plan, trip_id)
            sp.note(stops=len(resolved_stops))
            sp.set_output(str(video_path))

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
    summary = plan.get("title") or spec.get("summary") or "your trip"
    return TripResult(trip_id=trip_id, video_path=str(video_path), url=url, summary=summary)
