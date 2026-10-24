"""Loads settings from the .env file."""
import os
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


def _encode_db_password(url: str) -> str:
    """Lets the .env hold the raw password, even with special characters like @ % & *.

    Postgres URLs need those characters percent-encoded, so we encode the
    password part here instead of asking the user to do it by hand.
    """
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" not in credentials:
        return url
    user, password = credentials.split(":", 1)
    return f"{scheme}://{user}:{quote(password, safe='')}@{host}"


DATABASE_URL = _encode_db_password(os.getenv("DATABASE_URL", "").strip())
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
BOT_NAME = os.getenv("BOT_NAME", "Spotter")

# Used only to estimate cost in agent_turns / llm_calls: (input, output) USD per 1M tokens.
# From https://ai.google.dev/gemini-api/docs/pricing (Oct 2026). 3.6/3.8 Flash double on 1 Jan 2027.
MODEL_PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash": (0.75, 3.75),   # billed as 3.6 Flash
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
}
GEMINI_INPUT_USD_PER_M, GEMINI_OUTPUT_USD_PER_M = MODEL_PRICES.get(GEMINI_MODEL, (0.75, 3.75))
GEMINI_INPUT_USD_PER_M = float(os.getenv("GEMINI_INPUT_USD_PER_M", GEMINI_INPUT_USD_PER_M))
GEMINI_OUTPUT_USD_PER_M = float(os.getenv("GEMINI_OUTPUT_USD_PER_M", GEMINI_OUTPUT_USD_PER_M))

# Background job that sends due reminders (turn off with SCHEDULER_ENABLED=false).
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

# WhatsApp Cloud API
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
CRON_SECRET = os.getenv("CRON_SECRET", "")

# The web simulator (test UI + its API). Turn off on a public server.
SIMULATOR_ENABLED = os.getenv("SIMULATOR_ENABLED", "true").lower() == "true"

# WhatsApp Flow (in-chat form) used for onboarding. Mode "draft" works for test numbers before publishing.
WHATSAPP_ONBOARDING_FLOW_ID = os.getenv("WHATSAPP_ONBOARDING_FLOW_ID", "")
WHATSAPP_FLOW_MODE = os.getenv("WHATSAPP_FLOW_MODE", "draft")
