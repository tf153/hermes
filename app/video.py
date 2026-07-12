"""Render a colorful, photo-led portrait map video for a personalized Goa plan.

Each stop shows a real photo of the place, how long to spend there, its rating
and a one-line caption, narrated by ElevenLabs. A route-map overview ties them
together. Segments are rendered to stills, turned into Ken-Burns clips with
muxed audio, then concatenated.
"""

import asyncio
import io
import logging
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from staticmap import CircleMarker, Line, StaticMap

from app import tts
from app.config import settings

logger = logging.getLogger(__name__)

TILE_URL = "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
TILE_HEADERS = {"User-Agent": "HermesGoaTravel/1.0 (+https://growthx.club)"}
FPS = 30

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
_FONT_REG = FONT_DIR / "DejaVuSans.ttf"
_FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"

ACCENT = (255, 111, 60)      # warm orange
NAVY = (12, 17, 29)
LIGHT = (248, 249, 251)


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT_BOLD if bold else _FONT_REG), size)


# ----------------------------- images -----------------------------

def _hires(url: str) -> str:
    return re.sub(r"=w\d+-h\d+(-[a-z0-9-]+)?$", "=w1200-h1600", url) if url else url


def _download_image(url: str | None) -> Image.Image | None:
    if not url:
        return None
    for candidate in (_hires(url), url):
        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                resp = client.get(candidate)
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
        except Exception:  # noqa: BLE001 - any failure -> try next / None
            continue
    return None


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = max(int(iw * scale + 0.5), w), max(int(ih * scale + 0.5), h)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _gradient_bg(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    base = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(h - 1, 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base


def _shade(img: Image.Image, w: int, h: int) -> Image.Image:
    """Darken top and bottom of a photo so overlaid text stays legible."""
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    top_h = int(h * 0.16)
    for y in range(top_h):
        a = int(150 * (1 - y / top_h))
        draw.line([(0, y), (w, y)], fill=(*NAVY, a))
    bot_start = int(h * 0.48)
    for y in range(bot_start, h):
        a = int(238 * (y - bot_start) / (h - bot_start))
        draw.line([(0, y), (w, y)], fill=(*NAVY, min(a, 240)))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font, fill, text_fill, pad_x=22, h=56) -> int:
    tw = draw.textlength(text, font=font)
    draw.rounded_rectangle([x, y, x + tw + pad_x * 2, y + h], radius=h // 2, fill=fill)
    draw.text((x + pad_x, y + (h - font.size) // 2 - 2), text, font=font, fill=text_fill)
    return int(x + tw + pad_x * 2)


def _dwell_pill(draw: ImageDraw.ImageDraw, right_x: int, y: int, text: str, font, h=56) -> None:
    """Right-aligned white pill with a drawn clock icon + dwell time."""
    icon, gap, pad = 28, 12, 22
    tw = draw.textlength(text, font=font)
    pill_w = int(pad + icon + gap + tw + pad)
    x = right_x - pill_w
    draw.rounded_rectangle([x, y, x + pill_w, y + h], radius=h // 2, fill=(255, 255, 255, 240))
    cx, cy, r = x + pad + icon / 2, y + h / 2, icon / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=NAVY, width=3)
    draw.line([cx, cy, cx, cy - r * 0.55], fill=NAVY, width=3)
    draw.line([cx, cy, cx + r * 0.5, cy], fill=NAVY, width=3)
    draw.text((x + pad + icon + gap, y + (h - font.size) // 2 - 2), text, font=font, fill=NAVY)


# ----------------------------- map rendering -----------------------------

def _lonlat_to_world(lon: float, lat: float, scale: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * scale
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    return x, y


def _fit_zoom(stops: list[dict], w: int, h: int, pad: int = 110, tile: int = 256) -> tuple[int, tuple[float, float]]:
    lats = [s["lat"] for s in stops]
    lngs = [s["lng"] for s in stops]
    center = ((min(lngs) + max(lngs)) / 2.0, (min(lats) + max(lats)) / 2.0)
    if len(stops) == 1:
        return 12, center
    for z in range(15, 7, -1):
        scale = tile * (2 ** z)
        xs = [_lonlat_to_world(s["lng"], s["lat"], scale)[0] for s in stops]
        ys = [_lonlat_to_world(s["lng"], s["lat"], scale)[1] for s in stops]
        if (max(xs) - min(xs)) <= (w - 2 * pad) and (max(ys) - min(ys)) <= (h - 2 * pad):
            return z, center
    return 9, center


def _render_route_map(stops: list[dict], w: int, h: int) -> Image.Image:
    zoom, center = _fit_zoom(stops, w, h)
    m = StaticMap(w, h, url_template=TILE_URL, headers=TILE_HEADERS, tile_request_timeout=15)
    m.add_line(Line([(s["lng"], s["lat"]) for s in stops], "#ff6f3c", 6))
    for i, s in enumerate(stops):
        m.add_marker(CircleMarker((s["lng"], s["lat"]), "#ffffff", 24))
        m.add_marker(CircleMarker((s["lng"], s["lat"]), "#ff6f3c", 17))
    try:
        img = m.render(zoom=zoom, center=center)
    except Exception as exc:  # noqa: BLE001
        logger.warning("map render failed (%s)", exc)
        img = _gradient_bg(w, h, (222, 232, 240), (200, 214, 226))
    img = _shade(img, w, h)
    draw = ImageDraw.Draw(img, "RGBA")
    _pill(draw, 40, 54, "YOUR ROUTE", _font(True, 30), (*ACCENT, 235), LIGHT)
    draw.text((44, h - 150), f"{len(stops)} stops across Goa", font=_font(True, 52), fill=LIGHT)
    draw.text((46, h - 88), "Tap play to explore each one", font=_font(False, 34), fill=(210, 216, 226))
    return img


# ----------------------------- cards -----------------------------

def _hero_card(title: str, subtitle: str, hero: Image.Image | None, w: int, h: int) -> Image.Image:
    if hero is not None:
        img = _shade(_cover(hero, w, h), w, h)
    else:
        img = _gradient_bg(w, h, (255, 138, 76), (196, 62, 40))
    draw = ImageDraw.Draw(img, "RGBA")
    draw.text((48, h * 0.30), "YOUR GOA, PERSONALIZED", font=_font(True, 34), fill=ACCENT)
    size = 82
    lines = _wrap(draw, title, _font(True, size), w - 96)
    if len(lines) > 3:
        size = 62
        lines = _wrap(draw, title, _font(True, size), w - 96)
    y = h * 0.36
    for line in lines:
        draw.text((46, y), line, font=_font(True, size), fill=LIGHT)
        y += size + 12
    if subtitle:
        y += 16
        for line in _wrap(draw, subtitle, _font(False, 40), w - 96):
            draw.text((46, y), line, font=_font(False, 40), fill=(255, 236, 228))
            y += 52
    return img


def _outro_card(closing: str, w: int, h: int) -> Image.Image:
    img = _gradient_bg(w, h, (255, 120, 66), (150, 40, 30))
    draw = ImageDraw.Draw(img)
    y = h * 0.32
    for line in _wrap(draw, closing or "Your Goa awaits.", _font(True, 60), w - 96):
        draw.text((46, y), line, font=_font(True, 60), fill=LIGHT)
        y += 78
    draw.text((46, h * 0.82), "Planned by your AI travel companion", font=_font(False, 32), fill=(255, 235, 228))
    draw.text((46, h * 0.82 + 44), "Built on Hermes", font=_font(True, 34), fill=LIGHT)
    return img


def _stop_frame(stop: dict, index: int, total: int, photo: Image.Image | None, w: int, h: int) -> Image.Image:
    if photo is not None:
        base = _shade(_cover(photo, w, h), w, h)
    else:
        # fall back to the location on the map when no photo is available
        base = _render_stop_map(stop, w, h)
    img = base
    draw = ImageDraw.Draw(img, "RGBA")

    # top row: stop counter (left) + time to spend (right)
    _pill(draw, 40, 52, f"STOP {index + 1}/{total}", _font(True, 30), (*ACCENT, 240), LIGHT)
    if stop.get("dwell"):
        _dwell_pill(draw, w - 40, 52, str(stop["dwell"]), _font(True, 30))

    # bottom caption block
    x, y = 46, int(h * 0.62)
    if stop.get("time_label"):
        draw.text((x, y), stop["time_label"].upper(), font=_font(True, 32), fill=ACCENT)
        y += 48
    for line in _wrap(draw, stop.get("name", ""), _font(True, 64), w - 92)[:2]:
        draw.text((x, y), line, font=_font(True, 64), fill=LIGHT)
        y += 74
    if stop.get("blurb"):
        for line in _wrap(draw, stop["blurb"], _font(False, 38), w - 92)[:2]:
            draw.text((x, y), line, font=_font(False, 38), fill=(224, 228, 236))
            y += 50
    if stop.get("rating"):
        draw.text((x, y + 4), f"\u2605 {stop['rating']}", font=_font(True, 40), fill=(255, 214, 90))
    return img


def _render_stop_map(stop: dict, w: int, h: int) -> Image.Image:
    m = StaticMap(w, h, url_template=TILE_URL, headers=TILE_HEADERS, tile_request_timeout=15)
    m.add_marker(CircleMarker((stop["lng"], stop["lat"]), "#ffffff", 30))
    m.add_marker(CircleMarker((stop["lng"], stop["lat"]), "#ff6f3c", 22))
    try:
        img = m.render(zoom=13, center=(stop["lng"], stop["lat"]))
    except Exception:  # noqa: BLE001
        img = _gradient_bg(w, h, (222, 232, 240), (200, 214, 226))
    return _shade(img, w, h)


# ----------------------------- ffmpeg assembly -----------------------------

def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return max(float(out.stdout.strip()), 0.5)
    except (ValueError, subprocess.SubprocessError):
        return 0.0


def _estimate_duration(text: str) -> float:
    return min(max(len((text or "").split()) / 2.8 + 0.8, 2.0), 9.0)


def _segment_clip(image_path: Path, audio_path: Path | None, duration: float, out_path: Path, w: int, h: int) -> None:
    frames = max(int(duration * FPS), 1)
    vf = (
        f"scale={w*2}:{h*2},zoompan=z='min(zoom+0.0007,1.14)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={FPS},format=yuv420p"
    )
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image_path)]
    if audio_path is not None:
        cmd += ["-i", str(audio_path)]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += [
        "-t", f"{duration:.3f}", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-r", str(FPS), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-shortest", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)


def _concat(clips: list[Path], out_path: Path, workdir: Path) -> None:
    list_file = workdir / "concat.txt"
    list_file.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", "-movflags", "+faststart", str(out_path)],
        check=True, capture_output=True, timeout=180,
    )


# ----------------------------- orchestration -----------------------------

async def render_trip_video(plan: dict, trip_id: str) -> Path:
    stops = plan.get("stops") or []
    if not stops:
        raise RuntimeError("cannot render video: no stops")

    w, h = settings.video_width, settings.video_height
    workdir = Path(tempfile.mkdtemp(prefix=f"goa_{trip_id}_"))

    segments: list[dict] = [{"kind": "hero", "text": plan.get("intro") or plan.get("title") or "Welcome to Goa."}]
    for i, s in enumerate(stops):
        segments.append({"kind": "stop", "text": s.get("narration") or s.get("name", ""), "index": i})
    segments.append({"kind": "map", "text": "And here's your whole route across Goa."})
    segments.append({"kind": "outro", "text": plan.get("closing") or "Your Goa awaits."})

    for n, seg in enumerate(segments):
        audio = await tts.synthesize(seg["text"], workdir / f"a{n}.mp3")
        if audio is not None:
            seg["audio"] = audio
            seg["duration"] = _probe_duration(audio) + 0.35
        else:
            seg["audio"] = None
            seg["duration"] = _estimate_duration(seg["text"])

    def _build() -> Path:
        photos = [_download_image(s.get("thumbnail")) for s in stops]
        clips: list[Path] = []
        for n, seg in enumerate(segments):
            img_path = workdir / f"f{n}.png"
            if seg["kind"] == "hero":
                _hero_card(plan.get("title") or "Your Goa", plan.get("subtitle") or "",
                           photos[0] if photos else None, w, h).save(img_path)
            elif seg["kind"] == "map":
                _render_route_map(stops, w, h).save(img_path)
            elif seg["kind"] == "outro":
                _outro_card(plan.get("closing") or "", w, h).save(img_path)
            else:
                i = seg["index"]
                _stop_frame(stops[i], i, len(stops), photos[i], w, h).save(img_path)

            clip_path = workdir / f"c{n}.mp4"
            _segment_clip(img_path, seg["audio"], seg["duration"], clip_path, w, h)
            clips.append(clip_path)

        out_path = settings.videos_dir / f"{trip_id}.mp4"
        _concat(clips, out_path, workdir)
        return out_path

    try:
        out_path = await asyncio.to_thread(_build)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info("rendered video %s (%d segments)", out_path, len(segments))
    return out_path
