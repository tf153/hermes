"""FastAPI app: serves trip pages, runs the Telegram bot, cleans up expired trips."""

import asyncio
import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.bot import build_application
from app.config import settings

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
    return FileResponse(path, media_type="video/mp4", filename=f"goa-{trip_id}.mp4")


BUILDING_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Building your Goa trip…</title>
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
  <h1>Building your Goa trip…</h1>
  <p class="sub" id="summary">This page updates live - keep it open.</p>
  <ul id="stages"></ul>
  <div class="err" id="err"></div>
  <div class="hint">Usually ready in 2-3 minutes. The video appears here automatically.</div>
</div>
<script>
const tripId = "__TRIP_ID__";
async function poll() {
  try {
    const res = await fetch(`/trip/${tripId}/status`, {cache: "no-store"});
    if (!res.ok) return;
    const s = await res.json();
    if (s.done) { location.reload(); return; }
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
      el.textContent = "Build failed: " + s.error + " - ask the bot to try again.";
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
