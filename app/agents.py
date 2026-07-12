"""The travel-desk crew: a manager that plans the run and delegates to specialists.

If a travel agency were staffed by agents instead of humans, this is the org:

- **Trip Director** (manager): reads the specific request, plans which specialists
  to run, and reviews the itinerary - sending it back for one revision when a
  specialist flags a problem. Its plan is request-specific: a plain beach trip
  skips the accessibility review; a "my parents can't climb stairs" trip adds it.
- **Intake Analyst**: turns the traveller's message into a structured spec.
- **Place Researcher**: live-searches real places (Linkup) - a tool-using step.
- **Itinerary Planner**: selects and routes stops and writes narration.
- **Accessibility Reviewer**: checks the itinerary against mobility/age limits.
- **Video Producer**: renders the narrated map video (deterministic tool).

Every model call runs inside a `trace.span`, so the run viewer shows who was
called, in what order, with tokens, cost and latency per step.
"""

import json
import logging

from app import trace
from app.config import settings
from app.hermes_runner import HermesError, extract_json, run_hermes

logger = logging.getLogger(__name__)


MANAGER_PROMPT = """You are the Trip Director, the manager of an AI travel desk.
Read the incoming request and decide the plan of work for your specialist crew.
Do NOT plan the trip itself - only decide how the crew should handle THIS request.

Stored trip spec from earlier in this chat (null if this is a new conversation):
{stored_spec}

New message from the traveller:
"{message}"

Your specialists:
- Intake Analyst: turns the message into a structured traveller spec.
- Place Researcher: live web search for real places with ratings and reviews.
- Itinerary Planner: selects and routes stops and writes narration.
- Accessibility Reviewer: checks the itinerary against mobility/age limits.
- Video Producer: renders the final video.

Respond with ONLY a JSON object (no prose, no fences):
{{
  "request_type": "fresh_trip" | "refinement",
  "needs_accessibility_review": true or false,
  "subtasks": [ {{"agent": "<specialist name>", "goal": "<one short line>"}} ],
  "reasoning": "one short line: why this plan fits THIS specific request"
}}

Set needs_accessibility_review to true when the traveller (now or in the stored
spec) implies seniors, young kids, disability, low mobility, or asks to avoid
stairs, steep climbs or long walks. Otherwise set it false and skip that step."""


INTAKE_PROMPT = """You are the Intake Analyst on a travel desk.
Today's date is {today}.

Stored trip spec from earlier messages in this chat (null if none):
{stored_spec}

New message from the user:
"{message}"

Decide whether the message starts a fresh trip or refines the stored spec, then
merge accordingly. Infer the traveler persona(s) from this set when implied:
pilgrimage, sunset, trek, photography, family_with_kids, seniors_low_mobility,
accessibility_first, food, slow_traveler, beaches, nature.

Extract the destination the user wants to visit (a city, region or area). If
the user does not name any destination and none is stored, use "{default_dest}".

Respond with ONLY a JSON object (no prose, no fences), using null/[] when unknown:
{{
  "destination": "the place to visit, e.g. 'Goa, India' or 'Kyoto, Japan'",
  "personas": ["one or more of the persona keys above"],
  "days": <int, default 2 if unstated>,
  "group": {{"kids": <bool>, "seniors": <bool>}},
  "pace": "relaxed | balanced | packed",
  "accessibility": ["constraints, e.g. 'no long walks', 'avoid stairs and steep climbs'"],
  "interests": ["free-form extra wishes, e.g. 'seafood', 'quiet cafes'"],
  "summary": "one short line describing this traveler's trip"
}}

Keep everything from the stored spec that the new message does not change."""


PLAN_PROMPT = """You are the Itinerary Planner on a travel desk for {destination}.
You design a short map video itinerary tailored to the traveler.

Traveler spec:
{spec}

Candidate places in {destination} (real data with ratings and review snippets).
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
  "title": "punchy 3-6 word title, e.g. 'Kyoto for Sunset Chasers'",
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


REVIEW_PROMPT = """You are the Accessibility Reviewer on a travel desk. Check the
proposed itinerary against the traveller's stated limits and decide whether it is
safe to ship or needs one revision.

Traveller spec:
{spec}

Proposed stops (name, on-screen caption, rating):
{stops}

