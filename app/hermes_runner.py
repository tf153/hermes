"""Async wrapper around the Hermes Agent CLI one-shot mode (`hermes -z`)."""

import asyncio
import json
import logging
import os
import re

from app.config import settings

logger = logging.getLogger(__name__)


class HermesError(RuntimeError):
    """Raised when a hermes invocation fails, times out, or returns garbage."""


async def run_hermes(prompt: str, timeout: float | None = None) -> str:
    """Run `hermes -z <prompt>` with the trip-planner skill preloaded.

    We preload our Hermes-native `trip-planner` skill (`-s`), which carries the
    travel-desk methodology (persona-fit selection, accessibility rules,
    narration style, strict-JSON contract). Note: `--ignore-rules` explicitly
    skips preloaded skills, so it is intentionally NOT passed here.
    """
    timeout = timeout or settings.hermes_timeout_seconds
    logger.info("hermes -z (%d chars prompt, skill=%s)", len(prompt), settings.hermes_skill)
    env = {
        **os.environ,
        # Long single-shot generations (full HTML page) exceed hermes' default
        # 90s non-streaming stale timeout; give the API call more headroom.
        "HERMES_API_CALL_STALE_TIMEOUT": str(int(timeout * 0.75)),
    }
    cmd = [settings.hermes_bin, "-z", prompt]
    # Minimal toolset keeps the system prompt small; "skills" keeps the skill
    # mechanism active so the preloaded skill is honoured.
    if settings.hermes_toolsets:
        cmd += ["-t", settings.hermes_toolsets]
    if settings.hermes_skill:
        cmd += ["-s", settings.hermes_skill]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=settings.hermes_workdir,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HermesError(f"hermes timed out after {timeout:.0f}s")

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        detail = (err or out)[-2000:]
        raise HermesError(f"hermes exited with code {proc.returncode}: {detail}")
    if not out:
        raise HermesError(f"hermes returned empty output. stderr: {err[-2000:]}")
    return out


async def run_hermes_json(prompt: str, timeout: float | None = None) -> dict:
    """Run hermes and parse a JSON object out of the response."""
    text = await run_hermes(prompt, timeout)
    return extract_json(text)


def extract_json(text: str) -> dict:
    """Pull a JSON object out of an LLM response (handles fences and prose)."""
    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    candidates.extend(fenced)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise HermesError(f"could not parse JSON from hermes output: {text[:500]}")


def extract_html(text: str) -> str:
    """Pull an HTML document out of an LLM response."""
    fenced = re.findall(r"```(?:html)?\s*(.*?)```", text, flags=re.DOTALL)
    for candidate in fenced:
        if "<html" in candidate.lower():
            text = candidate
            break

    lower = text.lower()
    start = lower.find("<!doctype")
    if start == -1:
        start = lower.find("<html")
    if start == -1:
        raise HermesError(f"no HTML document in hermes output: {text[:500]}")
    end = lower.rfind("</html>")
    if end == -1:
        return text[start:].strip()
    return text[start : end + len("</html>")].strip()
