"""Cliente mínimo da Bot API do Telegram."""
from __future__ import annotations

import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        if not token:
            raise ValueError("Falta TELEGRAM_TOKEN.")
        self.base = f"https://api.telegram.org/bot{token}/"
        self.s = session or requests.Session()

    def call(self, method: str, timeout: float = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        r = self.s.post(self.base + method, json=params, timeout=timeout)
        try:
            data = r.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: HTTP {r.status_code}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def safe(self, method: str, **params):
        try:
            return self.call(method, **params)
        except (TelegramError, requests.RequestException):
            return None

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict]:
        """Long polling: espera até `timeout` segundos por mensagens novas."""
        params = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        r = self.s.post(self.base + "getUpdates", json=params, timeout=timeout + 15)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"getUpdates: {data.get('description')}")
        return data["result"]

    def send(self, chat_id, text: str, markup: dict | None = None, preview: bool = False):
        return self.safe(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=markup, link_preview_options={"is_disabled": not preview},
        )

    def send_photo(self, chat_id, photo: str, caption: str, markup: dict | None = None):
        res = self.safe("sendPhoto", chat_id=chat_id, photo=photo, caption=caption,
                        parse_mode="HTML", reply_markup=markup)
        if res is None:  # foto indisponível → envia só texto
            res = self.send(chat_id, caption, markup)
        return res

    def edit(self, chat_id, message_id: int, text: str, markup: dict | None = None):
        return self.safe("editMessageText", chat_id=chat_id, message_id=message_id,
                         text=text, parse_mode="HTML", reply_markup=markup,
                         link_preview_options={"is_disabled": True})
