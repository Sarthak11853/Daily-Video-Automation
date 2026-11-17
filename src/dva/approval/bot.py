"""Long-running Telegram listener (long polling — no public server needed).

Handles: Approve / Regenerate / Reject buttons, finished videos you send in
prompt mode, the inbox folder, reminders, and a few commands."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..core.db import utcnow

log = logging.getLogger(__name__)

HELP = ("Send me your finished video (as a file) with your caption and I'll publish it.\n"
        "A text message = caption for the video waiting for one.\n\n"
        "/status – latest runs & pending videos\n/new – make today's topic/prompts now\n"
        "/pending – resend what's waiting for you\n/prompts <id> – resend prompts for a video\n"
        "/skip <id> – drop a pending topic\n/metrics – performance summary\n/help – this message")
ALLOWED = {"approve": ("awaiting_approval",), "reject": ("awaiting_approval", "awaiting_footage", "awaiting_caption"),
           "regen": ("awaiting_approval", "awaiting_footage"), "autocap": ("awaiting_caption",)}
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024


class ApprovalBot:
    def __init__(self, pipeline):
        self.p = pipeline
        self.tg = pipeline.telegram
        if not self.tg:
            raise SystemExit("Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        self.db = pipeline.db
        self.jobs = ThreadPoolExecutor(1)
        self.busy = False

    def _job(self, fn, *args):
        def wrap():
            self.busy = True
            try:
                msg = fn(*args)
                if msg:
                    self.tg.send_message(str(msg))
            except Exception as exc:  # noqa: BLE001
                log.exception("job failed")
                self.tg.send_message(f"⚠️ Job failed: {exc}")
            finally:
                self.busy = False
        self.jobs.submit(wrap)

    # ---------------------------------------------------------- buttons
    def handle_callback(self, cq: dict) -> None:
        chat = str(cq.get("message", {}).get("chat", {}).get("id"))
        if chat != self.tg.chat_id:
            self.tg.answer_callback(cq["id"], "Not authorised")
            return
        try:
            action, vid = cq.get("data", "").split(":")
            video_id = int(vid)
        except ValueError:
            self.tg.answer_callback(cq["id"], "Bad button")
            return
        v = self.db.get_video(video_id)
        if not v or v["status"] not in ALLOWED.get(action, ()):
            self.tg.answer_callback(cq["id"], f"Already handled ({v['status'] if v else 'missing'})")
            return
        labels = {"approve": "Publishing…", "reject": "Dropped", "regen": "Working on a new version…",
                  "autocap": "Publishing with the suggested caption…"}
        self.tg.answer_callback(cq["id"], labels.get(action, "?"))
        self.tg.clear_buttons(cq["message"]["message_id"])
        if action == "autocap":
            from ..footage.processor import FootageProcessor
            self._job(FootageProcessor(self.p).use_suggested, video_id)
        else:
            self._job(self.p.decide, video_id, action)

    # ----------------------------------------------------- footage intake
    def _target_for(self, msg: dict):
        from ..footage.processor import FootageProcessor
        proc = FootageProcessor(self.p)
        reply = (msg.get("reply_to_message") or {}).get("message_id")
        if reply:
            for v in proc.pending():
                if reply in json.loads(v["telegram_message_ids"] or "[]"):
                    return v
        return proc.resolve(msg.get("caption") or "")

    def handle_footage(self, msg: dict) -> None:
        media = msg.get("video") or msg.get("document") or msg.get("animation")
        name = media.get("file_name") or "video.mp4"
        if msg.get("document") and not (media.get("mime_type", "").startswith("video/")
                                        or Path(name).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}):
            self.tg.send_message("That file isn't a video. Send an MP4/MOV, please.")
            return
        target = self._target_for(msg)
        if target is None:
            self.tg.send_message("No video is waiting for footage right now. Send /new to get today's prompts.")
            return
        inbox = self.p.cfg.path("prompt_mode.inbox_dir", "inbox").expanduser()
        if (media.get("file_size") or 0) > TELEGRAM_DOWNLOAD_LIMIT:
            self.tg.send_message(
                f"This file is {media['file_size'] / 1e6:.0f} MB. Telegram bots can only download files up to 20 MB.\n"
                f"Drop it into the inbox folder instead (name it {target['id']}.mp4):\n{inbox}")
            return
        if self.busy:
            self.tg.send_message("I'm busy with another job; I'll process this right after.")
        self.tg.send_message(f"📥 Got it — finishing video #{target['id']} “{target['title']}”…")
        run = self.db.one("SELECT output_dir FROM runs WHERE id=?", (target["run_id"],))
        dest = Path(run["output_dir"]) / "footage_in" / f"{msg['message_id']}_{Path(name).stem}{Path(name).suffix or '.mp4'}"

        caption = msg.get("caption")

        def work():
            from ..footage.processor import FootageProcessor
            self.tg.download(media["file_id"], dest)
            return FootageProcessor(self.p).process(target["id"], [dest], caption=caption)
        self._job(work)

    def handle_caption_text(self, text: str) -> None:
        """A plain text message is a caption: for the video waiting for one, else for the next video."""
        from ..footage.processor import FootageProcessor
        proc = FootageProcessor(self.p)
        waiting = self.db.one("SELECT id FROM videos WHERE status='awaiting_caption' ORDER BY id DESC LIMIT 1")
        if waiting:
            cap = proc.clean_caption(text, waiting["id"])
            self.tg.send_message(f"✍️ Caption received for #{waiting['id']}, publishing…")
            self._job(proc.apply_caption, waiting["id"], cap)
            return
        target = proc.resolve(text)
        if target:
            cap = proc.clean_caption(text, target["id"])
            self.db.update_video(target["id"], user_caption=cap)
            self.tg.send_message(f"✍️ Saved as the caption for #{target['id']}. Now send the video.")
            return
        self.tg.send_message(HELP)

    def poll_inbox(self) -> None:
        if self.busy:
            return
        from ..footage.inbox import ready_items, scan_inbox
        inbox = self.p.cfg.path("prompt_mode.inbox_dir", "inbox").expanduser()
        if ready_items(inbox):
            self._job(lambda: "\n\n".join(scan_inbox(self.p)) or None)

    # --------------------------------------------------------- commands
    def handle_message(self, msg: dict) -> None:
        chat = str(msg.get("chat", {}).get("id"))
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            self.tg.send_message(f"Hi! Your chat id is {chat}. Put it in .env as TELEGRAM_CHAT_ID.\n\n{HELP}", chat_id=chat)
            return
        if chat != self.tg.chat_id:
            return
        if msg.get("video") or msg.get("document") or msg.get("animation"):
            self.handle_footage(msg)
            return
        if text and not text.startswith("/"):
            self.handle_caption_text(text)
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = int(parts[1].lstrip("#")) if len(parts) > 1 and re.fullmatch(r"#?\d+", parts[1]) else None
        if cmd == "/status":
            runs = self.db.query("SELECT id, run_date, kind, status, stage FROM runs ORDER BY id DESC LIMIT 5")
            pend = self.db.query("SELECT id, title, status FROM videos WHERE status IN ('awaiting_approval',"
                                 "'awaiting_footage','processing_footage','awaiting_caption') ORDER BY id DESC")
            out = ["Recent runs:"] + [f"#{r['id']} {r['run_date']} {r['kind']} {r['status']} ({r['stage']})" for r in runs]
            out += ["", "Waiting on you:"] + ([f"#{v['id']} {v['title']} — {v['status']}" for v in pend] or ["nothing"])
            out += ["", "Worker: busy" if self.busy else "Worker: idle"]
            self.tg.send_message("\n".join(out))
        elif cmd == "/new":
            if self.busy:
                self.tg.send_message("A job is already running, please wait.")
            else:
                self.tg.send_message("Finding a trending fact and preparing everything… (a few minutes)")
                self._job(lambda: (lambda r: f"Run finished: {r.get('status')} {r.get('error', '')[:500]}")(
                    self.p.run(kind="manual", force=True)))
        elif cmd == "/metrics":
            from ..analytics.collector import report
            self.tg.send_message(report(self.db))
        elif cmd == "/pending":
            rows = self.db.query("SELECT id, status FROM videos WHERE status IN "
                                 "('awaiting_approval','awaiting_footage','awaiting_caption')")
            if not rows:
                self.tg.send_message("Nothing is waiting for you. 🎉")
            for v in rows:
                if v["status"] == "awaiting_footage":
                    self.p.send_prompt_package(v["id"])
                elif v["status"] == "awaiting_caption":
                    from .telegram import Telegram
                    self.tg.send_message(f"✍️ #{v['id']} is waiting for your caption. Send it as a text message.",
                                         reply_markup=Telegram.caption_keyboard(v["id"]))
                else:
                    self.p.send_for_approval(v["id"])
        elif cmd == "/prompts" and arg:
            v = self.db.get_video(arg)
            if v and v["prompts_json"]:
                self.p.send_prompt_package(arg)
            else:
                self.tg.send_message(f"No prompts stored for #{arg}.")
        elif cmd == "/skip" and arg:
            self._job(self.p.decide, arg, "reject")
        else:
            self.tg.send_message(HELP)

    def remind(self) -> None:
        hours = float(self.p.cfg.get("approval.telegram.reminder_hours", 4))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.db.query("SELECT id, title, status FROM videos WHERE status IN "
                             "('awaiting_approval','awaiting_footage','awaiting_caption') "
                             "AND created_at < ? AND (reminded_at IS NULL OR reminded_at < ?)", (cutoff, cutoff))
        for r in rows:
            what = {"awaiting_footage": "is waiting for your video", "awaiting_caption": "is waiting for your caption"
                    }.get(r["status"], "is waiting for your approval")
            self.tg.send_message(f"⏰ Reminder: #{r['id']} “{r['title']}” {what}. Send /pending to see it again.")
            self.db.update_video(r["id"], reminded_at=utcnow())

    def loop(self) -> None:
        offset = self.db.kv_get("telegram_offset")
        timeout = int(self.p.cfg.get("approval.telegram.poll_timeout", 50))
        log.info("Telegram bot running (inbox: %s)", self.p.cfg.path("prompt_mode.inbox_dir", "inbox"))
        last_remind = last_inbox = 0.0
        while True:
            try:
                updates = self.tg.get_updates(offset, min(timeout, 25))
                for u in updates:
                    offset = u["update_id"] + 1
                    self.db.kv_set("telegram_offset", offset)   # persist first: never process an update twice
                    if "callback_query" in u:
                        self.handle_callback(u["callback_query"])
                    elif "message" in u:
                        self.handle_message(u["message"])
                if time.time() - last_inbox > 30:
                    self.poll_inbox()
                    last_inbox = time.time()
                if time.time() - last_remind > 600:
                    self.remind()
                    last_remind = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — network blips, Telegram outages
                log.warning("bot loop error: %s", exc)
                time.sleep(10)


def watch_loop(pipeline, interval: float = 30) -> None:
    """Inbox-only watcher for setups without Telegram."""
    from ..footage.inbox import scan_inbox
    log.info("Watching inbox %s", pipeline.cfg.path("prompt_mode.inbox_dir", "inbox"))
    while True:
        try:
            for m in scan_inbox(pipeline):
                print(m, flush=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("inbox scan failed: %s", exc)
        time.sleep(interval)
