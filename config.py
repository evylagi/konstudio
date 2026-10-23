import os

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8778761518:AAHhKbvJSt5fz9UlfAls5uAFxNu8NoSR1FM",
)

MAX_CONCURRENT_PER_USER = int(os.getenv("MAX_CONCURRENT_PER_USER", "10"))
MAX_CARDS_PER_CHECK = int(os.getenv("MAX_CARDS_PER_CHECK", "5000"))

PROXIES = [p for p in os.getenv("PROXIES", "").split(",") if p.strip()]

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