If any stop is a poor fit for the stated limits - steep climbs, many stairs, long
or rough walks for seniors, low-mobility travellers or young kids - request a
revision, name the stops to drop, and say what to prefer instead.

Respond with ONLY a JSON object (no prose, no fences):
{{
  "verdict": "approve" | "revise",
  "issues": ["short issue descriptions, [] if none"],
  "revision_notes": "concrete instructions for the Itinerary Planner (empty if approve)"
}}"""


async def _llm_json(agent: str, task: str, prompt: str) -> dict:
    """Run one specialist model call inside a trace span and parse its JSON."""
    async with trace.span(agent, task, kind="llm", model=settings.hermes_model) as sp:
        text = await run_hermes(prompt)
        sp.set_llm(prompt, text, settings.hermes_model)
        data = extract_json(text)
        return data


async def manager_plan(message: str, stored_spec: dict | None) -> dict:
    """Trip Director decides how the crew should handle this specific request."""
    prompt = MANAGER_PROMPT.format(
        stored_spec=json.dumps(stored_spec, ensure_ascii=False),
        message=message.replace('"', "'"),
    )
    try:
        decision = await _llm_json("Trip Director", "Plan the run", prompt)
    except HermesError:
        logger.warning("manager planning failed; using a default plan")
        decision = {}
    # Normalise so downstream code can rely on the shape.
    decision.setdefault(
        "request_type", "refinement" if stored_spec else "fresh_trip"
    )
    decision.setdefault("needs_accessibility_review", False)
    decision.setdefault(
        "subtasks",
        [
            {"agent": "Intake Analyst", "goal": "Parse the request"},
            {"agent": "Place Researcher", "goal": "Find real places"},
            {"agent": "Itinerary Planner", "goal": "Select and route stops"},
            {"agent": "Video Producer", "goal": "Render the video"},
        ],
    )
    decision.setdefault("reasoning", "default plan")
    return decision


async def intake_analyst(
    message: str, stored_spec: dict | None, today: str, default_dest: str
) -> dict:
    """Turn the traveller's message + stored context into a structured spec."""
    prompt = INTAKE_PROMPT.format(
        today=today,
        stored_spec=json.dumps(stored_spec, ensure_ascii=False),
        message=message.replace('"', "'"),
        default_dest=default_dest,
    )
    return await _llm_json("Intake Analyst", "Parse the traveller's request", prompt)


async def itinerary_planner(
    destination: str,
    spec: dict,
    places: list[dict],
    n_stops: int,
    revision_notes: str | None = None,
) -> dict:
    """Select and route stops; when revising, address the reviewer's notes."""
    prompt = PLAN_PROMPT.format(
        destination=destination,
        spec=json.dumps(spec, ensure_ascii=False),
        places=json.dumps(places, ensure_ascii=False),
        n_stops=n_stops,
    )
    task = "Select and route stops"
    if revision_notes:
        prompt += (
            "\n\nREVISION REQUESTED by the Trip Director. The first draft was sent "
            "back. Address these notes and adjust the stops accordingly:\n"
            f"{revision_notes}"
        )
        task = "Revise itinerary per reviewer notes"
    return await _llm_json("Itinerary Planner", task, prompt)


async def accessibility_reviewer(spec: dict, stops: list[dict]) -> dict:
    """Review the itinerary against the traveller's mobility/age limits."""
    slim = [
        {
            "name": s.get("name"),
            "caption": s.get("blurb") or "",
            "rating": s.get("rating"),
        }
        for s in stops
    ]
    prompt = REVIEW_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False),
        stops=json.dumps(slim, ensure_ascii=False),
    )
    try:
        review = await _llm_json(
            "Accessibility Reviewer", "Check stops vs traveller limits", prompt
        )
    except HermesError:
        logger.warning("accessibility review failed; approving by default")
        return {"verdict": "approve", "issues": [], "revision_notes": ""}
    review.setdefault("verdict", "approve")
    review.setdefault("issues", [])
    review.setdefault("revision_notes", "")
    return review


def spec_has_mobility_limits(spec: dict) -> bool:
    """True when the traveller spec implies an accessibility review is warranted."""
    group = spec.get("group") or {}
    if group.get("seniors") or group.get("kids"):
        return True
    if spec.get("accessibility"):
        return True
    limited = {"seniors_low_mobility", "accessibility_first", "family_with_kids"}
    return bool(limited.intersection(spec.get("personas") or []))
