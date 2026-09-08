"""Self-serve access gate: users must be a member of a configured Telegram
channel before they can use any bot command. Membership is checked live via
the Bot API (cached briefly per user to avoid hammering Telegram)."""
import logging
import os
import time
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

logger = logging.getLogger(__name__)

_raw_channel_id = os.environ["CHANNEL_ID"]
# numeric channel ids (private channels) look like "-1001234567890";
# public channels can instead be referenced as "@channelusername".
CHANNEL_ID = int(_raw_channel_id) if _raw_channel_id.lstrip("-").isdigit() else _raw_channel_id
CHANNEL_INVITE_LINK = os.environ["CHANNEL_INVITE_LINK"]

_MEMBER_STATUSES = {"member", "administrator", "creator"}
_CACHE_TTL_SECONDS = 300
_membership_cache: dict[int, tuple[bool, float]] = {}

VERIFY_CALLBACK = "verify_join"


async def is_channel_member(bot, user_id: int, *, force: bool = False) -> bool:
    if not force:
        cached = _membership_cache.get(user_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        ok = member.status in _MEMBER_STATUSES
    except TelegramError as exc:
        # Also fires if the bot itself isn't an admin of the channel yet - check the logs
        # if every user is unexpectedly being rejected.
        logger.warning("get_chat_member(%s, %s) failed: %s", CHANNEL_ID, user_id, exc)
        ok = False
    _membership_cache[user_id] = (ok, time.monotonic() + _CACHE_TTL_SECONDS)
    return ok


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join the channel", url=CHANNEL_INVITE_LINK)],
            [InlineKeyboardButton("✅ I've joined — Verify", callback_data=VERIFY_CALLBACK)],
        ]
    )


async def send_join_prompt(update: Update):
    text = (
        "🔒 *This bot is free for members of our channel.*\n\n"
        "Join the channel, then tap *Verify* below."
    )
    if update.callback_query:
        await update.callback_query.answer("Please join the channel first.", show_alert=True)
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=_join_keyboard(), parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError:
            pass
    elif update.message:
        await update.message.reply_text(text, reply_markup=_join_keyboard(), parse_mode=ParseMode.MARKDOWN)


def require_membership(handler):
    """Decorator for command/conversation-entry handlers: gates on channel membership."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if await is_channel_member(context.bot, user_id):
            return await handler(update, context)
        await send_join_prompt(update)
        return ConversationHandler.END

    return wrapper


async def verify_join_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    ok = await is_channel_member(context.bot, user_id, force=True)
    if ok:
        await query.answer("You're in! 🎉")
        await query.edit_message_text("✅ Verified! Send /start to get going.")
    else:
        await query.answer(
            "Still not seeing you in the channel — join it, then tap Verify again.", show_alert=True
        )
