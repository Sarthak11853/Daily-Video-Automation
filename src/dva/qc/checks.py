"""Automated quality checks before a video is sent for approval."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..core.models import FactSheet
from ..editor.timeline import Timeline


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _detect(path: Path, filt: str, kind: str) -> list[tuple[float, float]]:
    """Run blackdetect/silencedetect and return (start, duration) spans."""
    if kind == "video":
        args = ["-i", str(path), "-vf", filt, "-an", "-f", "null", "-"]
    else:
        args = ["-i", str(path), "-af", filt, "-vn", "-f", "null", "-"]
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True)
    spans = []
    if kind == "video":
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", r.stderr):
            spans.append((float(m.group(1)), float(m.group(3))))
    else:
        starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
        durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", r.stderr)]
        spans = list(zip(starts, durs))
    return spans


def integrity_errors(path: Path) -> str:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return r.stderr.strip()


def run_qc(video: Path, tl: Timeline, cues: list[dict], sheet: FactSheet, script: dict, cfg,
           voice_track: Path | None = None) -> dict:
    v = cfg.section("video")
    q = cfg.section("qc")
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "", severity: str = "error"):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": severity})

    if not video.exists():
        add("file_exists", False, str(video))
        return {"passed": False, "checks": checks}
    info = ffprobe(video)
    vs = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    au = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"].get("duration", 0))
    size_mb = int(info["format"].get("size", 0)) / 1e6
    add("video_stream", vs is not None)
    add("audio_stream", au is not None)
    if vs:
        add("resolution", (vs["width"], vs["height"]) == (v.get("width", 1080), v.get("height", 1920)),
            f"{vs['width']}x{vs['height']}")
        add("codec", vs.get("codec_name") == "h264" and vs.get("pix_fmt") == "yuv420p",
            f"{vs.get('codec_name')} {vs.get('pix_fmt')}")
        num, den = (vs.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den or 1)
        add("frame_rate", abs(fps - float(v.get("fps", 30))) < 0.6, f"{fps:.2f} fps")
    if au:
        add("audio_codec", au.get("codec_name") == "aac", f"{au.get('codec_name')} {au.get('sample_rate')} Hz")
    add("duration", float(v.get("min_duration", 45)) <= dur <= float(v.get("max_duration", 75)),
        f"{dur:.2f}s (target {v.get('target_duration', 60)}s)")
    add("file_size", size_mb <= float(v.get("max_file_mb", 49)), f"{size_mb:.1f} MB")
    err = integrity_errors(video)
    add("decode_integrity", not err, err[:300])
    blacks = [b for b in _detect(video, "blackdetect=d=0.2:pix_th=0.08", "video")
              if b[1] > float(q.get("max_black_seconds", 0.6))]
    add("no_black_frames", not blacks, f"{blacks[:3]}")
    silences = [s for s in _detect(video, "silencedetect=noise=-45dB:d=0.5", "audio")
                if s[1] > float(q.get("max_silence_seconds", 2.0))]
    add("no_long_silence", not silences, f"{silences[:3]}")

    # --- audio/subtitle sync ------------------------------------------
    drift_limit = float(q.get("max_sync_drift_seconds", 0.5))
    drifts = []
    for sc in tl.scenes:
        if sc.words:
            drifts.append(abs(sc.words[0]["start"] - sc.voice_start))
            drifts.append(abs(sc.words[-1]["end"] - (sc.voice_start + sc.voice_dur)))
    add("subtitle_voice_sync", max(drifts or [0]) <= drift_limit, f"max drift {max(drifts or [0]):.3f}s")
    if voice_track and voice_track.exists():
        # every scene's voice window must actually contain speech (catches TTS dropouts)
        quiet = _detect(voice_track, "silencedetect=noise=-40dB:d=0.4", "audio")
        bad = []
        for sc in tl.scenes:
            for s0, sd in quiet:
                if s0 <= sc.voice_start + 0.1 and s0 + sd >= sc.voice_start + sc.voice_dur - 0.1:
                    bad.append(sc.id)
        add("voice_present_each_scene", not bad, f"silent scenes: {bad}")
    overlaps = [i for i in range(1, len(cues)) if cues[i]["start"] < cues[i - 1]["end"] - 0.05]
    add("subtitles_no_overlap", not overlaps, f"{len(overlaps)} overlaps")
    add("subtitles_within_video", all(c["end"] <= dur + 0.3 for c in cues), "")
    add("subtitles_cover_narration",
        sum(len(c["text"].split()) for c in cues) == sum(len(s["narration_display"].split()) for s in script["scenes"]))

    # --- facts ---------------------------------------------------------
    verified = {c.id for c in sheet.verified_claims}
    used = {cid for s in script["scenes"] for cid in s.get("claim_ids", [])}
    add("facts_verified", used <= verified and bool(used),
        f"{len(used)} claims used, unverified: {sorted(used - verified)}")
    add("sources_listed", bool(sheet.sources), f"{len(sheet.sources)} sources")
    if sheet.demo:
        add("demo_content", True, "demo fact sheet (hand-curated)", severity="warning")
    passed = all(c["ok"] for c in checks if c["severity"] == "error")
    return {"passed": passed, "duration": round(dur, 2), "size_mb": round(size_mb, 1), "checks": checks}


def summarize(report: dict) -> str:
    bad = [c for c in report["checks"] if not c["ok"]]
    head = f"QC {'PASSED' if report['passed'] else 'FAILED'} — {report.get('duration')}s, {report.get('size_mb')} MB"
    return head + "".join(f"\n  ✗ {c['name']}: {c['detail']}" for c in bad)
