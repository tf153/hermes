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

import asyncio
import json
import logging

from app import trace
from app.config import settings
from app.hermes_runner import HermesError, extract_json, run_hermes

logger = logging.getLogger(__name__)


# The capabilities the Trip Director can staff a trip with. The manager picks
# only the ones that apply to a given traveller and invents a role title + brief
# for each, so the crew that appears in the trace is composed per request rather
# than a fixed roster (emergent org, not a static routing table).
CAPABILITY_LIBRARY: dict[str, str] = {
    "seniors": (
        "Accessibility and low-mobility: prefer step-free access, short flat "
        "walks, benches and shade; avoid steep climbs, many stairs and long treks."
    ),
    "spiritual": (
        "Pilgrimage and spiritual sites: temples, churches, shrines; note dress "
        "codes and visiting hours; keep the pacing calm and respectful."
    ),
    "roadtrip": (
        "Roadtrip routing: order stops to minimise backtracking, group nearby "
        "spots, and favour scenic drivable stretches between them."
    ),
    "family": (
        "Family with kids: kid-friendly, low-tiring, safe; short activities with "
        "snack and rest options; avoid anything strenuous or risky."
    ),
    "food": (
        "Food and culinary: local cuisine, markets and iconic dishes; place meal "
        "stops at sensible times of day."
    ),
    "sunset_photo": (
        "Sunset and photography: golden-hour viewpoints and photogenic spots; "
        "schedule sunset locations late in the day."
    ),
}


def _capability_menu() -> str:
    return "\n".join(f"- {key}: {desc}" for key, desc in CAPABILITY_LIBRARY.items())


MANAGER_PROMPT = """You are the Trip Director, the manager of an AI travel desk.
Given the structured traveller spec, assemble the SMALLEST crew of specialists
that will produce the best itinerary for THIS specific traveller. Do not plan the
trip yourself - your specialists will advise the Itinerary Planner.

Traveller spec:
{spec}

Available specialist capabilities (choose ONLY the ones that clearly apply):
{capabilities}

For each capability you choose, invent a short role TITLE tailored to this
traveller and write a one-line BRIEF telling that specialist exactly what to do
for this trip. Choose at most 4. Skip capabilities that do not apply.

Respond with ONLY a JSON object (no prose, no fences):
{{
  "crew": [
    {{"capability": "<one capability key from the list>",
      "role": "<invented role title for this traveller>",
      "brief": "<one line, specific to this traveller>"}}
  ],
  "needs_accessibility_review": true or false,
  "reasoning": "one short line: why this crew fits THIS traveller"
}}

Set needs_accessibility_review true when the spec implies seniors, young kids,
disability, low mobility, or avoiding stairs, steep climbs or long walks."""


SPECIALIST_PROMPT = """You are the {role}, a specialist on an AI travel desk.
Your brief for this trip:
{brief}

Your capability focus: {guidance}

Traveller spec:
{spec}

Candidate places (real data with ratings and review snippets). Refer to them by
their EXACT "name":
{places}

Recommend how the Itinerary Planner should use these places for THIS traveller.
If you genuinely cannot fulfil your brief from these candidates (for example none
fit the constraint you own), escalate: set status to "blocked" with a concrete
blocker instead of guessing.

Respond with ONLY a JSON object (no prose, no fences):
{{
  "status": "ok" or "blocked",
  "blocker": "if blocked, the concrete reason (else empty string)",
  "prioritize": ["exact place names to feature"],
  "avoid": ["exact place names to avoid"],
  "ordering_hint": "e.g. 'put sunset spots last' (empty if none)",
  "notes": "one or two lines of concrete guidance for the planner"
}}"""


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


def _fallback_crew(spec: dict) -> list[dict]:
    """A sensible crew inferred from the spec when the manager call fails."""
    personas = set(spec.get("personas") or [])
    persona_to_cap = {
        "seniors_low_mobility": "seniors",
        "accessibility_first": "seniors",
        "family_with_kids": "family",
        "pilgrimage": "spiritual",
        "food": "food",
        "sunset": "sunset_photo",
        "photography": "sunset_photo",
    }
    caps: list[str] = []
    for persona in personas:
        cap = persona_to_cap.get(persona)
        if cap and cap not in caps:
            caps.append(cap)
    if spec_has_mobility_limits(spec) and "seniors" not in caps:
        caps.insert(0, "seniors")
    return [
        {"capability": cap, "role": cap.replace("_", " ").title() + " Specialist",
         "brief": f"Optimise the itinerary for {cap.replace('_', ' ')}."}
        for cap in caps[:4]
    ]


