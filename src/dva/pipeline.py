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


class DurationMismatch(QCFailed):
    """The voiced script is too short/long; carries the measured length for a rewrite."""

    def __init__(self, seconds: float, wps: float):
        super().__init__(f"voiced narration gives a {seconds:.1f}s video")
        self.seconds = seconds
        self.wps = wps
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
        # calibrate the TTS speaking rate (words/sec) so future scripts hit the target length
        words = sum(len(s["narration_display"].split()) for s in script["scenes"])
        wps = words / max(0.1, sum(x["duration"] for x in voice))
        old = self.db.kv_get("tts_wps")
        self.db.kv_set("tts_wps", round(wps if old is None else 0.7 * old + 0.3 * wps, 3))
        if not float(v.get("min_duration", 45)) <= tl.total <= float(v.get("max_duration", 75)):
            raise DurationMismatch(tl.total, wps)
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

    # ------------------------------------------------------- prompt mode
    def prompt_package(self, run_id: int, sheet: FactSheet, script: dict, out_dir: Path,
                       topic_id: int | None) -> int:
        """AI-video prompts (+ suggested script/captions, optional TTS voiceover) sent to you; the video
        then waits for your footage."""
        from .editor.compose import build_audio
        from .editor.subtitles import write_subtitles
        from .editor.timeline import build_timeline
        from .prompting.video_prompts import VideoPromptWriter, save_package
        from .publish.metadata import build_metadata
        from .tts.engines import voice_scenes

        cfg = self.cfg
        meta = build_metadata(script, sheet, cfg, mode="prompt")
        (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.db.update_run(run_id, stage="voice")
        voice = None
        if cfg.get("prompt_mode.generate_voiceover", False):
            try:
                voice = voice_scenes(script, self.tts, out_dir / "voice")
            except Exception as exc:  # noqa: BLE001 — prompts are still useful without our voiceover
                log.warning("No voiceover generated (%s); prompts will use estimated timings", exc)
        v = cfg.section("video")
        if voice:
            timing = voice
            words = sum(len(s["narration_display"].split()) for s in script["scenes"])
            wps = words / max(0.1, sum(x["duration"] for x in voice))
            old = self.db.kv_get("tts_wps")
            self.db.kv_set("tts_wps", round(wps if old is None else 0.7 * old + 0.3 * wps, 3))
        else:
            wps = float(self.db.kv_get("tts_wps") or cfg.get("script.words_per_second", 2.55))
            timing = [{"id": s["id"], "duration": round(len(s["narration_display"].split()) / wps, 2)}
                      for s in script["scenes"]]
        tl = build_timeline(script["scenes"], timing, transition=0.0, pad=float(v.get("scene_padding_seconds", 0.35)))
        if not float(v.get("min_duration", 45)) <= tl.total <= float(v.get("max_duration", 75)):
            raise DurationMismatch(tl.total, wps)
        (out_dir / "timeline.json").write_text(json.dumps(tl.to_dict(), ensure_ascii=False, indent=2))
        if voice:
            write_subtitles(tl, cfg, out_dir)
            (out_dir / "voice.json").write_text(json.dumps(voice, indent=2))
            seed = int(hashlib.sha1(f"{run_id}:{sheet.topic}".encode()).hexdigest()[:6], 16)
            build_audio(tl, voice, script["scenes"], cfg, out_dir, seed, sfx_enabled=False,
                        out_name="voiceover.wav", music_on=False)
            build_audio(tl, voice, script["scenes"], cfg, out_dir, seed, sfx_enabled=False,
                        out_name="voiceover_with_music.wav")
            for tmp in ("voice_track.wav", "sfx_track.wav", "music_track.wav"):
                (out_dir / tmp).unlink(missing_ok=True)
        self.db.update_run(run_id, stage="prompts")
        pkg = VideoPromptWriter(cfg, self.llm).write(script, sheet, {st.id: st.clip_len for st in tl.scenes})
        video_id = self.db.add_video(run_id=run_id, topic_id=topic_id, title=script.get("title") or sheet.topic,
                                     category=sheet.category, status="awaiting_footage", mode="prompt",
                                     duration=tl.total, script_json=json.dumps(script, ensure_ascii=False),
                                     facts_json=json.dumps(sheet.to_dict(), ensure_ascii=False),
                                     metadata_json=json.dumps(meta, ensure_ascii=False),
                                     prompts_json=json.dumps(pkg, ensure_ascii=False), qc_json="{}")
        save_package(out_dir, pkg, script, sheet, meta, video_id)
        self.send_prompt_package(video_id)
        return video_id

    def big_file_hint(self, video_id: int | None = None) -> str:
        """Where to send videos bigger than Telegram's 20 MB bot limit."""
        if self.cfg.get("cloud.enabled", False) or self.cfg.secret(self.cfg.get("cloud.drive_folder_id_env",
                                                                                 "DVA_DRIVE_FOLDER_ID")):
            name = f"{video_id}.mp4" if video_id else "the video"
            return (f"Upload it to your Google Drive folder “{self.cfg.get('cloud.drive_folder_name', 'DVA Inbox')}” "
                    f"instead (e.g. as {name}) and send the caption here as a text message.")
        inbox = self.cfg.path("prompt_mode.inbox_dir", "inbox")
        return f"Drop it into the inbox folder instead (name it {video_id}.mp4): {inbox}"

    def send_prompt_package(self, video_id: int) -> None:
        from html import escape
        v = self.db.get_video(video_id)
        run = self.db.one("SELECT output_dir FROM runs WHERE id=?", (v["run_id"],))
        out_dir = Path(run["output_dir"])
        if not self.telegram:
            log.warning("Telegram not configured. Prompts for #%s: %s — then: dva submit <video> --video-id %s",
                        video_id, out_dir / "PROMPTS.md", video_id)
            return
        from .approval.telegram import Telegram
        tg = self.telegram
        pkg, meta = json.loads(v["prompts_json"]), json.loads(v["metadata_json"])
        facts = json.loads(v["facts_json"])
        n_clips = sum(len(s["clips"]) for s in pkg["scenes"])
        has_voice = (out_dir / "voiceover.wav").exists()
        inbox = self.cfg.path("prompt_mode.inbox_dir", "inbox")
        demo = "🧪 DEMO CONTENT — facts hand-curated, re-check before publishing\n" if facts.get("demo") else ""
        we_add = self.cfg.get("prompt_mode.post_process", "none") != "none"
        step3 = ("Keep it <b>silent</b>: I'll add the voiceover, music and subtitles."
                 if we_add and has_voice else
                 "Add your voiceover (easy English script below), subtitles and the background music below.")
        caption_mode = self.cfg.get("prompt_mode.caption_source", "yours_or_generated")
        step4 = ("4. Send me the finished video as a <b>file</b> (≤ 20 MB). The caption, hashtags and YouTube tags "
                 "are added automatically (type your own caption with the video only if you want to replace them)."
                 if caption_mode != "yours_only" else
                 "4. Send me the finished video as a <b>file</b> (≤ 20 MB) <b>with your caption</b> in the same "
                 "message (or as text right after).")
        ids = tg.send_message(
            f"{demo}🎬 <b>Video #{video_id}: {escape(v['title'] or '')}</b>\n"
            f"📂 {escape(v['category'] or '')} · ⏱ ~{pkg['total_seconds']:.0f}s · {len(pkg['scenes'])} scenes · "
            f"{n_clips} clips\n💡 {escape(facts.get('angle', ''))}\n\n"
            "<b>What to do</b>\n1. Generate the clips below in your AI video tool (each prompt is complete, "
            "with style and character included).\n2. Edit them together.\n"
            f"3. {step3}\n"
            f"{step4} Bigger than 20 MB? {escape(self.big_file_hint(video_id))}\n"
            "I publish it to YouTube Shorts + Instagram Reels exactly as you made it.",
            parse_mode="HTML")
        claims = [c for c in facts["claims"] if c.get("verified")]
        ids += tg.send_message("✅ VERIFIED FACTS (safe to say in your voiceover)\n"
                               + "\n".join(f"• {c['text']}" for c in claims)
                               + "\n\n📚 SOURCES\n" + "\n".join(f"• {s['url']}" for s in facts["sources"]))
        ids += tg.send_message("📜 VOICEOVER SCRIPT (easy English; use it or write your own)\n"
                               + "\n".join(f"{s['id']}. ({s['seconds']:.0f}s) {s['voiceover']}" for s in pkg["scenes"]))
        m = pkg.get("music") or {}
        if m:
            ids += tg.send_message(
                "🎵 <b>Background music</b>\n"
                f"• Genre: {escape(str(m.get('genre', '')))}\n• Mood: {escape(str(m.get('mood', '')))}\n"
                f"• Tempo: ~{escape(str(m.get('tempo_bpm', '')))} BPM\n"
                f"• Instruments: {escape(str(m.get('instruments', '')))}\n"
                f"• Energy: {escape(str(m.get('energy', '')))}\n"
                f"• Search in CapCut / YouTube Audio Library: <code>{escape(str(m.get('search_keywords', '')))}</code>\n"
                f"• Avoid: {escape(str(m.get('avoid', '')))}\n"
                "Use one instrumental track for the whole video, ~15–20 dB under your voice. Only use royalty-free "
                "or in-app library music, otherwise the post can be muted or claimed.", parse_mode="HTML")
        for s in pkg["scenes"]:
            head = (f"<b>Scene {s['id']} · {s['seconds']:.1f}s</b>"
                    + (f" · text: <b>{escape(s['on_screen_text'])}</b>" if s.get("on_screen_text") else "")
                    + f"\n🗣 <i>{escape(s['voiceover'])}</i>"
                    + (f"\n🎵 {escape(s['music_cue'])}" if s.get("music_cue") else "") + "\n")
            body = ""
            for c in s["clips"]:
                block = f"\n<b>Clip {c['clip']} ({c['seconds']:.1f}s)</b>\n<pre>{escape(c['prompt'])}</pre>"
                if len(head) + len(body) + len(block) > 3900:
                    ids += tg.send_message(head + body, parse_mode="HTML")
                    head, body = f"<b>Scene {s['id']} (cont.)</b>\n", ""
                body += block
            ids += tg.send_message(head + body, parse_mode="HTML")
        ids += tg.send_message(f"🚫 <b>Negative prompt</b> (if your tool has one)\n<pre>{escape(pkg['negative_prompt'])}</pre>",
                               parse_mode="HTML")
        if len(pkg["single_prompt"]) < 3800:
            ids += tg.send_message(f"🎞 <b>Single prompt</b> (tools that make long videos)\n"
                                   f"<pre>{escape(pkg['single_prompt'])}</pre>", parse_mode="HTML")
        for name, cap in (("PROMPTS.md", "All prompts, script, facts, sources and captions"),
                          ("voiceover_with_music.wav", "Voiceover + music (put under the video from 0:00)"),
                          ("voiceover.wav", "Voice only"), ("subtitles.srt", "Subtitles (timed to the voiceover)")):
            f = out_dir / name
            if f.exists():
                try:
                    ids.append(tg.send_document(f, cap))
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not send %s: %s", name, exc)
        ids += tg.send_message(
            f"💬 Caption, hashtags and tags that will be posted for #{video_id} (automatic)\n\n"
            f"▶️ YouTube title: {meta['youtube']['title']}\n🏷 YouTube tags: {', '.join(meta['youtube'].get('tags', []))[:600]}"
            f"\n\n📸 Instagram caption:\n{meta['instagram']['caption'][:1500]}"
            f"\n\n➡️ Reply to this message with your finished video.",
            reply_markup=Telegram.prompt_keyboard(video_id))
        self.db.update_video(video_id, telegram_message_ids=json.dumps(ids))

    # ---------------------------------------------------------- approval
    def approval_text(self, video_id: int) -> tuple[str, str]:
        v = self.db.get_video(video_id)
        script, facts, meta, qc = (json.loads(v[k]) for k in ("script_json", "facts_json", "metadata_json", "qc_json"))
        demo = "🧪 DEMO CONTENT — facts hand-curated, re-check before publishing\n" if facts.get("demo") else ""
        warnings = [c for c in qc["checks"] if c["severity"] == "warning"]
        how = (f"{qc.get('renderer')}, {qc.get('tts')}" if qc.get("renderer")
               else "your footage: " + "; ".join(qc.get("notes", [])))
        caption = (f"{demo}🎬 #{video_id} {v['title']}\n📂 {facts['category']} · ⏱ {v['duration']}s · "
                   f"QC ✅ ({how})\n\n"
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
        keyboard = Telegram.keyboard(video_id, regen=(v["mode"] or "full") != "prompt")
        mid = self.telegram.send_video(Path(v["video_path"]), caption, keyboard, thumb,
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
            return self.publish_approved(video_id)
        if action == "reject":
            if not self.db.transition_video(video_id, ("awaiting_approval", "approved", "awaiting_footage",
                                                       "awaiting_caption"), "rejected"):
                return f"#{video_id} is already '{v['status']}'"
            if v["topic_id"]:
                self.db.execute("UPDATE topics SET status='blacklisted' WHERE id=?", (v["topic_id"],))
            return f"❌ #{video_id} rejected; topic blacklisted. Send /new for a fresh video."
        if action == "regen":
            prev = v["status"]
            if not self.db.transition_video(video_id, ("awaiting_approval", "awaiting_footage"), "regenerating"):
                return f"#{video_id} is already '{v['status']}'"
            res = self.run(kind="regenerate", regenerate_of=video_id, force=True,
                           mode="prompt" if prev == "awaiting_footage" else None)
            return f"🔁 Regenerated → {res.get('status')} (video #{res.get('video_id')})"
        return f"unknown action {action}"

    def publish_approved(self, video_id: int) -> str:
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

    # ----------------------------------------------------------------- run
    def run(self, kind: str = "daily", force: bool = False, regenerate_of: int | None = None,
            demo: bool = False, mode: str | None = None) -> dict:
        mode = mode or self.cfg.get("workflow.mode", "prompt")
        with run_lock(self.cfg.data_dir / "run.lock"):
            return self._run(kind, force, regenerate_of, demo, mode)

    def _run(self, kind: str, force: bool, regenerate_of: int | None, demo: bool, mode: str = "full") -> dict:
        day = self.today()
        if kind == "daily" and not force:
            done = self.db.one("SELECT id, status FROM runs WHERE run_date=? AND kind='daily' AND status IN "
                               "('awaiting_approval','awaiting_footage','done','package')", (day,))
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
            writer = None
            if script is None:
                self.db.update_run(run_id, stage="script")
                from .analytics.collector import learnings
                from .script.writer import ScriptWriter
                writer = ScriptWriter(self.cfg, self.llm, self.db.kv_get("tts_wps"))
                script = writer.write(sheet, learnings(self.db))
            (out_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            # ---- prompt mode: hand over prompts + voiceover, wait for your footage --------
            if mode == "prompt":
                try:
                    video_id = self.prompt_package(run_id, sheet, script, out_dir, topic_id)
                except DurationMismatch as dm:
                    if writer is None:
                        raise
                    script = self._rewrite_for_length(writer, sheet, dm, run_id, out_dir)
                    video_id = self.prompt_package(run_id, sheet, script, out_dir, topic_id)
                if regenerate_of:
                    self.db.update_video(regenerate_of, status="superseded")
                self.db.update_run(run_id, status="awaiting_footage", stage="awaiting_footage")
                return {"status": "awaiting_footage", "run_id": run_id, "video_id": video_id,
                        "prompts": str(out_dir / "PROMPTS.md")}
            # ---- production ---------------------------------------------
            seed_base = f"{day}:{sheet.topic}:{run_id}"
            try:
                video_id = self.produce(run_id, sheet, script, out_dir, topic_id, seed_base)
            except DurationMismatch as dm:
                if writer is None:
                    raise
                script = self._rewrite_for_length(writer, sheet, dm, run_id, out_dir)
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
                self.db.transition_video(regenerate_of, ("regenerating",),
                                         "awaiting_footage" if mode == "prompt" else "awaiting_approval")
            return self._fail(run_id, out_dir, sheet, script, exc)

    def _rewrite_for_length(self, writer, sheet: FactSheet, dm: "DurationMismatch", run_id: int,
                            out_dir: Path) -> dict:
        target = float(self.cfg.get("video.target_duration", 60))
        log.warning("%s — rewriting once for ~%ss", dm, target)
        self.db.update_run(run_id, stage="script")
        writer.wps = dm.wps
        script = writer.write(sheet, f"\nNOTE: your last draft was {dm.seconds:.0f}s when spoken; "
                                     f"the video must be about {target:.0f}s.\n")
        (out_dir / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        return script

    def _stage(self, run_id: int) -> str:
        r = self.db.one("SELECT stage FROM runs WHERE id=?", (run_id,))
        return r["stage"] if r else "?"

    def notify_failure(self, run_id: int, stage: str, exc: Exception, err: str) -> None:
        """Calm, actionable messages: one 'delayed' note, silent retries, one final alert."""
        from .core.errors import ProviderUnavailable, QuotaExceeded, TransientError
        run = self.db.one("SELECT kind, run_date FROM runs WHERE id=?", (run_id,))
        cloud = bool(self.cfg.get("cloud.enabled", False))
        busy = isinstance(exc, (ProviderUnavailable, QuotaExceeded, TransientError, VerificationFailed))
        why = ("Google's free AI (Gemini) is overloaded or out of free quota right now"
               if isinstance(exc, (ProviderUnavailable, QuotaExceeded)) else
               "no topic could be verified against trusted sources" if isinstance(exc, VerificationFailed)
               else "a temporary problem")
        retry_how = ("send /new later (or GitHub → Actions → dva → Run workflow → run)" if cloud
                     else "run `dva run --force` later")
        if run and run["kind"] == "daily" and busy:
            tries = self.db.one("SELECT COUNT(*) n FROM runs WHERE run_date=? AND kind='daily'",
                                (run["run_date"],))["n"]
            limit = int(self.cfg.get("cloud.daily_attempts", 8))
            every = int(self.cfg.get("cloud.retry_minutes", 30))
            if tries < limit:
                if tries == 1:
                    self.notify(f"⏳ Today's topic is delayed: {why}. I'll retry automatically every ~{every} "
                                "minutes. Nothing for you to do.")
                return
            self.notify(f"⚠️ Couldn't prepare today's topic after {tries} tries: {why}.\n"
                        f"Nothing was published. I'll try again tomorrow, or {retry_how}.\n\nLast error: {err[:600]}")
            return
        self.notify(f"⚠️ Run #{run_id} failed at '{stage}': {why}.\nNothing was published. "
                    f"You can try again: {retry_how}.\n\nError: {err[:600]}")

    def _fail(self, run_id: int, out_dir: Path, sheet, script, exc: Exception) -> dict:
        stage = self._stage(run_id)
        err = f"{type(exc).__name__}: {exc}"
        if sheet is None or script is None:
            self.db.update_run(run_id, status="failed", error=err[:2000])
            self.notify_failure(run_id, stage, exc, err)
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
