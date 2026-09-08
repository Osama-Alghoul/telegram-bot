"""Telegram command / conversation handlers for the electricity usage bot.

Multi-user, self-serve: anyone who is a member of the configured channel
(see access.py) gets their own private meter tree, readings, and period
settings, isolated from every other user.
"""
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
import logic
from access import VERIFY_CALLBACK, require_membership, verify_join_cb

logger = logging.getLogger(__name__)

# conversation states
ADDM_NAME, ADDM_PARENT = range(2)
RM_CONFIRM = range(100, 101)[0]
RN_PICK, RN_NAME = range(200, 202)
MV_PICK, MV_PARENT = range(300, 302)
AR_PICK, AR_VALUE = range(400, 402)
SP_DAYS, SP_ANCHOR = range(500, 502)

CB_CANCEL = "cancel"


def meter_keyboard(user_id: int, exclude_ids: set[int] | None = None, extra_top: list[tuple[str, str]] | None = None):
    exclude_ids = exclude_ids or set()
    tree = logic.build_tree(user_id, None)
    rows = []
    if extra_top:
        for label, cbdata in extra_top:
            rows.append([InlineKeyboardButton(label, callback_data=cbdata)])

    def walk(nodes, depth):
        for meter, children in nodes:
            if meter["id"] not in exclude_ids:
                indent = "▫️" * depth
                label = f"{indent} {meter['name']}".strip()
                rows.append([InlineKeyboardButton(label, callback_data=f"pick:{meter['id']}")])
            walk(children, depth + 1)

    walk(tree, 0)
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


def descendants_of(meter_id: int, user_id: int) -> set[int]:
    result = set()
    frontier = [meter_id]
    while frontier:
        current = frontier.pop()
        for child in db.get_children(user_id, current):
            result.add(child["id"])
            frontier.append(child["id"])
    return result


# ---------------- basic commands ----------------

@require_membership
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ *Electricity usage tracker*\n\n"
        "Set up your meter hierarchy, then log readings any time. I'll do the subtraction.\n"
        "Your meters and readings are private to you.\n\n"
        "*Setup*\n"
        "/addmeter — add a meter (top-level or under another one)\n"
        "/movemeter — move a meter under a different parent\n"
        "/renamemeter — rename a meter\n"
        "/removemeter — remove a meter (and its sub-meters)\n"
        "/tree — show the meter structure\n\n"
        "*Recording & reports*\n"
        "/addreading — log a reading for a meter\n"
        "/history — see recent readings for a meter\n"
        "/usage — usage report for the current period\n\n"
        "*Period*\n"
        "/period — show current period settings\n"
        "/setperiod — change the period length (e.g. weekly, daily, custom days)\n\n"
        "/cancel — cancel whatever we're in the middle of",
        parse_mode=ParseMode.MARKDOWN,
    )


@require_membership
async def tree_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(logic.render_tree_text(user_id), parse_mode=ParseMode.MARKDOWN)


@require_membership
async def usage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    offset = 0
    if context.args:
        try:
            offset = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /usage [period offset, e.g. -1 for last period]")
            return
    period = logic.current_period(user_id)
    if offset:
        period = logic.period_by_index(user_id, period.index + offset)
    await update.message.reply_text(logic.render_usage_report(user_id, period), parse_mode=ParseMode.MARKDOWN)


@require_membership
async def period_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    p = logic.current_period(user_id)
    await update.message.reply_text(
        f"⏱ Period length: *{p.length_days} day(s)*\n"
        f"Current period: {p.start:%Y-%m-%d %H:%M} → {p.end:%Y-%m-%d %H:%M} UTC\n\n"
        f"To change it: /setperiod <days> [YYYY-MM-DD]\n"
        f"e.g. `/setperiod 7` (weekly, starting now) or `/setperiod 7 2026-09-08` "
        f"(weekly, anchored so periods start on that date's weekday).",
        parse_mode=ParseMode.MARKDOWN,
    )


