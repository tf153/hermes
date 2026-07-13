# Hermes Travel Backend

Hermes Agent trip planner for any destination, reachable from **Telegram** or
the **web landing page**. A user says where they're going and who's travelling
(personas like sunset chaser, family with kids, seniors, foodies) and the
backend designs a personalized itinerary and renders it as a ~1-minute
narrated map video. The reply is instant: a live trip page URL that shows
build progress and turns into the full trip page (video + interactive map +
itinerary + edit box) when ready.

If no destination is named, `DEFAULT_DESTINATION` (Goa, India) is used.

## How it works

The build is run by a small **agent crew** - a manager that plans the work and
delegates to specialists - not a fixed script:

```
Telegram user (long polling)          Web user (landing page / trip edit box)
  -> bot.py handle_message               -> POST /api/trip | /api/trip/{id}/refine
  -> trip created instantly -> reply with PUBLIC_BASE_URL/trip/{id}
  -> background build, orchestrated by the Trip Director (manager):
       1. Intake Analyst  : message + stored per-chat spec -> structured spec
                            (guardrail: rejects anything that isn't trip planning)
       2. Place Researcher: live Linkup search for real places w/ coords,
                            ratings, review quotes (cached in data/places/)
       3. Trip Director   : composes a crew for THIS traveller - picks the
                            relevant capabilities and invents a role + brief
                            for each; decides if an accessibility review is needed
       4. Persona specialists (spawned per request, run concurrently): each
                            advises which stops to prefer/avoid + ordering, or
                            escalates with a blocker it can't satisfy
       5. Itinerary Planner: pick + route stops using the crew's guidance,
                            write narration
       6. Accessibility Reviewer (conditional): checks stops vs mobility/age
                            limits; can bounce the plan back for one revision
       7. Video Producer  : ffmpeg map video (Ken Burns) + ElevenLabs voiceover
  -> every step is recorded to data/trips/{id}/trace.json
  -> /trip/{id} polls status live, then swaps to the video player
  -> bot also sends the MP4 in the chat
```

The crew is **composed per request**: the Trip Director picks from a capability
library (seniors/low-mobility, spiritual, roadtrip, family, food,
sunset/photography) and spawns specialists with role titles it invents for that
traveller, so the roster in the trace differs run to run - a plain beach trip
spawns none of them and skips the review; a "parents can't climb stairs, temples
only" trip spawns access + pilgrimage specialists and often triggers a revision.
Per-chat context lives in `data/sessions/{chat_id}.json`, so follow-up messages
like "make it senior-friendly" refine the trip. `/reset` (Telegram) or
"Start a fresh trip" (web) clears it. Web browsers get a stable `client_id` in
localStorage that acts like a Telegram chat id, and the "Edit this trip" box on
every finished trip page rebuilds with the same memory.

### Guardrails and abuse protection

- **Trip-only filter**: the Intake Analyst classifies every message first; the
  build aborts before any place search or video render when the message is not
  about planning/refining a trip (coding help, homework, jailbreaks, etc.).
  Rejections are captured to `eval/captured.jsonl` (reason `off_topic`).
- **Rate limiting** (`app/ratelimit.py`): sliding-window caps on starting
  builds - per user (`RATE_LIMIT_REQUESTS`/`RATE_LIMIT_WINDOW_MINUTES`,
  default 5/hour) and global (`RATE_LIMIT_GLOBAL_PER_HOUR`, default 30/hour).
  Web gets HTTP 429 + Retry-After; Telegram gets a friendly retry message.
- **One build at a time** per chat/client; duplicates get "one moment" without
  consuming rate-limit quota.
- **Photo caching**: photo URLs fetched per stop are written back into the
  destination's places cache (`data/places/{slug}.json`), so rebuilds do not
  re-hit SerpAPI.

### Hermes skill

Every planning call preloads a **Hermes-native skill** we authored,
`trip-planner` (source in [`skills/trip-planner/SKILL.md`](skills/trip-planner/SKILL.md)),
which holds the travel-desk methodology (persona-first selection, accessibility
as a hard constraint, spoken-narration style, strict-JSON contract). The runner
invokes `hermes -z <prompt> -t skills -s trip-planner`; note `--ignore-rules` is
deliberately not passed, since it would skip preloaded skills. Install it for
the agent once:

```bash
mkdir -p ~/.hermes/skills/travel/trip-planner
cp skills/trip-planner/SKILL.md ~/.hermes/skills/travel/trip-planner/
hermes skills list | grep trip-planner    # -> trip-planner | travel | local | enabled
```

Configurable in `.env`: `HERMES_SKILL` (default `trip-planner`, set empty to
disable) and `HERMES_TOOLSETS` (default `skills`).

## Observability (watch the agents work)

Every run writes a step-by-step trace (who called whom, tokens, cost and latency
per step). A live viewer streams it while the build runs:

- `GET /runs/{trip_id}` - live trace viewer: the manager -> specialist call tree,
  per-step tokens / est. cost / duration, filter by agent, expand any step to see
  its input/output. Linked from the build page and the finished video page.
- `GET /trip/{trip_id}/trace` - raw trace JSON.
- `GET /runs` - index of recent runs with totals.

Token counts are estimated (~4 chars/token) since `hermes -z` does not expose
usage; latency is real wall-clock. Cost rates are configurable in `.env`
(`COST_INPUT_PER_MTOK`, `COST_OUTPUT_PER_MTOK`).

## Evals

A named, version-controlled eval set (`eval/dataset.json`) checks that the crew
extracts the right destination/persona and routes the accessibility review
correctly. Results are saved per version and can be compared over time:

```bash
venv/bin/python -m eval.run_evals                 # live (needs hermes configured)
venv/bin/python -m eval.run_evals --version v2    # tag the saved results
venv/bin/python -m eval.run_evals --offline       # dataset/harness check, no API calls
venv/bin/python -m eval.run_evals --trend         # pass rate across saved versions
```

The runner exits non-zero below the dataset threshold, so CI can gate a release:
the offline check runs on every PR and the live evals run when provider secrets
are present (`.github/workflows/evals.yml`).

**Closed loop:** when a run is flagged in production - the Accessibility Reviewer
requests a revision, or a plan yields no usable stops - the case is appended to
`eval/captured.jsonl` (`app` → `eval/capture.py`) and folded back into the eval
set on the next run, so the set grows from real failures and later versions are
scored against the exact inputs that once broke. Prompts live in git, and
per-version results in `eval/results/` make cross-version gains measurable.

## Prerequisites

1. **Hermes Agent CLI** installed (`hermes --version` should work) with an
   inference provider configured:

   ```bash
   echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
   hermes model                 # pick a model
   hermes -z "say hi"           # verify one-shot mode works
   ```

2. **A Telegram bot token**: message `@BotFather` -> `/newbot` -> copy the token.

3. (Optional) **Linkup API key** from https://app.linkup.so for live place
   data with ratings and real review quotes. Without it, SerpAPI Google Maps
   data is used when `SERPAPI_API_KEY` is set; as a last resort a built-in
   seed list covers Goa only.

4. (Optional) **ElevenLabs API key** for the video voiceover; silent timing is
   used when unset.

## Setup

```bash
cd /root/hermes
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env: TELEGRAM_BOT_TOKEN, PUBLIC_BASE_URL, LINKUP_API_KEY, ELEVENLABS_API_KEY
```

## Run

Installed as a systemd service on this droplet:

```bash
systemctl restart hermes-travel.service   # restart (picks up .env and code changes)
systemctl status hermes-travel.service    # is it up?
journalctl -u hermes-travel -f            # logs
curl -sk https://localhost/health         # {"status":"ok", ...} + which keys are configured
```

The service listens on port 443 with a self-signed TLS cert (Cloudflare "Full"
mode in front). Any `.env` change (e.g. a new SerpAPI or Linkup key) requires a
restart to take effect.

Or manually:

```bash
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

After changing the Linkup key, refresh the places caches:

```bash
rm -f data/places/*.json   # refetched via Linkup on the next trip build
```

## Try it

Message the bot (@hermes_smart_travel_bot) or open https://www.rahuljoshi.info
and type into the landing page:

> 2 days in Goa, I'm a sunset chaser and love photography

Then refine (reply in Telegram, or use the "Edit this trip" box on the page):

> My parents are joining - keep it easy, no stairs

## Layout

| Path                   | Purpose                                                     |
| ---------------------- | ----------------------------------------------------------- |
| `app/config.py`        | Env settings (pydantic-settings)                            |
| `app/hermes_runner.py` | Async subprocess wrapper around `hermes -z` one-shot mode   |
| `app/agents.py`        | The crew: Trip Director + per-request persona specialists, all traced |
| `app/trace.py`         | Per-run span tracing (call tree, tokens, cost, latency)     |
| `app/store.py`         | Per-chat context store under `data/sessions/`               |
| `app/linkup.py`        | Linkup structured search for places (cached per destination)|
| `app/photos.py`        | SerpAPI Google Maps photos + place-data fallback            |
| `app/ratelimit.py`     | Sliding-window rate limits (per user + global)              |
| `app/pipeline.py`      | create_trip (instant URL) + build_trip (runs the crew)      |
| `app/video.py`         | Map video renderer (staticmap + Pillow + ffmpeg)            |
| `app/tts.py`           | ElevenLabs narration audio                                  |
| `app/bot.py`           | Telegram handlers (`/start`, `/reset`, free text)           |
| `app/main.py`          | FastAPI app: landing page, trip pages, web API, run viewer, bot, TTL cleanup |
| `skills/trip-planner/` | Hermes-native skill (`SKILL.md`) preloaded on every call    |
| `eval/`                | Named eval set + runner (`run_evals.py`), closed-loop capture (`capture.py`), per-version results |
