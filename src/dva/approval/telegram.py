"""Telegram Bot API client (long polling — no public webhook / server needed)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..core.errors import ConfigError
from ..core.http import HttpClient

log = logging.getLogger(__name__)
MAX_CAPTION = 1024
MAX_TEXT = 4096


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, http: HttpClient | None = None):
        if not token or not chat_id:
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = http or HttpClient(timeout=120)

    @classmethod
    def from_config(cls, cfg) -> "Telegram":
        t = cfg.section("approval.telegram")
        return cls(cfg.secret(t.get("bot_token_env", "TELEGRAM_BOT_TOKEN")), cfg.secret(t.get("chat_id_env", "TELEGRAM_CHAT_ID")))

    def _call(self, method: str, files: dict | None = None, retry: bool = True, **params):
        data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items() if v is not None}
        fn = self.http.post if retry else self.http.post_once
        resp = fn(f"{self.base}/{method}", data=data, files=files, ok_status=())
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]

    @staticmethod
    def keyboard(video_id: int) -> dict:
        return {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{video_id}"},
            {"text": "🔁 Regenerate", "callback_data": f"regen:{video_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{video_id}"},
        ]]}

    def send_message(self, text: str, reply_markup: dict | None = None, chat_id: str | None = None) -> list[int]:
        ids = []
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            res = self._call("sendMessage", chat_id=chat_id or self.chat_id, text=chunk,
                             disable_web_page_preview=True,
                             reply_markup=reply_markup if i == len(chunks) - 1 else None)
            ids.append(res["message_id"])
        return ids

    def send_video(self, path: Path, caption: str, reply_markup: dict | None = None,
                   thumb: Path | None = None, width: int = 1080, height: int = 1920, duration: int = 60) -> int:
        files = {"video": (path.name, open(path, "rb"), "video/mp4")}
        if thumb and thumb.exists():
            files["thumbnail"] = (thumb.name, open(thumb, "rb"), "image/jpeg")
        try:
            res = self._call("sendVideo", files=files, retry=False, chat_id=self.chat_id, caption=caption[:MAX_CAPTION],
                             supports_streaming="true", width=width, height=height, duration=duration,
                             reply_markup=reply_markup)
        finally:
            for f in files.values():
                f[1].close()
        return res["message_id"]

    def send_document(self, path: Path, caption: str = "", reply_markup: dict | None = None) -> int:
        with open(path, "rb") as fh:
            res = self._call("sendDocument", files={"document": (path.name, fh)}, retry=False,
                             chat_id=self.chat_id, caption=caption[:MAX_CAPTION], reply_markup=reply_markup)
        return res["message_id"]

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        resp = self.http.session.get(f"{self.base}/getUpdates",
                                     params={"offset": offset, "timeout": timeout,
                                             "allowed_updates": json.dumps(["message", "callback_query"])},
                                     timeout=timeout + 15)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"getUpdates failed: {body.get('description')}")
        return body["result"]

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except Exception as exc:  # noqa: BLE001 — callback may be too old
            log.debug("answerCallbackQuery failed: %s", exc)

    def clear_buttons(self, message_id: int) -> None:
        try:
            self._call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                       reply_markup={"inline_keyboard": []})
        except Exception as exc:  # noqa: BLE001
            log.debug("editMessageReplyMarkup failed: %s", exc)
