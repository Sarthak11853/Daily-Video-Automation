"""Command-line entry point:  python -m dva <command>   (or `dva <command>`)."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .core.config import Config
from .core.log import setup_logging


def _pipeline(args):
    from .pipeline import Pipeline
    cfg = Config.load(args.config)
    setup_logging(cfg.data_dir / "logs", logging.DEBUG if args.verbose else logging.INFO)
    return Pipeline(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dva", description="Daily AI animated facts video automation")
    ap.add_argument("--config", default=None, help="path to settings.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the daily pipeline (idempotent per day)")
    r.add_argument("--force", action="store_true", help="run even if today's video already exists")
    r.add_argument("--json", action="store_true", help="print machine-readable result (for n8n)")
    d = sub.add_parser("demo", help="render the built-in demo video (no API keys needed)")
    d.add_argument("--fast", action="store_true", help="Blender 'stills' mode at 50%% resolution (quick preview)")
    d.add_argument("--fallback", action="store_true", help="use the Pillow fallback renderer only")
    d.add_argument("--tts", default=None, help="force a TTS engine (piper|macos_say|espeak|tone)")
    sub.add_parser("bot", help="run the Telegram approval listener (long-running)")
    for name in ("approve", "reject", "regen"):
        p = sub.add_parser(name, help=f"{name} a video manually (same as the Telegram button)")
        p.add_argument("video_id", type=int)
    p = sub.add_parser("publish", help="(re)try publishing an approved video")
    p.add_argument("video_id", type=int)
    p = sub.add_parser("mark-published", help="record a manual upload to stop automatic retries")
    p.add_argument("video_id", type=int)
    p.add_argument("platform", choices=["youtube", "instagram"])
    p.add_argument("url")
    p = sub.add_parser("reset-publication", help="clear an 'unknown' publication state after checking manually")
    p.add_argument("video_id", type=int)
    p.add_argument("platform", choices=["youtube", "instagram"])
    sub.add_parser("metrics", help="collect metrics for published videos")
    sub.add_parser("report", help="print performance report")
    sub.add_parser("auth-youtube", help="one-time YouTube OAuth consent")
    sub.add_parser("doctor", help="check tools, credentials and quotas")
    sub.add_parser("telegram-whoami", help="print chat ids that messaged the bot")
    nt = sub.add_parser("notify", help="send a Telegram message (used by n8n error branch)")
    nt.add_argument("text")
    c = sub.add_parser("cleanup", help="delete heavy intermediates of old runs")
    c.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        from .core.config import Config as C
        from .pipeline import Pipeline
        overrides: dict = {}
        if args.fast:
            overrides = {"render": {"blender": {"mode": "stills", "resolution_percentage": 50}}}
        if args.fallback:
            overrides.setdefault("render", {})["engine_order"] = ["fallback"]
        if args.tts:
            overrides["tts"] = {"engine_order": [args.tts]}
        cfg = C.load(args.config, overrides=overrides)
        setup_logging(cfg.data_dir / "logs", logging.DEBUG if args.verbose else logging.INFO)
        res = Pipeline(cfg).run(kind="demo", demo=True, force=True)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "awaiting_approval" else 1

    pipe = _pipeline(args)
    cfg, db = pipe.cfg, pipe.db
    if args.cmd == "run":
        res = pipe.run(kind="daily", force=args.force)
        print(json.dumps(res) if args.json else json.dumps(res, indent=2))
        return 0 if res["status"] in ("awaiting_approval", "skipped") else 2 if res["status"] == "package" else 1
    if args.cmd == "bot":
        from .approval.bot import ApprovalBot
        ApprovalBot(pipe).loop()
    elif args.cmd in ("approve", "reject", "regen"):
        print(pipe.decide(args.video_id, args.cmd))
    elif args.cmd == "publish":
        from .publish.manager import Publisher
        db.transition_video(args.video_id, ("publishing",), "approved")
        print(json.dumps(Publisher(cfg, db).publish(args.video_id), indent=2))
    elif args.cmd == "mark-published":
        from .core.db import utcnow
        pub = db.claim_publication(args.video_id, args.platform)
        db.update_publication(pub["id"], status="published", url=args.url, published_at=utcnow(),
                              external_id=args.url.rstrip("/").split("/")[-1])
        print("ok")
    elif args.cmd == "reset-publication":
        pub = db.claim_publication(args.video_id, args.platform)
        db.update_publication(pub["id"], status="failed", upload_session=None)
        print("ok — next `dva publish` will upload again")
    elif args.cmd == "metrics":
        from .analytics.collector import collect
        from .publish.instagram import Instagram
        from .publish.youtube import YouTube
        print(f"collected {collect(cfg, db, YouTube(cfg), Instagram(cfg, db))} snapshots")
    elif args.cmd == "report":
        from .analytics.collector import report
        print(report(db))
    elif args.cmd == "auth-youtube":
        from .publish.youtube import YouTube
        YouTube(cfg).authorize()
    elif args.cmd == "telegram-whoami":
        tg = pipe.telegram
        if not tg:
            from .approval.telegram import Telegram
            tg = Telegram(cfg.secret("TELEGRAM_BOT_TOKEN"), "0")
        for u in tg.get_updates(None, 0):
            m = u.get("message") or {}
            print(m.get("chat", {}).get("id"), m.get("from", {}).get("username"), m.get("text"))
    elif args.cmd == "doctor":
        from .doctor import doctor
        return doctor(pipe)
    elif args.cmd == "notify":
        pipe.notify(args.text)
    elif args.cmd == "cleanup":
        print(f"cleaned {pipe.cleanup(args.days)} folders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
