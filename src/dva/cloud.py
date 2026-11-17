"""Cloud mode: one short, stateless run (GitHub Actions every ~10 min).

Each run: process Telegram messages/buttons received since the last run,
pick up videos from the Google Drive inbox, send reminders, start today's
topic once it's past the daily time, and collect metrics when due. Then it
exits; the database is persisted (encrypted) by scripts/cloud_state.sh."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

from .core.db import utcnow

log = logging.getLogger(__name__)
SUCCESS = ("awaiting_approval", "awaiting_footage", "done", "package")


class _Sync:
    def submit(self, fn):
        fn()


def daily_due(pipe) -> bool:
    now = datetime.now(pipe.tz)
    hh, mm = (int(x) for x in str(pipe.cfg.get("channel.daily_time", "09:00")).split(":"))
    if now.time() < dtime(hh, mm):
        return False
    runs = pipe.db.query("SELECT status FROM runs WHERE run_date=? AND kind='daily'", (now.date().isoformat(),))
    if any(r["status"] in SUCCESS for r in runs):
        return False
    return len(runs) < int(pipe.cfg.get("cloud.daily_attempts", 2))


def metrics_due(pipe) -> bool:
    if not pipe.db.one("SELECT id FROM publications WHERE status='published' LIMIT 1"):
        return False
    last = pipe.db.kv_get("metrics_last", 0) or 0
    return time.time() - float(last) > float(pipe.cfg.get("cloud.metrics_every_hours", 6)) * 3600


def reminders_due(pipe) -> bool:
    hours = float(pipe.cfg.get("approval.telegram.reminder_hours", 4))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return pipe.db.one("SELECT id FROM videos WHERE status IN ('awaiting_approval','awaiting_footage','awaiting_caption') "
                       "AND created_at < ? AND (reminded_at IS NULL OR reminded_at < ?) LIMIT 1",
                       (cutoff, cutoff)) is not None


def poll(pipe, check_only: bool = False) -> dict:
    from .footage.drive import DriveInbox
    db, tg = pipe.db, pipe.telegram
    offset = db.kv_get("telegram_offset")
    updates = tg.get_updates(offset, 0) if tg else []
    drive = DriveInbox(pipe.cfg, db)
    drive_new = drive.new_videos() if drive.configured() else []
    work = {"telegram": len(updates), "drive": len(drive_new), "daily": daily_due(pipe),
            "metrics": metrics_due(pipe), "reminders": reminders_due(pipe) and bool(tg)}
    work["any"] = any(bool(v) for v in work.values())
    if check_only or not work["any"]:
        return work

    bot = None
    if tg:
        from .approval.bot import ApprovalBot
        bot = ApprovalBot(pipe)
        bot.jobs = _Sync()
    # 1) your messages and button taps (oldest first; offset saved before handling = never handled twice)
    while updates and bot:
        for u in updates:
            offset = u["update_id"] + 1
            db.kv_set("telegram_offset", offset)
            try:
                bot.handle_update(u)
            except Exception as exc:  # noqa: BLE001
                log.exception("update failed")
                tg.send_message(f"⚠️ Something went wrong handling your message: {exc}")
        updates = tg.get_updates(offset, 0)
    # 2) videos uploaded to the Drive inbox
    for msg in drive.scan(pipe) if drive_new else []:
        pipe.notify(msg)
    # 3) today's topic + prompts
    if daily_due(pipe):
        res = pipe.run(kind="daily")
        log.info("daily run: %s", res.get("status"))
    # 4) reminders, 5) metrics
    if bot and reminders_due(pipe):
        bot.remind()
    if metrics_due(pipe):
        from .analytics.collector import collect
        from .publish.instagram import Instagram
        from .publish.youtube import YouTube
        try:
            n = collect(pipe.cfg, db, YouTube(pipe.cfg), Instagram(pipe.cfg, db))
            log.info("metrics: %d snapshots", n)
        finally:
            db.kv_set("metrics_last", time.time())
    db.kv_set("last_poll", utcnow())
    return work
