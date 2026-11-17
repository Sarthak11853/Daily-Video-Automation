"""Long-running Telegram listener: handles Approve / Reject / Regenerate
buttons and a few commands. Runs locally (launchd keeps it alive)."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..core.db import utcnow

log = logging.getLogger(__name__)

HELP = ("/status – latest runs & pending videos\n/new – make a new video now\n/metrics – performance summary\n"
        "/pending – resend videos waiting for approval\n/help – this message")


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
        if not v or v["status"] != "awaiting_approval":
            self.tg.answer_callback(cq["id"], f"Already handled ({v['status'] if v else 'missing'})")
            return
        labels = {"approve": "Publishing…", "reject": "Rejected", "regen": "Regenerating (this takes a while)…"}
        self.tg.answer_callback(cq["id"], labels.get(action, "?"))
        self.tg.clear_buttons(cq["message"]["message_id"])
        self._job(self.p.decide, video_id, action)

    def handle_message(self, msg: dict) -> None:
        chat = str(msg.get("chat", {}).get("id"))
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            self.tg.send_message(f"Hi! Your chat id is {chat}. Put it in .env as TELEGRAM_CHAT_ID.\n\n{HELP}", chat_id=chat)
            return
        if chat != self.tg.chat_id:
            return
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd == "/status":
            runs = self.db.query("SELECT id, run_date, kind, status, stage FROM runs ORDER BY id DESC LIMIT 5")
            pend = self.db.query("SELECT id, title FROM videos WHERE status='awaiting_approval'")
            out = ["Recent runs:"] + [f"#{r['id']} {r['run_date']} {r['kind']} {r['status']} ({r['stage']})" for r in runs]
            out += ["", f"Pending approval: {', '.join(f'#{v['id']} {v['title']}' for v in pend) or 'none'}"]
            out += ["", "Worker: busy" if self.busy else "Worker: idle"]
            self.tg.send_message("\n".join(out))
        elif cmd == "/new":
            if self.busy:
                self.tg.send_message("A job is already running, please wait.")
            else:
                self.tg.send_message("Starting a new video… I'll send it when ready.")
                self._job(lambda: (lambda r: f"Run finished: {r.get('status')} {r.get('error', '')[:500]}")(
                    self.p.run(kind="manual", force=True)))
        elif cmd == "/metrics":
            from ..analytics.collector import report
            self.tg.send_message(report(self.db))
        elif cmd == "/pending":
            for v in self.db.query("SELECT id FROM videos WHERE status='awaiting_approval'"):
                self.p.send_for_approval(v["id"])
        else:
            self.tg.send_message(HELP)

    def remind(self) -> None:
        hours = float(self.p.cfg.get("approval.telegram.reminder_hours", 4))
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.db.query("SELECT id, title FROM videos WHERE status='awaiting_approval' AND created_at < ? "
                             "AND (reminded_at IS NULL OR reminded_at < ?)", (cutoff, cutoff))
        for r in rows:
            self.tg.send_message(f"⏰ Reminder: video #{r['id']} “{r['title']}” is waiting for your approval. "
                                 "Scroll up to the video, or send /pending.")
            self.db.update_video(r["id"], reminded_at=utcnow())

    def loop(self) -> None:
        offset = self.db.kv_get("telegram_offset")
        timeout = int(self.p.cfg.get("approval.telegram.poll_timeout", 50))
        log.info("Telegram approval bot running")
        last_remind = 0.0
        while True:
            try:
                updates = self.tg.get_updates(offset, timeout)
                for u in updates:
                    offset = u["update_id"] + 1
                    self.db.kv_set("telegram_offset", offset)   # persist first: never process an update twice
                    if "callback_query" in u:
                        self.handle_callback(u["callback_query"])
                    elif "message" in u:
                        self.handle_message(u["message"])
                if time.time() - last_remind > 600:
                    self.remind()
                    last_remind = time.time()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — network blips, Telegram outages
                log.warning("bot loop error: %s", exc)
                time.sleep(10)
