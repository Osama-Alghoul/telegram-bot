# Electricity Usage Bot

A Telegram bot that tracks electricity usage across a hierarchy of Watt
meters (a main meter, sub-meters under it, sub-sub-meters, etc). You log the
raw cumulative reading you see on each meter whenever you happen to check it;
the bot works out usage per meter for the current counting period by
subtracting the reading at the start of the period from the latest one.

Multi-user, self-serve: anyone who joins your Telegram channel can start
using the bot on their own — no manual approval needed from you. Each person
gets their own completely separate, private meter tree, reading history, and
period settings on the same bot; nobody can see anyone else's data.

## Setup

1. **Create the bot**: message [@BotFather](https://t.me/BotFather) on
   Telegram, run `/newbot`, and copy the token it gives you.
2. **Create (or pick) your channel**, and **add the bot as an admin of it**:
   open the channel → *Administrators* → *Add Admin* → search for your bot by
   username. This is required — Telegram bots can only check channel
   membership if they're an admin there, even with no special permissions
   ticked.
3. **Get the channel id and invite link**:
   - Public channel: use `@yourchannelusername` directly as `CHANNEL_ID`, and
     `https://t.me/yourchannelusername` as `CHANNEL_INVITE_LINK`.
   - Private channel: get the numeric id (e.g. by forwarding a channel post to
     [@userinfobot](https://t.me/userinfobot)) for `CHANNEL_ID`, and generate
     an invite link from *Administrators* → *Invite Links* for
     `CHANNEL_INVITE_LINK`.
4. **Configure**:
   ```
   copy .env.example .env
   ```
   Edit `.env` and fill in `TELEGRAM_BOT_TOKEN`, `CHANNEL_ID`, and
   `CHANNEL_INVITE_LINK`.
5. **Install dependencies** (a virtualenv named `venv` is already set up in
   this folder):
   ```
   venv\Scripts\pip install -r requirements.txt
   ```
6. **Run it**:
   ```
   venv\Scripts\python bot.py
   ```
   Leave this running (or set it up as a scheduled task / service) — it polls
   Telegram for messages, no inbound networking or hosting needed.

Data is stored locally in `meters.db` (SQLite, created automatically).

### How the join flow works

Anyone who messages the bot without being a channel member gets a "Join the
channel, then tap Verify" prompt instead of the normal reply. Once they join
and tap **Verify**, they're checked live against the channel and immediately
free to use every command — no action needed from you. Membership is
re-checked (with a 5-minute cache) on every command, so someone who leaves
the channel loses access again automatically.

## Using it

Setup:
- `/addmeter` — add a meter, choosing its parent (or top-level) via buttons
- `/movemeter` — move a meter under a different parent
- `/renamemeter` — rename a meter
- `/removemeter` — delete a meter (and its sub-meters + their history)
- `/tree` — show the full meter structure with each meter's last reading

Recording & reports:
- `/addreading` — log today's reading for a meter, any time
- `/history` — see the last 10 readings for a meter
- `/usage` — usage report for the current period, per meter, with an
  "unaccounted" figure per parent (its usage minus the sum of its direct
  children's usage — useful for spotting load that isn't sub-metered)
- `/usage -1` — same, but for the previous period (`-2` for the one before, etc.)

Period:
- `/period` — show the current period length and its start/end
- `/setperiod <days>` — change the period length, e.g. `/setperiod 7` for
  weekly (starting a fresh period right now), `/setperiod 1` for daily, or
  any custom number of days. Add a date to anchor it, e.g.
  `/setperiod 7 2026-09-08` to make periods start on that date's weekday.

`/cancel` aborts whatever multi-step flow you're in the middle of.

## How usage is computed

For a meter and a period `[start, end)`:
- **baseline** = the meter's last reading at or before `start` (falls back to
  the first reading *after* start if none exists yet — marked with `~` since
  it's an approximation)
- **latest** = the meter's last reading at or before `end` (or "now" if the
  period is still ongoing)
- **usage** = `latest − baseline`

If a meter has fewer than two readings in range, usage shows as "no data" /
"no baseline yet" instead of a number.

## Notes

- Everything is in UTC internally; report timestamps are shown as UTC.
- Access is gated live by channel membership; each member gets fully isolated
  data (own meters, readings, and period settings) — nobody can see or affect
  anyone else's, even though it's the same bot and database file.
- If everyone is unexpectedly getting the "join the channel" prompt even
  though they've joined, the usual cause is the bot not actually being an
  admin of the channel — check the bot's own logs for `get_chat_member(...)
  failed` warnings.