@require_membership
async def setperiod_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /setperiod <days> [YYYY-MM-DD]\n"
            "Examples:\n"
            "`/setperiod 1` — daily\n"
            "`/setperiod 7` — weekly, new period starting right now\n"
            "`/setperiod 7 2026-09-08` — weekly, anchored to that date",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        days = int(context.args[0])
        if days < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Period length must be a positive integer number of days.")
        return

    anchor = None
    if len(context.args) > 1:
        try:
            anchor = datetime.strptime(context.args[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            await update.message.reply_text("Anchor date must look like YYYY-MM-DD.")
            return

    logic.set_period(user_id, days, anchor)
    p = logic.current_period(user_id)
    await update.message.reply_text(
        f"✅ Period set to *{days} day(s)*.\n"
        f"Current period: {p.start:%Y-%m-%d %H:%M} → {p.end:%Y-%m-%d %H:%M} UTC",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Cancelled.")
    return ConversationHandler.END


# ---------------- add meter ----------------

@require_membership
async def addmeter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("What should the new meter be called?")
    return ADDM_NAME


async def addmeter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a non-empty name.")
        return ADDM_NAME
    context.user_data["new_meter_name"] = name
    user_id = update.effective_user.id
    kb = meter_keyboard(user_id, extra_top=[("➕ Top level (no parent)", "parent:none")])
    await update.message.reply_text(
        f"Where does *{name}* sit? Pick its parent meter, or top level.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADDM_PARENT


async def addmeter_parent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    name = context.user_data.pop("new_meter_name", None)
    if not name:
        await query.edit_message_text("Something went wrong, please start again with /addmeter.")
        return ConversationHandler.END

    if data == "parent:none":
        parent_id = None
    else:
        parent_id = int(data.split(":")[1])
        if db.get_meter(parent_id, user_id) is None:
            await query.edit_message_text("That parent meter no longer exists, please start again with /addmeter.")
            return ConversationHandler.END

    meter_id = db.add_meter(user_id, name, parent_id)
    location = "top level" if parent_id is None else logic.format_meter_path(parent_id, user_id)
    await query.edit_message_text(f"✅ Added *{name}* (`#{meter_id}`) under {location}.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- remove meter ----------------

@require_membership
async def removemeter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.list_meters(user_id):
        await update.message.reply_text("No meters yet.")
        return ConversationHandler.END
    await update.message.reply_text("Which meter do you want to remove?", reply_markup=meter_keyboard(user_id))
    return RM_CONFIRM


async def removemeter_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    if meter is None:
        await query.edit_message_text("That meter no longer exists.")
        return ConversationHandler.END
    descendants = descendants_of(meter_id, user_id)
    warn = f"\n⚠️ This will also delete {len(descendants)} sub-meter(s) and all their readings." if descendants else ""
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗑 Yes, delete", callback_data=f"del:{meter_id}"),
                InlineKeyboardButton("✖ Cancel", callback_data=CB_CANCEL),
            ]
        ]
    )
    await query.edit_message_text(
        f"Delete *{meter['name']}* and all its readings?{warn}", reply_markup=kb, parse_mode=ParseMode.MARKDOWN
    )
    return RM_CONFIRM


async def removemeter_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    name = meter["name"] if meter else "meter"
    db.delete_meter(meter_id, user_id)
    await query.edit_message_text(f"🗑 Deleted *{name}*.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- rename meter ----------------

@require_membership
async def renamemeter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.list_meters(user_id):
        await update.message.reply_text("No meters yet.")
        return ConversationHandler.END
    await update.message.reply_text("Which meter do you want to rename?", reply_markup=meter_keyboard(user_id))
    return RN_PICK


async def renamemeter_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    if meter is None:
        await query.edit_message_text("That meter no longer exists.")
        return ConversationHandler.END
    context.user_data["rename_id"] = meter_id
    await query.edit_message_text(f"New name for *{meter['name']}*?", parse_mode=ParseMode.MARKDOWN)
    return RN_NAME


async def renamemeter_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    meter_id = context.user_data.pop("rename_id", None)
    if meter_id is None:
        await update.message.reply_text("Something went wrong, please start again with /renamemeter.")
        return ConversationHandler.END
    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("Please send a non-empty name.")
        context.user_data["rename_id"] = meter_id
        return RN_NAME
    db.rename_meter(meter_id, user_id, new_name)
    await update.message.reply_text(f"✅ Renamed to *{new_name}*.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- move meter ----------------

@require_membership
async def movemeter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.list_meters(user_id):
        await update.message.reply_text("No meters yet.")
        return ConversationHandler.END
    await update.message.reply_text("Which meter do you want to move?", reply_markup=meter_keyboard(user_id))
    return MV_PICK


async def movemeter_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    if meter is None:
        await query.edit_message_text("That meter no longer exists.")
        return ConversationHandler.END
    context.user_data["move_id"] = meter_id
    exclude = descendants_of(meter_id, user_id) | {meter_id}
    kb = meter_keyboard(user_id, exclude_ids=exclude, extra_top=[("➕ Top level (no parent)", "parent:none")])
    await query.edit_message_text(
        f"Move *{meter['name']}* under which meter?", reply_markup=kb, parse_mode=ParseMode.MARKDOWN
    )
    return MV_PARENT


async def movemeter_parent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = context.user_data.pop("move_id", None)
    if meter_id is None:
        await query.edit_message_text("Something went wrong, please start again with /movemeter.")
        return ConversationHandler.END
    data = query.data
    if data == "parent:none":
        new_parent_id = None
    else:
        new_parent_id = int(data.split(":")[1])
        if db.get_meter(new_parent_id, user_id) is None:
            await query.edit_message_text("That meter no longer exists, please start again with /movemeter.")
            return ConversationHandler.END
    db.move_meter(meter_id, user_id, new_parent_id)
    meter = db.get_meter(meter_id, user_id)
    location = "top level" if new_parent_id is None else logic.format_meter_path(new_parent_id, user_id)
    await query.edit_message_text(f"✅ Moved *{meter['name']}* under {location}.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- add reading ----------------

@require_membership
async def addreading_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.list_meters(user_id):
        await update.message.reply_text("No meters yet — use /addmeter first.")
        return ConversationHandler.END
    await update.message.reply_text("Which meter are you logging a reading for?", reply_markup=meter_keyboard(user_id))
    return AR_PICK


async def addreading_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    if meter is None:
        await query.edit_message_text("That meter no longer exists.")
        return ConversationHandler.END
    context.user_data["reading_meter_id"] = meter_id
    last = db.get_last_reading(meter_id, user_id)
    hint = f" (last logged: {logic.fmt_num(last['value'])})" if last else ""
    await query.edit_message_text(
        f"Current reading for *{meter['name']}*{hint}?", parse_mode=ParseMode.MARKDOWN
    )
    return AR_VALUE


async def addreading_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    meter_id = context.user_data.get("reading_meter_id")
    if meter_id is None:
        await update.message.reply_text("Something went wrong, please start again with /addreading.")
        return ConversationHandler.END
    text = update.message.text.strip().replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        await update.message.reply_text("That doesn't look like a number. Send the meter reading, e.g. 1234.5")
        return AR_VALUE

    context.user_data.pop("reading_meter_id", None)
    db.add_reading(meter_id, user_id, value)
    meter = db.get_meter(meter_id, user_id)
    period = logic.current_period(user_id)
    mu = logic.compute_usage(meter_id, user_id, period)
    usage_line = f"\nUsage this period so far: {logic.fmt_num(mu.usage)} kWh" if mu.usage is not None else ""
    await update.message.reply_text(
        f"✅ Logged {logic.fmt_num(value)} for *{meter['name']}*.{usage_line}", parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


# ---------------- history ----------------

@require_membership
async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.list_meters(user_id):
        await update.message.reply_text("No meters yet.")
        return ConversationHandler.END
    await update.message.reply_text("Show history for which meter?", reply_markup=meter_keyboard(user_id))
    return AR_PICK  # reuse state number space is fine, separate ConversationHandler


async def history_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    meter_id = int(query.data.split(":")[1])
    meter = db.get_meter(meter_id, user_id)
    if meter is None:
        await query.edit_message_text("That meter no longer exists.")
        return ConversationHandler.END
    rows = db.get_recent_readings(meter_id, user_id, limit=10)
    if not rows:
        await query.edit_message_text(f"No readings logged yet for *{meter['name']}*.", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    lines = [f"🕘 *Recent readings — {meter['name']}*", ""]
    for r in rows:
        dt = logic.parse_iso(r["recorded_at"])
        lines.append(f"`{dt:%Y-%m-%d %H:%M}`  {logic.fmt_num(r['value'])}")
    await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- registration ----------------

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()

    app.add_handler(CallbackQueryHandler(verify_join_cb, pattern=f"^{VERIFY_CALLBACK}$"))

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("tree", tree_cmd))
    app.add_handler(CommandHandler("usage", usage_cmd))
    app.add_handler(CommandHandler("period", period_cmd))
    app.add_handler(CommandHandler("setperiod", setperiod_cmd))

    addmeter_conv = ConversationHandler(
        entry_points=[CommandHandler("addmeter", addmeter_start)],
        states={
            ADDM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addmeter_name)],
            ADDM_PARENT: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(addmeter_parent, pattern=r"^(parent:none|pick:\d+)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    removemeter_conv = ConversationHandler(
        entry_points=[CommandHandler("removemeter", removemeter_start)],
        states={
            RM_CONFIRM: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(removemeter_confirm, pattern=r"^del:\d+$"),
                CallbackQueryHandler(removemeter_pick, pattern=r"^pick:\d+$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    renamemeter_conv = ConversationHandler(
        entry_points=[CommandHandler("renamemeter", renamemeter_start)],
        states={
            RN_PICK: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(renamemeter_pick, pattern=r"^pick:\d+$"),
            ],
            RN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, renamemeter_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    movemeter_conv = ConversationHandler(
        entry_points=[CommandHandler("movemeter", movemeter_start)],
        states={
            MV_PICK: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(movemeter_pick, pattern=r"^pick:\d+$"),
            ],
            MV_PARENT: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(movemeter_parent, pattern=r"^(parent:none|pick:\d+)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    addreading_conv = ConversationHandler(
        entry_points=[CommandHandler("addreading", addreading_start)],
        states={
            AR_PICK: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(addreading_pick, pattern=r"^pick:\d+$"),
            ],
            AR_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addreading_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    history_conv = ConversationHandler(
        entry_points=[CommandHandler("history", history_start)],
        states={
            AR_PICK: [
                CallbackQueryHandler(cancel_cb, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(history_pick, pattern=r"^pick:\d+$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd)],
    )

    for conv in (
        addmeter_conv,
        removemeter_conv,
        renamemeter_conv,
        movemeter_conv,
        addreading_conv,
        history_conv,
    ):
        app.add_handler(conv)

    return app
