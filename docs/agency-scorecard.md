# AI as Agency — Scorecard (Hermes Travel Desk)

> Judge-facing self-assessment for the **AI as Agency** track. Every level is
> meant to be **verified live** — each row lists exactly what to open or run.
> Scoring formula (same everywhere): **points = (L − 1) × weight**, so L5 = 4 × weight.

## What this is

A Telegram-native **AI travel desk**: a manager-led crew of agents that replaces
the human function a travel brand staffs with an intake planner, an itinerary
researcher, an accessibility checker and a video editor. A traveller messages the
bot in plain language ("2 days in Goa, sunset chaser, parents can't climb
stairs"); the **Trip Director** plans the work, delegates to specialists, reviews
the itinerary, and ships a narrated ~1-minute vertical map video — as a shareable
public page and an MP4 in the chat.

**The crew (who does what):**

| Agent | Role |
|---|---|
| Trip Director (manager) | Plans the subtasks for this specific request; decides whether an accessibility review is needed; reviews the itinerary and sends it back for one revision when flagged. |
| Intake Analyst | Turns the message + stored context into a structured traveller spec. |
| Place Researcher | Live Linkup web search for real places with ratings/reviews. |
| Itinerary Planner | Selects and routes stops, writes narration; revises on request. |
| Accessibility Reviewer | Checks stops against mobility/age limits; approves or requests a revision. |
| Video Producer | Renders the narrated map video. |

Delegation is **dynamic**: a plain beach request skips the review step; a
seniors/kids/no-stairs request adds it and often triggers a real revision cycle.

**Try it (judge, from your own phone):** message the bot, get a live build page
instantly and the video in ~2–3 min. Open `PUBLIC_BASE_URL/runs/{trip_id}` to
watch the crew work step by step.

## Eligibility (Hermes usage)

- **Base harness** — the product runs on Hermes: the manager and every specialist
  are `hermes -z` calls doing real work in each trip (`app/agents.py`,
  `app/hermes_runner.py`).
- **Coding partner** — built with Hermes; session receipts available on request.

## The rubric (max = L5 on every parameter)

`80 + 20 + 28 + 20 + 8 + 4 + 4 = 164` base points. The root parameter (real
output) **overflows uncapped**: +1 pt × 20x per additional real task completed
autonomously during judging.

## Where this build lands (self-assessed, verify live)

| Parameter | Weight | Level | Points | What to verify |
|---|---|:--:|---:|---|
| Working product shipping real output | 20x | **L3** | 40 | Message the bot; a real video + public trip page are produced end-to-end with no human in the loop. *(L4 argued: fully autonomous, no approval step.)* Overflow: run more trips live for +20 each. |
| Agent org structure | 5x | **L4** | 15 | Manager plans subtasks for the specific request, delegates, and reviews outputs. Verify: give a plain "beach trip in Bali" (review step is skipped) vs "parents can't climb stairs" (review runs + the itinerary is sent back to the Planner for a revision) — the trace shows two different plans and a revision span. |
| Observability | 7x | **L4** | 21 | Open `/runs/{trip_id}`: manager→specialist call tree, tokens + est. cost + latency on every step, filter by agent, expand any step for its input/output. Raw JSON at `/trip/{id}/trace`. *(L5 would add run diffing + alerts + search across runs.)* |
| Evaluation and iteration | 5x | **L3** | 10 | Named, version-controlled set (`eval/dataset.json`); run `python -m eval.run_evals` to compare versions. CI gate stub (`.github/workflows/evals.yml`) points toward L4. |
| Agent handoffs and memory | 2x | **L3** | 4 | Per-chat spec persists across messages (`data/sessions/{chat_id}.json`); a follow-up ("add my parents, no stairs") refines the same trip. *(L4 argued.)* |
| Cost and latency per task | 1x | **L3** | 2 | Trace shows per-run wall-clock + est. cost; a full run is a few minutes and cents. Time a fresh run live (worse of time/cost governs). |
| Management UI | 1x | **L1** | 0 | The run viewer is read-only observability, not an operator console for defining agent roles. Honest floor. |
| **Base subtotal** | | | **92** | |

## Power-ups (+25 each, flat, stack on the base)

| Partner | Status | Evidence to show |
|---|:--:|---|
| ElevenLabs | ✅ live | Narration voice is the video's voiceover — play any trip. |
| Linkup | ✅ live | Place Researcher's live search supplies real places + ratings/reviews (`app/linkup.py`; visible as a tool span in the trace). |
| Cloudflare | ✅ live | Hosting/TLS in front of the app — live URL + CF dashboard. |
| Wispr Flow | ⏳ reachable | Voice-note transcription (`app/stt.py`); dictate 500+ words during the event to bank it. |
| Convex | — | Not wired (state is filesystem JSON). |
| Dodo Payments | — | No checkout (Revenue-track integration). |

**Confirmed now: +75** (ElevenLabs + Linkup + Cloudflare). **+100** with Wispr Flow.

## Cross-track bonus (Virality, half weight, cap 50)

LinkedIn launch post: ~300 impressions, 15 likes, 3 comments → reactions+comments
= 18 → Virality L3 → **+2 pts**. Impressions/amplification are not cross-track
eligible; drive product visitors/signups to unlock the parameters that pay.

## Bottom line

| Component | Points |
|---|---:|
| AI as Agency base (as built) | ~92 |
| Power-ups live now | +75 |
| Cross-track Virality bonus | +2 |
| **Total (today)** | **~169** |

With Wispr Flow banked: **~194**, plus overflow for every extra autonomous trip
run live during judging, plus upside if the real-output row is granted L4.

*Levels are our honest self-assessment to speed verification, not final scores —
please check each row against the live product and the `/runs` trace.*
