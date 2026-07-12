"""FastAPI app: landing page, trip pages, the Telegram bot, and trip cleanup."""

import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app import store
from app.bot import build_application
from app.config import settings
from app.pipeline import build_trip, create_trip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600


def _trip_created_at(trip_dir: Path) -> datetime | None:
    try:
        meta = json.loads((trip_dir / "meta.json").read_text(encoding="utf-8"))
        return datetime.fromisoformat(meta["created_at"])
    except (OSError, KeyError, ValueError):
        return None


def _is_expired(trip_dir: Path) -> bool:
    created_at = _trip_created_at(trip_dir)
    if created_at is None:
        return True
    ttl = timedelta(hours=settings.trip_ttl_hours)
    return datetime.now(timezone.utc) - created_at > ttl


def _video_expired(path: Path) -> bool:
    ttl = timedelta(hours=settings.trip_ttl_hours)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return datetime.now(timezone.utc) - mtime > ttl


async def _cleanup_loop() -> None:
    while True:
        for trip_dir in settings.trips_dir.iterdir():
            if trip_dir.is_dir() and _is_expired(trip_dir):
                logger.info("removing expired trip %s", trip_dir.name)
                shutil.rmtree(trip_dir, ignore_errors=True)
        for path in settings.videos_dir.glob("*"):
            if path.is_file() and _video_expired(path):
                logger.info("removing expired video %s", path.name)
                path.unlink(missing_ok=True)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())

    telegram_app = None
    if settings.telegram_bot_token:
        telegram_app = build_application()
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set - running API only. "
            "Add the token to .env and restart to enable the bot."
        )

    yield

    cleanup_task.cancel()
    if telegram_app is not None:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram bot stopped")


app = FastAPI(title="Hermes Travel Backend", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "bot_configured": bool(settings.telegram_bot_token),
        "linkup_configured": bool(settings.linkup_api_key),
        "serpapi_configured": bool(settings.serpapi_api_key),
    }


# ------------------------------ landing page ------------------------------

LANDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Travel - one message, a whole trip</title>
<style>
  :root { --bg:#0f1420; --card:#1a2231; --line:#273246; --muted:#8b97ab;
          --text:#eef2f8; --accent:#ff6f3c; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         color:var(--text); min-height:100vh; display:flex; align-items:center;
         justify-content:center; padding:24px;
         background:radial-gradient(1100px 500px at 85% -10%, #2a1a12 0%, transparent 55%),
                    radial-gradient(900px 500px at -10% 110%, #14203a 0%, transparent 55%),
                    var(--bg); }
  .hero { width:min(94vw, 640px); }
  .kicker { color:var(--accent); font-weight:700; letter-spacing:.09em;
            font-size:.78rem; text-transform:uppercase; margin-bottom:10px; }
  h1 { margin:0 0 10px; font-size:clamp(1.7rem, 4.5vw, 2.5rem); line-height:1.15; }
  p.lead { margin:0 0 26px; color:var(--muted); font-size:1.02rem; line-height:1.55; }
  form { background:var(--card); border:1px solid var(--line); border-radius:18px;
         padding:16px; box-shadow:0 18px 60px rgba(0,0,0,.45); }
  textarea { width:100%; min-height:88px; resize:vertical; background:transparent;
             border:none; outline:none; color:var(--text); font:inherit;
             font-size:1rem; line-height:1.5; }
  textarea::placeholder { color:#5c6a80; }
  .row { display:flex; align-items:center; gap:12px; margin-top:10px; }
  .row .spacer { flex:1; }
  button { background:var(--accent); color:#16100c; border:none; border-radius:12px;
           padding:12px 26px; font-size:1rem; font-weight:700; cursor:pointer; }
  button:disabled { opacity:.55; cursor:wait; }
  .status { color:var(--muted); font-size:.85rem; min-height:1.1em; }
  .status.err { color:#f87171; }
  .examples { display:flex; flex-wrap:wrap; gap:8px; margin-top:18px; }
  .chip { background:var(--card); border:1px solid var(--line); color:var(--muted);
          border-radius:999px; padding:7px 14px; font-size:.82rem; cursor:pointer; }
  .chip:hover { border-color:var(--accent); color:var(--text); }
  .how { display:flex; gap:18px; flex-wrap:wrap; margin-top:30px; }
  .step { flex:1; min-width:150px; color:var(--muted); font-size:.82rem; line-height:1.45; }
  .step b { display:block; color:var(--text); font-size:.88rem; margin-bottom:3px; }
  .step .n { color:var(--accent); font-weight:700; }
  footer { margin-top:30px; color:var(--muted); font-size:.8rem; }
  footer a { color:var(--accent); text-decoration:none; font-weight:600; }
</style>
</head>
<body>
<div class="hero">
  <div class="kicker">Hermes Travel</div>
  <h1>One message. A whole trip, planned.</h1>
  <p class="lead">Say where you're going, who's travelling and what you love.
  An AI crew researches real places, designs your itinerary and renders a
  narrated map video - live, on one page.</p>

  <form id="f">
    <textarea id="msg" maxlength="1000"
      placeholder="e.g. 2 days in Goa, I'm a sunset chaser and love photography"></textarea>
    <div class="row">
      <div class="status" id="status">Follow-up messages refine your last trip.</div>
      <div class="spacer"></div>
      <button type="submit" id="go">Plan my trip</button>
    </div>
  </form>

  <div class="examples" id="examples"></div>

  <div class="how">
    <div class="step"><span class="n">1</span> <b>Tell us the trip</b>
      Destination, days, personas, constraints - plain language.</div>
    <div class="step"><span class="n">2</span> <b>Watch it build</b>
      You get a link instantly; the page updates live as agents work.</div>
    <div class="step"><span class="n">3</span> <b>Get the plan</b>
      Itinerary with real photos, map and a ~1-min narrated video.</div>
  </div>

  <footer>Also on Telegram:
    <a href="https://t.me/hermes_smart_travel_bot">@hermes_smart_travel_bot</a>
    &nbsp;·&nbsp; <a href="#" id="reset">Start a fresh trip</a>
  </footer>
</div>

<script>
const EXAMPLES = [
  "2 days in Goa, I'm a sunset chaser and love photography",
  "Family trip to Jaipur with 2 young kids, nothing too tiring",
  "Parents coming to Varanasi, no stairs - temples + easy sights",
  "Solo slow traveller in Pondicherry, just cafes and quiet mornings",
];
const ex = document.getElementById("examples");
ex.innerHTML = EXAMPLES.map(e => `<span class="chip">${e}</span>`).join("");
ex.querySelectorAll(".chip").forEach((c, i) => c.onclick = () => {
  document.getElementById("msg").value = EXAMPLES[i];
  document.getElementById("msg").focus();
});

// Stable per-browser id so follow-up messages refine the same trip spec.
let clientId = localStorage.getItem("hermes_client_id");
if (!clientId) {
  clientId = String(1e15 + Math.floor(Math.random() * 9e15));
  localStorage.setItem("hermes_client_id", clientId);
}

const form = document.getElementById("f");
const status = document.getElementById("status");
form.onsubmit = async (ev) => {
  ev.preventDefault();
  const message = document.getElementById("msg").value.trim();
  if (!message) { status.textContent = "Tell me about the trip first."; return; }
  const btn = document.getElementById("go");
  btn.disabled = true;
  status.className = "status";
  status.textContent = "Starting your trip build\u2026";
  try {
    const res = await fetch("/api/trip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, client_id: Number(clientId) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(
      typeof data.detail === "string" ? data.detail : "Something went wrong");
    location.href = data.url;
  } catch (e) {
    status.className = "status err";
    status.textContent = e.message;
    btn.disabled = false;
  }
};
document.getElementById("msg").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
document.getElementById("reset").onclick = async (e) => {
  e.preventDefault();
  await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: Number(clientId) }),
  });
  status.className = "status";
  status.textContent = "Context cleared - tell me about your next trip!";
};
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    return HTMLResponse(LANDING_PAGE)


class TripRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    client_id: int = Field(gt=0)


class RefineRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


# web sessions with a build currently running (mirrors the bot's guard)
_active_web_clients: set[int] = set()
_web_tasks: set[asyncio.Task] = set()


def _launch_web_build(chat_id: int, message: str) -> JSONResponse:
    """Start a background build for a web request and return the live URL."""
    message = message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Tell me about the trip first")
    if chat_id in _active_web_clients:
        raise HTTPException(
            status_code=409,
            detail="I'm still building your previous trip - one moment!",
        )

    _active_web_clients.add(chat_id)
    handle = create_trip(chat_id)

    async def _build() -> None:
        try:
            await build_trip(handle.trip_id, chat_id, message)
        except Exception:
            logger.exception("web trip build failed for %s", handle.trip_id)
        finally:
            _active_web_clients.discard(chat_id)

    task = asyncio.create_task(_build())
    _web_tasks.add(task)
    task.add_done_callback(_web_tasks.discard)

    return JSONResponse({"trip_id": handle.trip_id, "url": handle.url})


@app.post("/api/trip")
async def start_trip(req: TripRequest) -> JSONResponse:
    """Web equivalent of a Telegram message: instant URL, background build."""
    return _launch_web_build(req.client_id, req.message)


@app.post("/api/trip/{trip_id}/refine")
async def refine_trip(trip_id: str, req: RefineRequest) -> JSONResponse:
    """Edit an existing trip: reuse its session (memory) and build a new one."""
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Trip not found")
    meta_path = settings.trips_dir / trip_id / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Trip not found")
    try:
        chat_id = int(json.loads(meta_path.read_text(encoding="utf-8"))["chat_id"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="Trip not found")
    return _launch_web_build(chat_id, req.message)


class ResetRequest(BaseModel):
    client_id: int = Field(gt=0)


@app.post("/api/reset")
async def reset_web_session(req: ResetRequest) -> JSONResponse:
    """Clear the stored spec for a web client (like /reset in Telegram)."""
    store.reset_session(req.client_id)
    return JSONResponse({"ok": True})


@app.get("/video/{trip_id}.mp4")
async def get_video(trip_id: str) -> FileResponse:
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Video not found")
    path = settings.videos_dir / f"{trip_id}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    if _video_expired(path):
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=410, detail="This video has expired")
    return FileResponse(path, media_type="video/mp4", filename=f"trip-{trip_id}.mp4")


BUILDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Building your trip…</title>
<style>
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:#111827; color:#f5f5f5; min-height:100vh;
         display:flex; align-items:center; justify-content:center;
         padding:24px; box-sizing:border-box; }
  .card { width:min(92vw,420px); background:#1f2937; border-radius:16px;
          padding:28px; box-shadow:0 12px 40px rgba(0,0,0,.5); }
  h1 { margin:0 0 6px; font-size:1.3rem; }
  p.sub { margin:0 0 22px; color:#9ca3af; font-size:.95rem; min-height:1.2em; }
  ul { list-style:none; margin:0; padding:0; }
  li { display:flex; align-items:center; gap:12px; padding:10px 0;
       color:#6b7280; transition:color .3s; }
  li .dot { width:22px; height:22px; border-radius:50%; flex:none;
            border:2px solid #374151; box-sizing:border-box; display:flex;
            align-items:center; justify-content:center; font-size:13px; }
  li.active { color:#f5f5f5; }
  li.active .dot { border-color:#ff6f3c; }
  li.active .dot::after { content:''; width:10px; height:10px; border-radius:50%;
            background:#ff6f3c; animation:pulse 1.2s infinite; }
  li.done { color:#d1d5db; }
  li.done .dot { border-color:#22c55e; background:#22c55e; color:#111827; }
  li.done .dot::before { content:'\\2713'; font-weight:700; }
  @keyframes pulse { 0%,100%{transform:scale(.7);opacity:.6} 50%{transform:scale(1);opacity:1} }
  .err { margin-top:18px; color:#f87171; font-size:.9rem; display:none; }
  .hint { margin-top:22px; color:#6b7280; font-size:.8rem; }
</style>
</head>
<body>
<div class="card">
  <h1 id="title">Building your trip…</h1>
  <p class="sub" id="summary">This page updates live - keep it open.</p>
  <ul id="stages"></ul>
  <div class="err" id="err"></div>
  <div class="hint">Usually ready in 2-3 minutes. The video appears here automatically.</div>
  <div class="hint"><a href="/runs/__TRIP_ID__" style="color:#ff6f3c;text-decoration:none;font-weight:600;">Watch the agents work live &rsaquo;</a></div>
</div>
<script>
const tripId = "__TRIP_ID__";
async function poll() {
  try {
    const res = await fetch(`/trip/${tripId}/status`, {cache: "no-store"});
    if (!res.ok) return;
    const s = await res.json();
    if (s.done) { location.reload(); return; }
    if (s.destination) {
      const city = s.destination.split(",")[0].trim();
      document.getElementById("title").textContent = `Building your ${city} trip…`;
    }
    if (s.summary) document.getElementById("summary").textContent = s.summary;
    const stages = s.stages || [];
    const idx = stages.findIndex(st => st.key === s.stage);
    document.getElementById("stages").innerHTML = stages.map((st, i) => {
      const cls = i < idx ? "done" : (i === idx ? "active" : "");
      return `<li class="${cls}"><span class="dot"></span>${st.label}</li>`;
    }).join("");
    if (s.error) {
      const el = document.getElementById("err");
      el.style.display = "block";
      el.textContent = "Build failed: " + s.error + " - send a new request to try again.";
    }
  } catch (e) { /* transient network error; keep polling */ }
}
poll();
setInterval(poll, 2500);
</script>
</body>
</html>
"""


@app.get("/trip/{trip_id}", response_class=HTMLResponse)
async def get_trip(trip_id: str) -> HTMLResponse:
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Trip not found")

    trip_dir = settings.trips_dir / trip_id
    if not trip_dir.exists():
        raise HTTPException(status_code=404, detail="Trip not found")

    if _is_expired(trip_dir):
        shutil.rmtree(trip_dir, ignore_errors=True)
        raise HTTPException(status_code=410, detail="This trip page has expired")

    index = trip_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))

    if (trip_dir / "status.json").exists():
        return HTMLResponse(BUILDING_PAGE.replace("__TRIP_ID__", trip_id))

    raise HTTPException(status_code=404, detail="Trip not found")


@app.get("/trip/{trip_id}/status")
async def get_trip_status(trip_id: str) -> JSONResponse:
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Trip not found")
    status_path = settings.trips_dir / trip_id / "status.json"
    if not status_path.exists():
        raise HTTPException(status_code=404, detail="Trip not found")
    return JSONResponse(json.loads(status_path.read_text(encoding="utf-8")))


# --------------------------- agent run observability ---------------------------


@app.get("/trip/{trip_id}/trace")
async def get_trip_trace(trip_id: str) -> JSONResponse:
    """Raw agent trace for one run (who called whom, tokens, cost, latency)."""
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Trace not found")
    trace_path = settings.trips_dir / trip_id / "trace.json"
    if not trace_path.exists():
        raise HTTPException(status_code=404, detail="Trace not found")
    return JSONResponse(json.loads(trace_path.read_text(encoding="utf-8")))


@app.get("/runs")
async def list_runs() -> JSONResponse:
    """Recent agent runs, newest first (id, title, totals) for the run index."""
    runs = []
    for trip_dir in settings.trips_dir.iterdir():
        trace_path = trip_dir / "trace.json"
        if not trip_dir.is_dir() or not trace_path.exists():
            continue
        try:
            data = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        title = None
        try:
            status = json.loads((trip_dir / "status.json").read_text(encoding="utf-8"))
            title = status.get("title") or status.get("summary")
        except (OSError, json.JSONDecodeError):
            pass
        runs.append(
            {
                "trip_id": trip_dir.name,
                "title": title,
                "started_at": data.get("started_at"),
                "updated_at": data.get("updated_at"),
                "totals": data.get("totals", {}),
            }
        )
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return JSONResponse({"runs": runs})


RUN_VIEWER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent run · __TRIP_ID__</title>
<style>
  :root { --bg:#0f1420; --card:#1a2231; --line:#273246; --muted:#8b97ab;
          --text:#eef2f8; --accent:#ff6f3c; --ok:#22c55e; --run:#f5b942; --err:#f87171; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
         background:var(--bg); color:var(--text); padding:22px; }
  h1 { font-size:1.25rem; margin:0 0 2px; }
  .sub { color:var(--muted); font-size:.85rem; margin-bottom:18px; }
  .sub a { color:var(--accent); text-decoration:none; }
  .totals { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:10px 16px; min-width:104px; }
  .stat .v { font-size:1.25rem; font-weight:700; }
  .stat .l { color:var(--muted); font-size:.72rem; text-transform:uppercase;
             letter-spacing:.04em; }
  .filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; align-items:center; }
  .chip { background:var(--card); border:1px solid var(--line); color:var(--text);
          border-radius:999px; padding:5px 13px; font-size:.8rem; cursor:pointer; }
  .chip.active { background:var(--accent); border-color:var(--accent); color:#111; font-weight:600; }
  .span { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:12px 14px; margin:8px 0; }
  .span.child { margin-left:26px; border-left:2px solid var(--accent); }
  .row { display:flex; align-items:center; gap:10px; cursor:pointer; }
  .dot { width:10px; height:10px; border-radius:50%; flex:none; background:var(--muted); }
  .dot.ok { background:var(--ok); } .dot.running { background:var(--run);
            animation:pulse 1s infinite; } .dot.error { background:var(--err); }
  @keyframes pulse { 0%,100%{opacity:.4} 50%{opacity:1} }
  .agent { font-weight:700; }
  .task { color:var(--muted); font-size:.85rem; }
  .badge { font-size:.66rem; text-transform:uppercase; letter-spacing:.04em;
           border:1px solid var(--line); border-radius:6px; padding:2px 7px; color:var(--muted); }
  .spacer { flex:1; }
  .metric { font-variant-numeric:tabular-nums; color:var(--muted); font-size:.8rem; }
  .metric b { color:var(--text); font-weight:600; }
  .detail { margin-top:10px; border-top:1px solid var(--line); padding-top:10px;
            display:none; font-size:.82rem; }
  .detail.open { display:block; }
  .detail pre { white-space:pre-wrap; word-break:break-word; background:#0b101a;
                border:1px solid var(--line); border-radius:8px; padding:10px;
                margin:6px 0 0; color:#cfd8e6; max-height:230px; overflow:auto; }
  .detail .k { color:var(--muted); }
  .empty { color:var(--muted); padding:30px 0; text-align:center; }
</style>
</head>
<body>
  <h1>Agent run</h1>
  <div class="sub">Trip <b>__TRIP_ID__</b> · <a href="/trip/__TRIP_ID__">open trip page</a> · <span id="live">live</span></div>
  <div class="totals" id="totals"></div>
  <div class="filters" id="filters"></div>
  <div id="spans"></div>
<script>
const tripId = "__TRIP_ID__";
let activeAgent = null;
let openIds = new Set();

function fmtDur(ms){ if(ms==null) return "—"; return ms<1000 ? ms+"ms" : (ms/1000).toFixed(1)+"s"; }
function fmtCost(c){ return c ? "$"+Number(c).toFixed(4) : "$0"; }
function esc(s){ return (s==null?"":String(s)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function render(data){
  const t = data.totals || {};
  document.getElementById("totals").innerHTML = [
    ["v", (t.agents||[]).length, "agents"],
    ["v", t.llm_calls||0, "LLM calls"],
    ["v", (t.tokens||0).toLocaleString(), "est. tokens"],
    ["v", fmtCost(t.cost_usd), "est. cost"],
    ["v", fmtDur(t.duration_ms), "wall clock"],
  ].map(([_,v,l])=>`<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  const agents = t.agents || [];
  const chips = ['<span class="chip'+(activeAgent===null?' active':'')+'" data-a="">All agents</span>']
    .concat(agents.map(a=>`<span class="chip${activeAgent===a?' active':''}" data-a="${esc(a)}">${esc(a)}</span>`));
  const f = document.getElementById("filters");
  f.innerHTML = chips.join("");
  f.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{ activeAgent = c.dataset.a || null; render(lastData); });

  const spans = (data.spans||[]).filter(s=>!activeAgent || s.agent===activeAgent);
  const box = document.getElementById("spans");
  if(!spans.length){ box.innerHTML = '<div class="empty">Waiting for the crew to start…</div>'; return; }
  box.innerHTML = spans.map(s=>{
    const child = s.parent_id ? " child" : "";
    const meta = s.meta ? Object.entries(s.meta).map(([k,v])=>`<div><span class="k">${esc(k)}:</span> ${esc(typeof v==='object'?JSON.stringify(v):v)}</div>`).join("") : "";
    const open = openIds.has(s.id) ? " open" : "";
    const model = s.model ? `<span class="badge">${esc(s.model)}</span>` : "";
    return `<div class="span${child}">
      <div class="row" data-id="${s.id}">
        <span class="dot ${s.status}"></span>
        <span class="agent">${esc(s.agent)}</span>
        <span class="task">${esc(s.task)}</span>
        <span class="badge">${esc(s.kind)}</span>${model}
        <span class="spacer"></span>
        <span class="metric"><b>${fmtDur(s.duration_ms)}</b></span>
        <span class="metric">${(s.tokens||0).toLocaleString()} tok</span>
        <span class="metric">${fmtCost(s.cost_usd)}</span>
      </div>
      <div class="detail${open}" id="d-${s.id}">
        ${meta}
        ${s.input_preview?`<div class="k">input</div><pre>${esc(s.input_preview)}</pre>`:""}
        ${s.output_preview?`<div class="k">output</div><pre>${esc(s.output_preview)}</pre>`:""}
        ${s.error?`<div class="k">error</div><pre>${esc(s.error)}</pre>`:""}
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll(".row").forEach(r=>r.onclick=()=>{
    const id=r.dataset.id; const d=document.getElementById("d-"+id);
    d.classList.toggle("open");
    if(d.classList.contains("open")) openIds.add(id); else openIds.delete(id);
  });
}

let lastData = {spans:[],totals:{}};
async function poll(){
  try{
    const res = await fetch(`/trip/${tripId}/trace`, {cache:"no-store"});
    if(res.ok){ lastData = await res.json(); render(lastData); }
  }catch(e){}
}
poll(); setInterval(poll, 1500);
</script>
</body>
</html>
"""


@app.get("/runs/{trip_id}", response_class=HTMLResponse)
async def run_viewer(trip_id: str) -> HTMLResponse:
    """Live trace viewer for one run - judges watch the crew work step by step."""
    if not trip_id.isalnum():
        raise HTTPException(status_code=404, detail="Run not found")
    return HTMLResponse(RUN_VIEWER_PAGE.replace("__TRIP_ID__", trip_id))
