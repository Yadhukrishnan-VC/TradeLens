"""Alerts: tell a human when something needs them. Sending NEVER raises into trading code: a broken
Telegram must not stop an exit order. Secrets are never logged (urllib errors embed the URL, which
contains the Telegram bot token, so only the error class / HTTP status is kept)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import AlertRow

log = logging.getLogger("tradelite.alerts")
LEVELS = ("info", "warning", "critical")
_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
MAX_TEXT = 3900   # Telegram's limit is 4096 characters


@dataclass(frozen=True)
class Alert:
    level: str
    title: str
    body: str = ""

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"alert level must be one of {LEVELS}")

    def text(self) -> str:
        t = f"{_ICON[self.level]} tradelite: {self.title}" + (f"\n{self.body}" if self.body else "")
        return t if len(t) <= MAX_TEXT else t[: MAX_TEXT - 1] + "…"


class Notifier(Protocol):
    def send(self, alert: Alert) -> bool: ...


def safe_http_url(url: str) -> str:
    """urllib would happily open file:// and ftp:// URLs: only http(s) is acceptable for outbound pings."""
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("URL must start with http:// or https://")
    return url


class LogNotifier:
    def send(self, alert: Alert) -> bool:
        log.log({"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}[alert.level],
                "%s %s", alert.title, alert.body.replace("\n", " | "))
        return False   # a log line is not a delivery to a human


class _HttpPoster:
    def __init__(self, opener: Callable = urllib.request.urlopen, timeout: float = 10.0) -> None:
        self._open, self.timeout = opener, timeout

    def _post(self, url: str, payload: dict) -> bool:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return 200 <= getattr(resp, "status", 200) < 300
        except urllib.error.HTTPError as e:
            log.error("alert delivery failed: HTTP %s", e.code)
        except Exception as e:  # noqa: BLE001 - never leak the URL (it holds the bot token)
            log.error("alert delivery failed: %s", type(e).__name__)
        return False


class TelegramNotifier(_HttpPoster):
    def __init__(self, bot_token: str, chat_id: str, **kw) -> None:
        super().__init__(**kw)
        if not bot_token or not chat_id:
            raise ValueError("Telegram needs both a bot token and a chat id")
        self._url, self._chat = f"https://api.telegram.org/bot{bot_token}/sendMessage", chat_id

    def send(self, alert: Alert) -> bool:
        return self._post(self._url, {"chat_id": self._chat, "text": alert.text(), "disable_web_page_preview": True})


class WebhookNotifier(_HttpPoster):
    def __init__(self, url: str, **kw) -> None:
        super().__init__(**kw)
        self._url = safe_http_url(url)

    def send(self, alert: Alert) -> bool:
        text = alert.text()
        return self._post(self._url, {"level": alert.level, "title": alert.title, "body": alert.body,
                                      "text": text, "content": text})   # text: Slack, content: Discord


class MultiNotifier:
    """Sends to every channel; True if at least one accepted the alert."""
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = notifiers

    def send(self, alert: Alert) -> bool:
        ok = False
        for n in self.notifiers:
            try:
                ok = n.send(alert) or ok
            except Exception as e:  # noqa: BLE001
                log.error("notifier %s crashed: %s", type(n).__name__, type(e).__name__)
        return ok


class RecordingNotifier:
    """In-memory, for tests."""
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class StoredNotifier:
    """Persists every alert (so the dashboard can list them) and forwards it to the real channels."""
    def __init__(self, sf: sessionmaker[Session], inner: Notifier,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)) -> None:
        self.sf, self.inner, self.clock = sf, inner, clock

    def send(self, alert: Alert) -> bool:
        try:
            delivered = self.inner.send(alert)
        except Exception as e:  # noqa: BLE001
            log.error("notifier crashed: %s", type(e).__name__)
            delivered = False
        try:
            with self.sf() as s:
                s.add(AlertRow(ts=self.clock(), level=alert.level, title=alert.title[:200], body=alert.body[:4000],
                               delivered=delivered))
                s.commit()
        except Exception as e:  # noqa: BLE001
            log.error("could not store alert: %s", type(e).__name__)
        return delivered


def build_notifier(settings: Settings, sf: sessionmaker[Session] | None = None) -> Notifier:
    chans: list[Notifier] = [LogNotifier()]
    if settings.telegram_bot_token and settings.telegram_chat_id:
        chans.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    elif settings.telegram_bot_token or settings.telegram_chat_id:
        log.warning("Telegram is half configured (need TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID): alerts stay in the log")
    if settings.alert_webhook_url:
        chans.append(WebhookNotifier(settings.alert_webhook_url))
    multi = MultiNotifier(*chans)
    return StoredNotifier(sf, multi) if sf is not None else multi