async def compose_crew(spec: dict) -> dict:
    """Trip Director assembles a request-specific crew of persona specialists."""
    prompt = MANAGER_PROMPT.format(
        spec=json.dumps(spec, ensure_ascii=False),
        capabilities=_capability_menu(),
    )
    try:
        decision = await _llm_json("Trip Director", "Compose the crew for this trip", prompt)
    except HermesError:
        logger.warning("crew composition failed; inferring a crew from the spec")
        decision = {}

    # Keep only known capabilities and give every entry a usable role/brief.
    crew: list[dict] = []
    for entry in decision.get("crew") or []:
        cap = entry.get("capability")
        if cap not in CAPABILITY_LIBRARY:
            continue
        crew.append(
            {
                "capability": cap,
                "role": (entry.get("role") or f"{cap.title()} Specialist").strip(),
                "brief": (entry.get("brief") or CAPABILITY_LIBRARY[cap]).strip(),
            }
        )
    if not crew:
        crew = _fallback_crew(spec)

    decision["crew"] = crew[:4]
    decision.setdefault(
        "needs_accessibility_review", spec_has_mobility_limits(spec)
    )
    decision.setdefault("reasoning", "crew composed from traveller spec")
    return decision


async def run_specialist(
    capability: str, role: str, brief: str, spec: dict, places: list[dict]
) -> dict:
    """Run one dynamically-composed persona specialist as a traced step.

    The span is labelled with the manager's invented role title, so the trace
    shows roles that did not exist at kickoff. A specialist that cannot satisfy
    its brief escalates with a concrete blocker instead of guessing.
    """
    guidance = CAPABILITY_LIBRARY.get(capability, "")
    prompt = SPECIALIST_PROMPT.format(
        role=role,
        brief=brief,
        guidance=guidance,
        spec=json.dumps(spec, ensure_ascii=False),
        places=json.dumps(places, ensure_ascii=False),
    )
    async with trace.span(role, f"[{capability}] {brief}"[:80], kind="llm",
                          model=settings.hermes_model) as sp:
        try:
            text = await run_hermes(prompt)
            sp.set_llm(prompt, text, settings.hermes_model)
            result = extract_json(text)
        except HermesError as exc:
            sp.note(escalated=True, blocker=str(exc)[:200])
            return {"capability": capability, "role": role, "status": "blocked",
                    "blocker": str(exc)[:200], "prioritize": [], "avoid": [], "notes": ""}
        result.update(capability=capability, role=role)
        if result.get("status") == "blocked":
            sp.note(escalated=True, blocker=result.get("blocker"))
        else:
            sp.note(
                prioritize=result.get("prioritize"),
                avoid=result.get("avoid"),
            )
        return result


async def run_crew_specialists(
    crew: list[dict], spec: dict, places: list[dict]
) -> list[dict]:
    """Run the composed specialists concurrently; one failure never aborts the run."""
    if not crew:
        return []
    tasks = [
        run_specialist(c["capability"], c["role"], c["brief"], spec, places)
        for c in crew
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict] = []
    for c, res in zip(crew, results):
        if isinstance(res, Exception):
            logger.warning("specialist %s failed: %s", c["role"], res)
            out.append(
                {"capability": c["capability"], "role": c["role"],
                 "status": "blocked", "blocker": str(res)[:200],
                 "prioritize": [], "avoid": [], "notes": ""}
            )
        else:
            out.append(res)
    return out


def format_specialist_guidance(results: list[dict]) -> str:
    """Fold specialist recommendations into a block for the Itinerary Planner."""
    lines: list[str] = []
    for r in results:
        if r.get("status") == "blocked":
            continue
        parts = []
        if r.get("prioritize"):
            parts.append("prefer " + ", ".join(r["prioritize"]))
        if r.get("avoid"):
            parts.append("avoid " + ", ".join(r["avoid"]))
        if r.get("ordering_hint"):
            parts.append(r["ordering_hint"])
        detail = "; ".join(parts)
        note = r.get("notes") or ""
        lines.append(f"- {r.get('role')}: {note} {('(' + detail + ')') if detail else ''}".rstrip())
    return "\n".join(lines)


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
    guidance: str | None = None,
    revision_notes: str | None = None,
) -> dict:
    """Select and route stops, using the specialists' guidance and any revision."""
    prompt = PLAN_PROMPT.format(
        destination=destination,
        spec=json.dumps(spec, ensure_ascii=False),
        places=json.dumps(places, ensure_ascii=False),
        n_stops=n_stops,
    )
    if guidance:
        prompt += (
            "\n\nSPECIALIST GUIDANCE from the crew (weigh these recommendations "
            "when choosing and ordering stops):\n" + guidance
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
