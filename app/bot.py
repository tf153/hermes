"""Telegram bot: receives travel requests, runs the pipeline, replies with URL."""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import store
from app.config import settings
from app.hermes_runner import HermesError
from app.pipeline import build_trip, create_trip

logger = logging.getLogger(__name__)

# chat ids with a pipeline currently running, to avoid concurrent runs per chat
_active_chats: set[int] = set()

WELCOME = (
    "Namaste! I'm your Goa travel companion.\n\n"
    "Tell me who's travelling and what you love, and I'll design a Goa trip "
    "just for you and send you a short map video of it.\n\n"
    "Try things like:\n"
    "- \"2 days in Goa, I'm a sunset chaser and love photography\"\n"
    "- \"Family trip with 2 young kids, nothing too tiring\"\n"
    "- \"My parents are coming, they can't climb stairs - temples + easy sights\"\n"
    "- \"Solo slow traveller, just cafes and quiet mornings\"\n\n"
    "Same Goa, a completely different trip for each traveller.\n\n"
    "Follow-up messages refine it (\"make it senior-friendly\"). "
    "Use /reset to start over."
)


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    store.reset_session(update.effective_chat.id)
    await update.message.reply_text("Context cleared. Tell me about your next trip!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return

    if chat_id in _active_chats:
        await update.message.reply_text(
            "I'm still working on your previous request - one moment!"
        )
        return

    _active_chats.add(chat_id)

    # The link goes out immediately; the page shows the build live and turns
    # into the video player on its own when the render finishes.
    handle = create_trip(chat_id)
    await update.message.reply_text(
        f"On it! Watch your Goa trip come together live:\n\n{handle.url}\n\n"
        f"I'll also send the video here when it's ready (~2-3 min)."
    )

    context.application.create_task(
        _build_and_deliver(update, handle.trip_id, chat_id, text), update=update
    )


async def _build_and_deliver(
    update: Update, trip_id: str, chat_id: int, text: str
) -> None:
    try:
        result = await build_trip(trip_id, chat_id, text)
        caption = (
            f"{result.summary}\n\n"
            f"Watch/share: {result.url}\n\n"
            f"Send another message to refine it, or /reset to start over."
        )
        with open(result.video_path, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                supports_streaming=True,
                width=settings.video_width,
                height=settings.video_height,
                read_timeout=120,
                write_timeout=120,
            )
    except HermesError as exc:
        logger.exception("pipeline failed for chat %s", chat_id)
        await update.message.reply_text(
            "Sorry, the planning agent failed on this one. "
            "If this keeps happening, check that hermes is configured "
            f"(`hermes -z \"say hi\"` on the server).\n\nDetails: {str(exc)[:300]}"
        )
    except Exception:
        logger.exception("unexpected pipeline error for chat %s", chat_id)
        await update.message.reply_text(
            "Sorry, something went wrong building that trip. Please try again."
        )
    finally:
        _active_chats.discard(chat_id)


def build_application() -> Application:
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    return application
