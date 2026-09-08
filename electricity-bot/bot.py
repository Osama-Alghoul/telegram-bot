"""Entry point for the electricity usage Telegram bot."""
import logging
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv

load_dotenv()  # must happen before importing access/handlers, which read env vars at import time

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

_token = os.environ.get("TELEGRAM_BOT_TOKEN")
_channel_id = os.environ.get("CHANNEL_ID")
_channel_invite = os.environ.get("CHANNEL_INVITE_LINK")
if not _token or not _channel_id or not _channel_invite:
    print(
        "Missing configuration.\n"
        "Create a .env file (see .env.example) with:\n"
        "  TELEGRAM_BOT_TOKEN=<token from @BotFather>\n"
        "  CHANNEL_ID=<your channel's @username, or numeric id for a private channel>\n"
        "  CHANNEL_INVITE_LINK=<url shown to users to join the channel>",
        file=sys.stderr,
    )
    sys.exit(1)

import db
from access import CHANNEL_ID
from handlers import build_application


def main():
    token = _token
    db.init_db()
    app = build_application(token)
    logging.getLogger(__name__).info("Bot starting (channel_id=%s)...", CHANNEL_ID)
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
