"""Free alert channels. The dashboard itself is always an alert channel; Telegram is optional."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable, Protocol


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class NullNotifier:
    def send(self, text: str) -> None:
        return None


def _http_post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


class TelegramNotifier:
    """Free: create a bot with @BotFather, put its token and your chat id in the environment."""

    def __init__(self, token: str, chat_id: str, post: Callable[[str, dict], None] = _http_post) -> None:
        self.url, self.chat_id, self._post = f"https://api.telegram.org/bot{token}/sendMessage", chat_id, post

    def send(self, text: str) -> None:
        self._post(self.url, {"chat_id": self.chat_id, "text": text})
