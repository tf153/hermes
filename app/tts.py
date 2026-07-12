"""ElevenLabs text-to-speech for the video narration.

Returns an mp3 path per line. When no API key is configured (or a call fails),
returns None so the video renderer falls back to timed silent segments and the
on-screen captions still carry the plan.
"""

import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"


def enabled() -> bool:
    return bool(settings.elevenlabs_api_key)


async def synthesize(text: str, out_path: Path) -> Path | None:
    """Synthesize `text` to `out_path` (mp3). Returns the path, or None on failure."""
    text = (text or "").strip()
    if not text or not enabled():
        return None

    url = f"{API_BASE}/{settings.elevenlabs_voice_id}"
    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0},
    }
    headers = {
        "xi-api-key": settings.elevenlabs_api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            out_path.write_bytes(resp.content)
            return out_path
    except httpx.HTTPError as exc:
        detail = ""
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text[:300]
        logger.warning("ElevenLabs TTS failed: %s %s", exc, detail)
        return None
