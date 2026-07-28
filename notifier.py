"""
Skickar notiser till din Telegram istället för riktig iOS push —
mycket enklare att sätta upp och du får notisen direkt i Telegram-appen.

Sätt upp (tar ~2 minuter):
1. Öppna Telegram, sök upp "BotFather", skicka /newbot och följ stegen.
   Du får en TOKEN, t.ex. 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxx
2. Sök upp din nya bot och skicka valfritt meddelande till den (t.ex. "hej").
3. Hämta ditt chat_id genom att öppna i webbläsaren:
   https://api.telegram.org/bot<DIN_TOKEN>/getUpdates
   Leta efter "chat":{"id": ... } i svaret.
4. Lägg TOKEN och chat_id i .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
"""
import logging
import requests
from config import settings

logger = logging.getLogger("notifier")


def send_notification(message: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram är inte konfigurerat (saknar token/chat_id) — hoppar över notis.")
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": settings.telegram_chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Kunde inte skicka Telegram-notis: %s", e)
        return False
