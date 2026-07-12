# Hermes Travel Backend

Telegram -> Hermes Agent trip planner for any destination. A user tells the
bot where they're going and who's travelling (personas like sunset chaser,
family with kids, seniors, foodies) and the backend designs a personalized
itinerary and renders it as a ~1-minute narrated map video. The bot replies
instantly with a live trip page URL; the page shows build progress and turns
into the video player when ready.

If no destination is named, `DEFAULT_DESTINATION` (Goa, India) is used.

## How it works

The build is run by a small **agent crew** - a manager that plans the work and
delegates to specialists - not a fixed script:

```
Telegram user
  -> python-telegram-bot (long polling, no webhook needed)
  -> trip created instantly -> bot replies with PUBLIC_BASE_URL/trip/{id}
  -> background build, orchestrated by the Trip Director (manager):
       0. Trip Director   : reads THIS request, plans the subtasks, and decides
                            whether an accessibility review is needed
       1. Intake Analyst  : message + stored per-chat spec -> structured spec
       2. Place Researcher: live Linkup search for real places w/ coords,
                            ratings, review quotes (cached in data/places/)
       3. Itinerary Planner: pick + route stops for this persona, write narration
       4. Accessibility Reviewer (conditional): checks stops vs mobility/age
                            limits; can bounce the plan back for one revision
       5. Video Producer  : ffmpeg map video (Ken Burns) + ElevenLabs voiceover
  -> every step is recorded to data/trips/{id}/trace.json
  -> /trip/{id} polls status live, then swaps to the video player
  -> bot also sends the MP4 in the chat
```

The manager's plan is **request-specific**: a plain beach trip skips the review
step; a "my parents can't climb stairs" trip adds it and often triggers a
revision. Per-chat context lives in `data/sessions/{chat_id}.json`, so follow-up
messages like "make it senior-friendly" refine the trip. `/reset` clears it.

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
correctly. Run it before/after a prompt change to compare versions:

```bash
venv/bin/python -m eval.run_evals            # live (needs hermes configured)
venv/bin/python -m eval.run_evals --offline  # dataset/harness check, no API calls
```

CI runs the offline check on every PR and the live evals when provider secrets
are present, failing the build if the pass rate drops below the dataset
threshold (`.github/workflows/evals.yml`).

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
systemctl restart hermes-travel.service   # port 443 with TLS (behind Cloudflare)
journalctl -u hermes-travel -f            # logs
```

Or manually:

```bash
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

After changing the Linkup key, refresh the places caches:

```bash
rm -f data/places/*.json   # refetched via Linkup on the next trip build
```

## Try it

Message the bot (@hermes_smart_travel_bot):

> 2 days in Goa, I'm a sunset chaser and love photography

Then refine:

> My parents are joining - keep it easy, no stairs

## Layout

| Path                   | Purpose                                                     |
| ---------------------- | ----------------------------------------------------------- |
| `app/config.py`        | Env settings (pydantic-settings)                            |
| `app/hermes_runner.py` | Async subprocess wrapper around `hermes -z` one-shot mode   |
| `app/agents.py`        | The crew: Trip Director (manager) + specialists, all traced |
| `app/trace.py`         | Per-run span tracing (call tree, tokens, cost, latency)     |
| `app/store.py`         | Per-chat context store under `data/sessions/`               |
| `app/linkup.py`        | Linkup structured search for places (cached per destination)|
| `app/photos.py`        | SerpAPI Google Maps photos + place-data fallback            |
| `app/pipeline.py`      | create_trip (instant URL) + build_trip (runs the crew)      |
| `app/video.py`         | Map video renderer (staticmap + Pillow + ffmpeg)            |
| `app/tts.py`           | ElevenLabs narration audio                                  |
| `app/bot.py`           | Telegram handlers (`/start`, `/reset`, free text)           |
| `app/main.py`          | FastAPI app + trip pages + run viewer + bot + TTL cleanup   |
| `eval/`                | Named eval set + runner (`run_evals.py`)                    |
