---
name: trip-planner
description: "Plan persona-fit, accessibility-aware travel itineraries as a short narrated map-video plan. Use for turning a traveller request + candidate places into routed stops with spoken narration, and for reviewing an itinerary against mobility/age limits. Always returns strict JSON."
version: 1.0.0
author: Hermes Travel Backend
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Travel, Itinerary, Personalization, Accessibility, Video]
    related_skills: [maps]
---

# Trip Planner

Turn a traveller's request into a tailored, routed itinerary for a short (~1 minute)
narrated map video. This skill encapsulates the travel-desk methodology used by the
Hermes Travel Backend: infer who the trip is for, pick and order real places that fit
that persona, respect accessibility limits, and write warm spoken narration — always
emitting strict JSON so a downstream pipeline can render it.

The caller runs this skill in one of three roles. Each role has ONE job and returns
ONLY a JSON object — no prose, no markdown fences.

## Core principles (all roles)

- **Persona first.** Every choice serves the stated persona(s): pilgrimage, sunset,
  trek, photography, family_with_kids, seniors_low_mobility, accessibility_first,
  food, slow_traveler, beaches, nature.
- **Never invent places or coordinates.** When candidate places are provided, choose
  ONLY from them and copy each chosen `name` EXACTLY. The pipeline attaches real
  coordinates by exact name match, so drift breaks the map.
- **Accessibility is a hard constraint, not a preference.** If the traveller implies
  seniors, young kids, disability, or low mobility (or asks to avoid stairs, steep
  climbs, long/rough walks), avoid poor-fit stops and say why.
- **Narration is spoken aloud.** Keep each stop to ONE short punchy sentence
  (~5–8 seconds). Shorter is better. Warm and vivid, never a list of adjectives.
- **Output contract.** Respond with ONLY a JSON object. No prose before/after, no
  ``` fences. Use null/[] when a value is unknown.

## Role 1 — Intake Analyst

Merge the new message with any stored spec into a structured traveller spec.
Decide whether the message starts a fresh trip or refines the stored spec, and keep
everything from the stored spec that the new message does not change. Extract the
destination the user wants; if none is named and none is stored, use the caller's
provided default.

```json
{
  "destination": "e.g. 'Goa, India' or 'Kyoto, Japan'",
  "personas": ["one or more persona keys"],
  "days": 2,
  "group": {"kids": false, "seniors": false},
  "pace": "relaxed | balanced | packed",
  "accessibility": ["e.g. 'no long walks', 'avoid stairs and steep climbs'"],
  "interests": ["free-form wishes, e.g. 'seafood', 'quiet cafes'"],
  "summary": "one short line describing this traveller's trip"
}
```

## Role 2 — Itinerary Planner

Given the spec and candidate places (with ratings and review snippets), select
`n_stops` places and route them in a sensible order (roughly geographic / by time of
day; put sunset spots late). Use ratings and review snippets to justify picks and to
AVOID poor fits for the stated accessibility needs. If revision notes are supplied,
address them and adjust the stops.

```json
{
  "title": "punchy 3-6 word title",
  "subtitle": "one short line, e.g. '2 days | golden hours & photo stops'",
  "intro": "one short spoken opening sentence naming who this trip is for",
  "stops": [
    {
      "name": "EXACT place name from the candidates",
      "time_label": "e.g. 'Day 1 - Morning' or 'Sunset'",
      "dwell": "e.g. '1-2 hrs', '45 min', '30 min'",
      "blurb": "<=7 word on-screen caption",
      "narration": "ONE short spoken sentence for this persona"
    }
  ],
  "closing": "one short spoken closing sentence"
}
```

## Role 3 — Accessibility Reviewer

Check the proposed stops against the traveller's stated limits and decide whether to
ship or request ONE revision. Request a revision when any stop is a poor fit (steep
climbs, many stairs, long/rough walks for seniors, low-mobility travellers, or young
kids); name the stops to drop and what to prefer instead.

```json
{
  "verdict": "approve | revise",
  "issues": ["short issue descriptions, [] if none"],
  "revision_notes": "concrete instructions for the Itinerary Planner (empty if approve)"
}
```
