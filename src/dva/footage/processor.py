"""Prompt mode, second half: take the video you made (one file or several clips)
and publish it with your caption. By default the video is uploaded exactly as
you made it; it is only re-encoded when a platform would reject the file
(wrong codec/shape/frame rate) or when clips need joining. Optional extras
(voiceover, subtitles, branding) exist but are off unless you enable them."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from ..core.db import utcnow
from ..core.errors import QCFailed
from ..core.models import FactSheet

log = logging.getLogger(__name__)
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise QCFailed(f"{path.name} is not a readable video file")
    info = json.loads(r.stdout)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vs:
        raise QCFailed(f"{path.name} has no video stream")
    au = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    rot = 0
    for sd in vs.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(float(sd["rotation"])))
    w, h = int(vs["width"]), int(vs["height"])
    if rot in (90, 270):
        w, h = h, w
    num, den = (vs.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den or 0) else 0.0
    return {"width": w, "height": h, "duration": float(info["format"].get("duration") or vs.get("duration") or 0),
            "has_audio": au is not None, "vcodec": vs.get("codec_name"), "pix_fmt": vs.get("pix_fmt"),
            "acodec": au.get("codec_name") if au else None, "fps": fps, "rotated": rot in (90, 270),
            "size_mb": int(info["format"].get("size") or path.stat().st_size) / 1e6}


def platform_ready(info: dict) -> list[str]:
    """Reasons a file can't be uploaded untouched (empty list = upload as-is)."""
    why = []
    if info["vcodec"] not in ("h264", "hevc"):
        why.append(f"video codec {info['vcodec']}")
    if info["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        why.append(f"pixel format {info['pix_fmt']}")
    if info["has_audio"] and info["acodec"] != "aac":
        why.append(f"audio codec {info['acodec']}")
    ar = info["width"] / max(1, info["height"])
    if abs(ar - 9 / 16) / (9 / 16) > 0.01:
        why.append(f"not 9:16 ({info['width']}x{info['height']})")
    if info["width"] < 720:
        why.append(f"low resolution ({info['width']}x{info['height']})")
    if not 23 <= info["fps"] <= 60.5:
        why.append(f"{info['fps']:.1f} fps")
    if info["size_mb"] > 280:
        why.append(f"{info['size_mb']:.0f} MB")
    if info["rotated"]:
        why.append("rotation metadata")
    return why


def is_silent(path: Path, threshold_db: float = -50.0) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume: (-?[\d.]+) dB", r.stderr)
    return m is None or float(m.group(1)) < threshold_db


def normalize_clip(src: Path, out: Path, w: int, h: int, fps: int) -> dict:
    """Conform any clip to WxH@fps H.264 + 48 kHz stereo AAC. Non-9:16 input gets a blurred fill."""
    info = probe(src)
    ar = info["width"] / max(1, info["height"])
    target = w / h
    if abs(ar - target) / target < 0.03:
        vf = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps},format=yuv420p[v]"
    else:
        vf = (f"[0:v]split=2[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
              f"boxblur=30:3,eq=brightness=-0.08[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[v]")
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if info["has_audio"]:
        amap = ["-map", "0:a:0"]
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        amap = ["-map", "1:a:0", "-shortest"]
    args += ["-filter_complex", vf, "-map", "[v]", *amap, "-c:v", "libx264", "-preset", "medium", "-crf", "16",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(out)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise QCFailed(f"could not convert {src.name}: {r.stderr[-400:]}")
    info["reframed"] = abs(ar - target) / target >= 0.03
    return info


def concat(parts: list[Path], out: Path) -> Path:
    if len(parts) == 1:
        shutil.copy2(parts[0], out)
        return out
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        raise QCFailed(f"could not join clips: {r.stderr[-400:]}")
    return out


class FootageProcessor:
    def __init__(self, pipeline):
        self.p = pipeline
        self.cfg = pipeline.cfg
        self.db = pipeline.db

    def pending(self) -> list:
        return self.db.query("SELECT * FROM videos WHERE status='awaiting_footage' ORDER BY id DESC")

    def resolve(self, hint: str | int | None = None):
        """Which pending video does an upload belong to? (#id in name/caption, else the newest pending)."""
        pend = self.pending()
        if not pend:
            return None
        ids = {r["id"]: r for r in pend}
        for tok in re.findall(r"\d+", str(hint or "")):
            if int(tok) in ids:
                return ids[int(tok)]
        return pend[0]

    @staticmethod
    def clean_caption(text: str | None, video_id: int) -> str | None:
        """Drop a bare '#<id>' tag used to address the video; keep everything else exactly as written."""
        if not text:
            return None
        text = re.sub(rf"(?<!\w)#{video_id}(?!\w)", "", text).strip()
        return text or None

    def process(self, video_id: int, inputs: list[Path], caption: str | None = None) -> str:
        if not self.db.transition_video(video_id, ("awaiting_footage",), "processing_footage"):
            v = self.db.get_video(video_id)
            return f"Video #{video_id} is '{v['status'] if v else 'missing'}' — not waiting for footage."
        try:
            return self._process(video_id, inputs, self.clean_caption(caption, video_id))
        except Exception as exc:  # noqa: BLE001 — always give the video back to the user
            log.exception("footage processing failed")
            self.db.update_video(video_id, status="awaiting_footage")
            detail = getattr(exc, "report", None)
            extra = ""
            if detail:
                bad = [c for c in detail.get("checks", []) if not c["ok"] and c["severity"] == "error"]
                extra = "\n" + "\n".join(f"✗ {c['name']}: {c['detail']}" for c in bad)
            return f"⚠️ Couldn't use the video for #{video_id}: {exc}{extra}\nFix it and send it again."

    def _process(self, video_id: int, inputs: list[Path], caption: str | None) -> str:
        from ..editor.compose import build_audio, overlay
        from ..editor.subtitles import write_subtitles
        from ..editor.timeline import Timeline
        from ..qc.checks import footage_qc, summarize

        v = self.db.get_video(video_id)
        run = self.db.one("SELECT output_dir FROM runs WHERE id=?", (v["run_id"],))
        out_dir = Path(run["output_dir"])
        work = out_dir / "footage"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        pm = self.cfg.section("prompt_mode")
        w, h = int(self.cfg.get("video.width", 1080)), int(self.cfg.get("video.height", 1920))
        fps = int(self.cfg.get("video.fps", 30))
        max_mb = float(pm.get("max_input_mb", 1000))
        files = sorted((p for p in inputs if p.suffix.lower() in VIDEO_EXT), key=lambda p: p.name.lower())
        if not files:
            raise QCFailed("no video files found (mp4/mov/m4v/webm/mkv)")
        for f in files:
            if f.stat().st_size / 1e6 > max_mb:
                raise QCFailed(f"{f.name} is larger than {max_mb:.0f} MB")
        mode = pm.get("post_process", "none")
        branding = bool(pm.get("add_branding", False))
        script = json.loads(v["script_json"])
        sheet = FactSheet.from_dict(json.loads(v["facts_json"]))
        final = out_dir / "final.mp4"
        notes: list[str] = []
        cues = None

        first = probe(files[0])
        untouched = len(files) == 1 and mode == "none" and not branding and not platform_ready(first)
        if untouched:
            # exactly your file: only the container is rewritten (stream copy, no quality loss)
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(files[0]),
                                "-map", "0", "-c", "copy", "-movflags", "+faststart", str(final)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise QCFailed(f"could not read {files[0].name}: {r.stderr[-300:]}")
            has_sound = first["has_audio"] and not is_silent(final)
            notes.append("uploaded exactly as you made it")
        else:
            parts, infos = [], []
            for i, f in enumerate(files):
                part = work / f"part_{i:02d}.mp4"
                infos.append(normalize_clip(f, part, w, h, fps))
                parts.append(part)
            joined = concat(parts, work / "joined.mp4")
            vid_dur = sum(i["duration"] for i in infos) or probe(joined)["duration"]
            has_sound = any(i["has_audio"] for i in infos) and not is_silent(joined)
            if len(files) > 1:
                notes.append(f"joined {len(files)} clips")
            if any(i.get("reframed") for i in infos):
                notes.append("reframed to 9:16 with a blurred background")
            elif len(files) == 1 and platform_ready(first):
                notes.append("re-encoded for the platforms (" + ", ".join(platform_ready(first)) + ")")
            tl_path, voice_path = out_dir / "timeline.json", out_dir / "voice.json"
            tl = Timeline.from_dict(json.loads(tl_path.read_text())) if tl_path.exists() else None
            voice = json.loads(voice_path.read_text()) if voice_path.exists() else None
            if mode == "auto":
                mode = "none" if has_sound else ("voice_and_subtitles" if voice else "none")
            if mode in ("voice_and_subtitles", "subtitles_only") and tl is None:
                mode = "none"
            if mode == "voice_and_subtitles" and not voice:
                mode = "subtitles_only" if has_sound else "none"
            if mode == "none":
                overlay(joined, vid_dur, self.cfg, final, branding=branding)
            else:
                target = tl.total
                stretch, loop, total = 1.0, False, target
                if vid_dur >= target:
                    total = vid_dur if vid_dur <= target + 4 else target
                elif vid_dur >= 0.8 * target:
                    stretch = target / vid_dur
                    notes.append(f"slowed video to {1 / stretch:.0%} speed to fit the voiceover")
                else:
                    loop = True
                    notes.append(f"video ({vid_dur:.0f}s) much shorter than the voiceover ({target:.0f}s), so it was looped")
                tl_fit = replace(tl, total=round(total, 3))
                ass, _srt, cues = write_subtitles(tl_fit, self.cfg, work)
                audio = None
                if mode == "voice_and_subtitles":
                    audio = build_audio(tl_fit, voice, script["scenes"], self.cfg, work, int(video_id) * 7919,
                                        sfx_enabled=False)
                    notes.append("added Hinglish voiceover + music + subtitles")
                    has_sound = True
                else:
                    notes.append("added subtitles over your audio")
                overlay(joined, total, self.cfg, final, audio=audio, ass=ass, stretch=stretch, loop=loop,
                        branding=branding)
        if not has_sound and not pm.get("allow_silent_video", False):
            raise QCFailed("your video has no sound. Did the voiceover get exported? "
                           "(set prompt_mode.allow_silent_video: true to publish silent videos)")

        report = footage_qc(final, self.cfg, cues, sheet, script, exact_size=not untouched)
        report["post_process"] = mode
        report["notes"] = notes
        (out_dir / "qc_report.json").write_text(json.dumps(report, indent=2))
        if not report["passed"]:
            raise QCFailed(summarize(report), report)
        from ..pipeline import sha256
        sha = sha256(final)
        dup = self.db.one("SELECT id FROM videos WHERE video_sha256=? AND id<>?", (sha, video_id))
        if dup:
            raise QCFailed(f"this exact video was already used for #{dup['id']}")
        fields = dict(video_path=str(final), video_sha256=sha, duration=report["duration"],
                      qc_json=json.dumps(report), footage_path=";".join(str(f) for f in files))
        if caption:
            fields["user_caption"] = caption
        self.db.update_video(video_id, **fields)
        summary = f"✅ Video #{video_id} received ({report['duration']:.0f}s): " + "; ".join(notes)
        return summary + "\n" + self.after_video(video_id)

    # -------------------------------------------------------------- caption
    def after_video(self, video_id: int) -> str:
        """Video is stored; publish now if a caption is settled, otherwise ask for it."""
        v = self.db.get_video(video_id)
        source = self.cfg.get("prompt_mode.caption_source", "yours_only")
        if v["user_caption"]:
            return self.apply_caption(video_id, v["user_caption"], from_status="processing_footage")
        if source in ("generated", "yours_or_generated"):
            return self.use_suggested(video_id, from_status="processing_footage")
        self.db.transition_video(video_id, ("processing_footage",), "awaiting_caption")
        self.db.update_video(video_id, created_at=utcnow(), reminded_at=None)
        if self.p.telegram:
            from ..approval.telegram import Telegram
            self.p.telegram.send_message(
                f"✍️ Now send the caption for #{video_id} as a text message. The first line becomes the "
                "YouTube title; the whole text is the YouTube description and Instagram caption.",
                reply_markup=Telegram.caption_keyboard(video_id))
        return f"Waiting for your caption for #{video_id} (send it as a text message, or `dva caption {video_id} \"...\"`)."

    def apply_caption(self, video_id: int, caption: str, from_status: str = "awaiting_caption") -> str:
        from ..publish.metadata import metadata_from_caption
        v = self.db.get_video(video_id)
        if v["status"] not in (from_status, "awaiting_caption", "processing_footage"):
            return f"#{video_id} is '{v['status']}' — caption not changed."
        facts = json.loads(v["facts_json"])
        meta = metadata_from_caption(caption, json.loads(v["metadata_json"]), self.cfg, facts.get("sources"))
        self.db.update_video(video_id, user_caption=caption, metadata_json=json.dumps(meta, ensure_ascii=False))
        warn = "".join(f"\n⚠️ {w}" for w in meta.get("warnings", []))
        return self._release(video_id, v["status"]) + warn

    def use_suggested(self, video_id: int, from_status: str = "awaiting_caption") -> str:
        v = self.db.get_video(video_id)
        if v["status"] not in (from_status, "awaiting_caption", "processing_footage"):
            return f"#{video_id} is '{v['status']}'."
        meta = json.loads(v["metadata_json"])
        meta["caption_source"] = "suggested"
        self.db.update_video(video_id, metadata_json=json.dumps(meta, ensure_ascii=False))
        return self._release(video_id, v["status"])

    def _release(self, video_id: int, status: str) -> str:
        if self.cfg.get("prompt_mode.confirm_before_publish", False):
            self.db.transition_video(video_id, (status,), "awaiting_approval")
            self.db.update_video(video_id, created_at=utcnow(), reminded_at=None)
            self.p.send_for_approval(video_id)
            return "Check the preview and tap Publish."
        if not self.db.transition_video(video_id, (status,), "approved"):
            return f"#{video_id} changed state; not publishing."
        return self.p.publish_approved(video_id)
