# Hermes Goa Travel Backend

Telegram -> Hermes Agent Goa trip planner. A user tells the bot who's
travelling (personas like sunset chaser, family with kids, seniors, foodies)
and the backend designs a personalized Goa itinerary and renders it as a
~1-minute narrated map video. The bot replies instantly with a live trip page
URL; the page shows build progress and turns into the video player when ready.

## How it works

```
Telegram user
  -> python-telegram-bot (long polling, no webhook needed)
  -> trip created instantly -> bot replies with PUBLIC_BASE_URL/trip/{id}
  -> background build:
       1. hermes -z : merge message + stored per-chat spec into a traveler spec
       2. Linkup    : Goa places with coords, ratings, review quotes (cached)
       3. hermes -z : pick + route stops for this persona, write narration
       4. ffmpeg    : map video (Ken Burns) + ElevenLabs voiceover
  -> /trip/{id} polls /trip/{id}/status live, then swaps to the video player
  -> bot also sends the MP4 in the chat
```

Per-chat context lives in `data/sessions/{chat_id}.json`, so follow-up messages
like "make it senior-friendly" refine the trip. `/reset` clears it.

## Prerequisites

1. **Hermes Agent CLI** installed (`hermes --version` should work) with an
   inference provider configured:

   ```bash
   echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
   hermes model                 # pick a model
   hermes -z "say hi"           # verify one-shot mode works
   ```

2. **A Telegram bot token**: message `@BotFather` -> `/newbot` -> copy the token.

3. (Optional) **Linkup API key** from https://app.linkup.so for live Goa place
   data with ratings and real review quotes. Without it a built-in seed list
   of well-known Goa spots is used.

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

After changing the Linkup key, refresh the places cache:

```bash
rm -f data/goa_places.json   # refetched via Linkup on the next trip build
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
| `app/store.py`         | Per-chat context store under `data/sessions/`               |
| `app/linkup.py`        | Linkup structured search for Goa places (cached + seed)     |
| `app/pipeline.py`      | create_trip (instant URL) + build_trip (spec, plan, video)  |
| `app/video.py`         | Map video renderer (staticmap + Pillow + ffmpeg)            |
| `app/tts.py`           | ElevenLabs narration audio                                  |
| `app/bot.py`           | Telegram handlers (`/start`, `/reset`, free text)           |
| `app/main.py`          | FastAPI app + live trip pages + bot lifecycle + TTL cleanup |
