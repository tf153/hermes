"""Speech-to-text for Telegram voice notes via the Wispr Flow REST API.

Telegram voice messages arrive as OGG/Opus; Wispr Flow wants base64-encoded
16kHz 16-bit mono PCM WAV, so ffmpeg converts in-memory first.
"""

import asyncio
import base64
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

WISPRFLOW_URL = "https://platform-api.wisprflow.ai/api/v1/dash/api"

# Wispr Flow rejects payloads over 25MB / 6 minutes of audio.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class TranscriptionError(RuntimeError):
    """Raised when a voice note cannot be converted or transcribed."""


async def _to_wav_16k(audio: bytes) -> bytes:
    """Convert any ffmpeg-readable audio (OGG/Opus, MP3, ...) to 16kHz mono WAV."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    wav, err = await proc.communicate(input=audio)
    if proc.returncode != 0 or not wav:
        raise TranscriptionError(
            f"ffmpeg audio conversion failed: {err.decode(errors='replace')[-300:]}"
        )
    return wav


async def transcribe(audio: bytes) -> str:
    """Transcribe a voice message; returns the cleaned-up transcript text."""
    if not settings.wisprflow_api_key:
        raise TranscriptionError("WISPRFLOW_API_KEY is not configured")

    wav = await _to_wav_16k(audio)
    if len(wav) > MAX_AUDIO_BYTES:
        raise TranscriptionError("voice message is too long (over 25MB of audio)")

    payload = {
        "audio": base64.b64encode(wav).decode(),
        "context": {"app": {"type": "ai"}},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                WISPRFLOW_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.wisprflow_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise TranscriptionError(f"Wispr Flow request failed: {exc}") from exc

    text = (data.get("text") or "").strip()
    if not text:
        raise TranscriptionError("Wispr Flow returned an empty transcript")
    logger.info(
        "wisprflow transcript (%s, %sms): %s",
        data.get("detected_language"),
        data.get("total_time"),
        text[:200],
    )
    return text
