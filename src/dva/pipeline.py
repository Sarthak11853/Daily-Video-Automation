"""End-to-end orchestration of the daily run.

discover -> research/verify -> script -> voice -> render -> edit -> QC
-> Telegram approval -> (on approve) publish -> metrics later.

Every stage writes its artefact into output/<date>_run<id>/ so a failed run
can be inspected, resumed, or shipped as a production package.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .core.config import Config
from .core.db import Database
from .core.errors import ConfigError, QCFailed, VerificationFailed
from .core.models import FactSheet
from .core.text import keywords, normalize

log = logging.getLogger(__name__)
DEMO_DIR = Path(__file__).parent / "demo"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@contextlib.contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise RuntimeError("Another dva run is in progress (lock held)") from None
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


class Pipeline:
    def __init__(self, cfg: Config, db: Database | None = None, llm=None, tts=None, telegram=None,
                 discovery=None, fetcher=None, publisher=None):
        self.cfg = cfg
        self.db = db or Database(cfg.data_dir / "dva.sqlite3")
        self._llm = llm
        self._tts = tts
        self._telegram = telegram
        self._discovery = discovery
        self._fetcher = fetcher
        self._publisher = publisher
        self.tz = ZoneInfo(cfg.get("channel.timezone", "Asia/Kolkata"))

    # ------------------------------------------------------------ lazies
    @property
    def llm(self):
        if self._llm is None:
            from .llm.factory import build_llm
            self._llm = build_llm(self.cfg, self.db)
        return self._llm

    @property
    def tts(self):
        if self._tts is None:
            from .tts.engines import pick_engine
            self._tts = pick_engine(self.cfg, self.cfg.root)
        return self._tts

    @property
    def telegram(self):
        if self._telegram is None:
            from .approval.telegram import Telegram
            try:
                self._telegram = Telegram.from_config(self.cfg)
            except ConfigError as exc:
                log.warning("Telegram disabled: %s", exc)
                self._telegram = False
        return self._telegram or None

    def today(self) -> str:
        return datetime.now(self.tz).date().isoformat()

    def notify(self, text: str) -> None:
        log.info("notify: %s", text.splitlines()[0] if text else "")
        if self.telegram:
            try:
                self.telegram.send_message(text)
            except Exception as exc:  # noqa: BLE001
                log.error("Telegram notify failed: %s", exc)

    # ------------------------------------------------------------ stages
    def research(self, out_dir: Path) -> tuple[FactSheet, object]:
        from .discovery.selector import TopicSelector
        from .discovery.sources import Discovery
        from .research.fetch import SourceFetcher
        from .research.verifier import FactVerifier
        disc = self._discovery or Discovery(self.cfg)
        fetcher = self._fetcher or SourceFetcher(self.cfg)
        cands = disc.collect()
        ranked = TopicSelector(self.cfg, self.db, self.llm).select(cands)
        (out_dir / "candidates.json").write_text(json.dumps([c.to_dict() for c in ranked[:30]], ensure_ascii=False,
                                                            indent=2), encoding="utf-8")
        verifier = FactVerifier(self.cfg, self.llm)
        errors = []
        for cand in ranked[: int(self.cfg.get("research.max_topic_attempts", 4))]:
            log.info("researching: %s (%s, score %.2f)", cand.title, cand.source, cand.score)
            try:
                trusted_article = cand.url if cand.source != "google_trends" else \
                    next((n["url"] for n in cand.extra.get("news", []) if n.get("url")), None)
                sources = fetcher.gather(cand.search_query or cand.title, trusted_article, cand.wiki_title)
                sheet = verifier.build(cand, sources)
                return sheet, cand
            except VerificationFailed as exc:
                log.warning("rejected topic: %s", exc)
                errors.append(str(exc))
        raise VerificationFailed("No candidate topic could be verified today: " + " | ".join(errors[-4:]))

    def produce(self, run_id: int, sheet: FactSheet, script: dict, out_dir: Path, topic_id: int | None,
                seed_base: str) -> int:
        """Voice, render, edit, QC, register the video and send it for approval. Returns video id."""
        from .editor.compose import build_audio, compose, thumbnail
        from .editor.subtitles import write_subtitles
        from .editor.timeline import build_timeline
        from .publish.metadata import build_metadata
        from .qc.checks import run_qc, summarize
        from .render.runner import render_video_scenes
        from .tts.engines import voice_scenes

        meta = build_metadata(script, sheet, self.cfg)
        (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.update_run(run_id, stage="voice")
        voice = voice_scenes(script, self.tts, out_dir / "voice")
        v = self.cfg.section("video")
        tl = build_timeline(script["scenes"], voice, float(v.get("transition_seconds", 0.35)),
                            pad=float(v.get("scene_padding_seconds", 0.35)))
        (out_dir / "timeline.json").write_text(json.dumps(tl.to_dict(), ensure_ascii=False, indent=2))
        if tl.total > float(v.get("max_duration", 75)):
            raise QCFailed(f"narration too long ({tl.total:.1f}s) — script must be shortened", {})
        self.db.update_run(run_id, stage="render")
        clips, renderer = render_video_scenes(self.cfg, script["scenes"], [s.clip_len for s in tl.scenes],
                                              out_dir / "render", seed_base)
        self.db.update_run(run_id, stage="edit")
        ass, srt, cues = write_subtitles(tl, self.cfg, out_dir)
        audio = build_audio(tl, voice, script["scenes"], self.cfg, out_dir, int(hashlib.sha1(seed_base.encode()).hexdigest()[:6], 16))
        final = compose(clips, tl, audio, ass, self.cfg, out_dir / "final.mp4")
        thumb = thumbnail(final, min(2.0, tl.total / 3), out_dir / "thumbnail.jpg")
        self.db.update_run(run_id, stage="qc")
        report = run_qc(final, tl, cues, sheet, script, self.cfg, out_dir / "voice_track.wav")
        report["renderer"] = renderer
        report["tts"] = getattr(self.tts, "name", "?")
        if renderer != "blender":
            report["checks"].append({"name": "renderer", "ok": True, "severity": "warning",
                                     "detail": "fallback 2.5D renderer used (Blender unavailable/failed)"})
        (out_dir / "qc_report.json").write_text(json.dumps(report, indent=2))
        log.info(summarize(report))
        if not report["passed"]:
            raise QCFailed(summarize(report), report)
        sha = sha256(final)
        existing = self.db.one("SELECT id FROM videos WHERE video_sha256=?", (sha,))
        if existing:
            return int(existing["id"])
        video_id = self.db.add_video(run_id=run_id, topic_id=topic_id, title=script.get("title") or sheet.topic,
                                     category=sheet.category, status="awaiting_approval", video_path=str(final),
                                     video_sha256=sha, duration=report["duration"],
                                     script_json=json.dumps(script, ensure_ascii=False),
                                     facts_json=json.dumps(sheet.to_dict(), ensure_ascii=False),
                                     metadata_json=json.dumps(meta, ensure_ascii=False), qc_json=json.dumps(report))
        self.send_for_approval(video_id, thumb)
        return video_id

    # ---------------------------------------------------------- approval
    def approval_text(self, video_id: int) -> tuple[str, str]:
        v = self.db.get_video(video_id)
        script, facts, meta, qc = (json.loads(v[k]) for k in ("script_json", "facts_json", "metadata_json", "qc_json"))
        demo = "🧪 DEMO CONTENT — facts hand-curated, re-check before publishing\n" if facts.get("demo") else ""
        warnings = [c for c in qc["checks"] if c["severity"] == "warning"]
        caption = (f"{demo}🎬 #{video_id} {v['title']}\n📂 {facts['category']} · ⏱ {v['duration']}s · "
                   f"QC ✅ ({qc.get('renderer')}, {qc.get('tts')})\n\n"
                   f"YT: {meta['youtube']['title']}\n\nApprove to publish on YouTube Shorts + Instagram Reels.")
        lines = [f"📝 SCRIPT #{video_id} — {facts['topic']}", ""]
        for s in script["scenes"]:
            lines.append(f"{s['id']}. {s['narration_display']}  [{', '.join(s['claim_ids'])}]")
        lines += ["", "✅ VERIFIED FACTS"]
        for c in facts["claims"]:
            if c["verified"]:
                lines.append(f"{c['id']}. {c['text']} ({c['verification_notes']})")
        lines += ["", "📚 SOURCES"] + [f"[{s['id']}] {s['url']}" for s in facts["sources"]]
        lines += ["", "▶️ YOUTUBE", meta["youtube"]["title"], "", meta["youtube"]["description"][:1200],
                  "", "📸 INSTAGRAM", meta["instagram"]["caption"]]
        if warnings:
            lines += ["", "⚠️ " + "; ".join(f"{w['name']}: {w['detail']}" for w in warnings)]
        return caption, "\n".join(lines)

    def send_for_approval(self, video_id: int, thumb: Path | None = None) -> None:
        v = self.db.get_video(video_id)
        caption, details = self.approval_text(video_id)
        if not self.telegram:
            log.warning("Telegram not configured. Approve manually: dva approve %s  (video: %s)", video_id,
                        v["video_path"])
            return
        from .approval.telegram import Telegram
        ids = self.telegram.send_message(details)
        mid = self.telegram.send_video(Path(v["video_path"]), caption, Telegram.keyboard(video_id), thumb,
                                       duration=int(v["duration"] or 60))
        self.db.update_video(video_id, telegram_message_ids=json.dumps(ids + [mid]))

    def decide(self, video_id: int, action: str) -> str:
        """Handle Approve / Reject / Regenerate. Safe to call twice (atomic state transitions)."""
        v = self.db.get_video(video_id)
        if not v:
            return f"Video #{video_id} not found"
        if action == "approve":
            if not self.db.transition_video(video_id, ("awaiting_approval",), "approved"):
                return f"#{video_id} is already '{v['status']}'"
            from .publish.manager import Publisher
            results = (self._publisher or Publisher(self.cfg, self.db)).publish(video_id)
            lines = [f"🚀 Publishing #{video_id}:"]
            for p, r in results.items():
                lines.append(f"• {p}: {r.get('status')} {r.get('url') or ''} {r.get('error') or ''}".rstrip())
                if p == "youtube" and r.get("privacy") == "private" and \
                        self.cfg.get("publish.youtube.privacy_status") == "public":
                    lines.append("  ⚠️ YouTube kept it PRIVATE — your API project needs Google's audit (docs/SETUP.md)")
            if any(r.get("status") != "published" for r in results.values()):
                lines.append(f"Retry failed platforms with: dva publish {video_id}")
            return "\n".join(lines)
        if action == "reject":
            if not self.db.transition_video(video_id, ("awaiting_approval", "approved"), "rejected"):
                return f"#{video_id} is already '{v['status']}'"
            if v["topic_id"]:
                self.db.execute("UPDATE topics SET status='blacklisted' WHERE id=?", (v["topic_id"],))
            return f"❌ #{video_id} rejected; topic blacklisted. Send /new for a fresh video."
        if action == "regen":
            if not self.db.transition_video(video_id, ("awaiting_approval",), "regenerating"):
                return f"#{video_id} is already '{v['status']}'"
            res = self.run(kind="regenerate", regenerate_of=video_id, force=True)
            return f"🔁 Regenerated → {res.get('status')} (video #{res.get('video_id')})"
        return f"unknown action {action}"

    # ----------------------------------------------------------------- run
    def run(self, kind: str = "daily", force: bool = False, regenerate_of: int | None = None,
            demo: bool = False) -> dict:
        with run_lock(self.cfg.data_dir / "run.lock"):
            return self._run(kind, force, regenerate_of, demo)

    def _run(self, kind: str, force: bool, regenerate_of: int | None, demo: bool) -> dict:
        day = self.today()
        if kind == "daily" and not force:
            done = self.db.one("SELECT id, status FROM runs WHERE run_date=? AND kind='daily' AND status IN "
                               "('awaiting_approval','done','package')", (day,))
            if done:
                log.info("Today's run already finished (run %s, %s) — use --force to run again", done["id"], done["status"])
                return {"status": "skipped", "run_id": done["id"]}
        run_id = self.db.start_run(day, kind)
        out_dir = self.cfg.output_dir / f"{day}_run{run_id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.db.update_run(run_id, output_dir=str(out_dir))
        sheet: FactSheet | None = None
        script: dict | None = None
        topic_id = None
        try:
            # ---- research ------------------------------------------------
            self.db.update_run(run_id, stage="research")
            if demo:
                sheet = FactSheet.from_dict(json.loads((DEMO_DIR / "octopus_facts.json").read_text(encoding="utf-8")))
                script = json.loads((DEMO_DIR / "octopus_script.json").read_text(encoding="utf-8"))
                from .script.writer import normalize_script
                script = normalize_script(script, self.cfg)
                cand_source, cand_url, wiki = "demo", sheet.sources[0].url, "Octopus"
            elif regenerate_of:
                old = self.db.get_video(regenerate_of)
                sheet = FactSheet.from_dict(json.loads(old["facts_json"]))
                topic_id = old["topic_id"]
                cand_source = cand_url = wiki = None
            else:
                sheet, cand = self.research(out_dir)
                cand_source, cand_url, wiki = cand.source, cand.url, cand.wiki_title or cand.search_query
            (out_dir / "facts.json").write_text(json.dumps(sheet.to_dict(), ensure_ascii=False, indent=2),
                                                encoding="utf-8")
            if topic_id is None and not demo:
                topic_id = self.db.add_topic(sheet.topic, normalize(sheet.topic), keywords(f"{sheet.topic} {wiki or ''}"),
                                             sheet.category, cand_source, cand_url, wiki, status="used")
            self.db.update_run(run_id, topic_id=topic_id)
            # ---- script --------------------------------------------------
            if script is None:
                self.db.update_run(run_id, stage="script")
                from .analytics.collector import learnings
                from .script.writer import ScriptWriter
                script = ScriptWriter(self.cfg, self.llm).write(sheet, learnings(self.db))
            (out_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            # ---- production ---------------------------------------------
            seed_base = f"{day}:{sheet.topic}:{run_id}"
            video_id = self.produce(run_id, sheet, script, out_dir, topic_id, seed_base)
            if regenerate_of:
                self.db.update_video(regenerate_of, status="superseded")
            self.db.update_run(run_id, status="awaiting_approval", stage="approval")
            return {"status": "awaiting_approval", "run_id": run_id, "video_id": video_id,
                    "video": str(out_dir / "final.mp4")}
        except Exception as exc:  # noqa: BLE001 — any failure ends in a package + notification
            log.error("run %s failed at stage %s: %s", run_id, self._stage(run_id), exc)
            log.debug(traceback.format_exc())
            if regenerate_of:
                self.db.transition_video(regenerate_of, ("regenerating",), "awaiting_approval")
            return self._fail(run_id, out_dir, sheet, script, exc)

    def _stage(self, run_id: int) -> str:
        r = self.db.one("SELECT stage FROM runs WHERE id=?", (run_id,))
        return r["stage"] if r else "?"

    def _fail(self, run_id: int, out_dir: Path, sheet, script, exc: Exception) -> dict:
        stage = self._stage(run_id)
        err = f"{type(exc).__name__}: {exc}"
        if sheet is None or script is None:
            self.db.update_run(run_id, status="failed", error=err[:2000])
            self.notify(f"⚠️ Daily video run #{run_id} failed at '{stage}'.\n{err[:1500]}\n\n"
                        "Nothing was published. Check data/logs/dva.log, then `dva run --force`.")
            return {"status": "failed", "run_id": run_id, "stage": stage, "error": err}
        from .editor.package import build_package
        meta = None
        if (out_dir / "metadata.json").exists():
            meta = json.loads((out_dir / "metadata.json").read_text(encoding="utf-8"))
        extras = [out_dir / n for n in ("voice", "subtitles.srt", "subtitles.ass", "final.mp4", "qc_report.json",
                                        "timeline.json")]
        extras += sorted((out_dir / "render").glob("scene_*.mp4")) if (out_dir / "render").exists() else []
        zpath = build_package(out_dir, script, sheet, meta, f"{stage}: {err[:300]}", extras)
        self.db.update_run(run_id, status="package", error=err[:2000])
        msg = (f"📦 Run #{run_id}: automatic {stage} failed — production package ready.\n{err[:1200]}\n\n"
               f"Topic: {sheet.topic}\nPackage: {zpath}")
        self.notify(msg)
        if self.telegram and zpath.stat().st_size < 49e6:
            try:
                self.telegram.send_document(zpath, f"Production package — run #{run_id}")
            except Exception as e2:  # noqa: BLE001
                log.error("could not send package: %s", e2)
        return {"status": "package", "run_id": run_id, "stage": stage, "error": err, "package": str(zpath)}

    def cleanup(self, keep_days: int = 14) -> int:
        """Delete heavy intermediates of old runs (keeps final.mp4 + json)."""
        n = 0
        for d in self.cfg.output_dir.glob("*_run*"):
            try:
                age = (datetime.now() - datetime.fromtimestamp(d.stat().st_mtime)).days
            except OSError:
                continue
            if age > keep_days:
                for sub in ("render", "voice"):
                    if (d / sub).exists():
                        shutil.rmtree(d / sub, ignore_errors=True)
                        n += 1
        return n
