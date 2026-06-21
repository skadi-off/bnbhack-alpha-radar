from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("alerts")


def _post(token: str, chat_id: str, text: str) -> None:
    data = urllib.parse.urlencode({
        "chat_id": str(chat_id), "text": text[:4000], "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception:
        pass  # алерт НИКОГДА не должен ронять/блокировать торговый поток


def notify(token: str, chat_id: str, text: str) -> None:
    """Прямой пинг в TG (старт/heartbeat/события). Без токена/чата — no-op."""
    if not token or not chat_id:
        return
    threading.Thread(target=_post, args=(token, chat_id, text), daemon=True).start()


class TelegramHandler(logging.Handler):
    """Шлёт лог-записи (по умолчанию WARNING+) в Telegram. В фоновом потоке с таймаутом —
    чтобы не блокировать однопоточный цикл демона и не падать на сетевых сбоях."""

    def __init__(self, token: str, chat_id: str, level=logging.WARNING):
        super().__init__(level)
        self.token = token
        self.chat_id = str(chat_id)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            notify(self.token, self.chat_id, self.format(record))
        except Exception:
            pass


def attach_telegram(token: str, chat_id: str, level=logging.WARNING) -> bool:
    """Повесить TG-handler на корневой логгер. True если подключили."""
    if not token or not chat_id:
        log.warning("TG-алерты выключены: нет tg_bot_token/tg_chat_id в конфиге")
        return False
    h = TelegramHandler(token, chat_id, level)
    h.setFormatter(logging.Formatter("twak %(levelname)s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(h)
    return True
