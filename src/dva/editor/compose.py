"""Audio mixing + final video assembly with FFmpeg."""
from __future__ import annotations

import logging
import random
import subprocess
from pathlib import Path

import numpy as np

from ..audio.synth import SR, music, read_wav, sfx, write_wav
from .timeline import Timeline

log = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font(cfg) -> str | None:
    fonts_dir = cfg.path("subtitles.fonts_dir", "assets/fonts")
    if fonts_dir.is_dir():
        for f in sorted(fonts_dir.glob("*.ttf")):
            return str(f)
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def ff(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-2000:]}")


def build_audio(tl: Timeline, voice: list[dict], scenes: list[dict], cfg, work: Path, seed: int) -> Path:
    """Voice timeline + ducked music + SFX -> loudness-normalised stereo WAV."""
    n = int((tl.total + 0.5) * SR)
    vo = np.zeros(n)
    fx = np.zeros(n)
    for st, v, sc in zip(tl.scenes, voice, scenes):
        a = read_wav(Path(v["wav"]))
        s = int(st.voice_start * SR)
        vo[s:s + len(a)] += a[: max(0, n - s)]
        if cfg.get("audio.sfx", True):
            kind = sc.get("sfx", "whoosh")
            if st.id == scenes[0]["id"] and kind == "whoosh":
                kind = "boom"
            clip = sfx(kind, seed + st.id)
            at = int(max(0.0, st.start - (0.1 if st.start > 0 else 0)) * SR)
            fx[at:at + len(clip)] += clip[: max(0, n - at)]
    vo_p, fx_p = work / "voice_track.wav", work / "sfx_track.wav"
    write_wav(vo_p, vo)
    write_wav(fx_p, fx)
    mus_p = work / "music_track.wav"
    lib = sorted(cfg.path("paths.assets_dir", "assets").joinpath("music").glob("*.[mw][pa][3v]"))
    if cfg.get("audio.music", "procedural") == "library" and lib:
        src = random.Random(seed).choice(lib)
        ff(["-stream_loop", "-1", "-i", str(src), "-t", f"{tl.total + 0.5:.2f}", "-ar", str(SR), "-ac", "2", str(mus_p)])
    else:
        write_wav(mus_p, music(tl.total + 0.5, seed))
    mvol = float(cfg.get("audio.music_volume_db", -20))
    lufs = float(cfg.get("audio.target_lufs", -14))
    out = work / "mix.wav"
    graph = (f"[2:a]volume={mvol}dB,afade=t=out:st={max(0, tl.total - 1.5):.2f}:d=1.5[m];"
             "[0:a]asplit=2[v][vk];"
             "[m][vk]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[md];"
             "[1:a]volume=-6dB[fx];"
             "[v][md][fx]amix=inputs=3:normalize=0:duration=first,"
             f"loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample={SR}[out]")
    ff(["-i", str(vo_p), "-i", str(fx_p), "-i", str(mus_p), "-filter_complex", graph, "-map", "[out]",
        "-ac", "2", "-ar", str(SR), "-t", f"{tl.total:.3f}", str(out)])
    return out


def _ff_escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def compose(clips: list[Path], tl: Timeline, audio: Path, ass: Path, cfg, out: Path) -> Path:
    w, h = int(cfg.get("video.width", 1080)), int(cfg.get("video.height", 1920))
    fps = int(cfg.get("video.fps", 30))
    T = tl.transition
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    parts = [f"[{i}:v]settb=AVTB,fps={fps},format=yuv420p,scale={w}:{h},setsar=1[s{i}]" for i in range(len(clips))]
    last = "s0"
    for i in range(1, len(clips)):
        st = tl.scenes[i]
        parts.append(f"[{last}][s{i}]xfade=transition={st.transition}:duration={T}:offset={st.start:.3f}[x{i}]")
        last = f"x{i}"
    fonts_dir = cfg.path("subtitles.fonts_dir", "assets/fonts")
    sub_filter = f"ass='{_ff_escape(str(ass))}'"
    if fonts_dir.is_dir():
        sub_filter += f":fontsdir='{_ff_escape(str(fonts_dir))}'"
    post = [sub_filter]
    if cfg.get("video.progress_bar", True):
        post.append(f"drawbox=x=0:y=ih-14:w='iw*t/{tl.total:.3f}':h=14:color=0xFFD21F@0.9:t=fill")
    font = find_font(cfg)
    if cfg.get("video.watermark", True) and font:
        handle = str(cfg.get("channel.handle", "")).replace(":", "\\:").replace("'", "")
        post.append(f"drawtext=fontfile='{_ff_escape(font)}':text='{handle}':x=40:y=60:fontsize=38:"
                    "fontcolor=white@0.75:borderw=3:bordercolor=black@0.5")
    parts.append(f"[{last}]" + ",".join(post) + "[vout]")
    graph = ";".join(parts)
    args = [*inputs, "-i", str(audio), "-filter_complex", graph, "-map", "[vout]", "-map", f"{len(clips)}:a",
            "-c:v", "libx264", "-preset", "slow", "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
            "-crf", str(cfg.get("video.crf", 20)), "-maxrate", str(cfg.get("video.maxrate", "5M")),
            "-bufsize", "10M", "-r", str(fps), "-g", str(fps * 2),
            "-c:a", "aac", "-b:a", str(cfg.get("video.audio_bitrate", "192k")), "-ar", str(SR),
            "-movflags", "+faststart", "-t", f"{tl.total:.3f}", str(out)]
    log.info("Composing final video (%d scenes, %.1fs)", len(clips), tl.total)
    ff(args)
    return out


def thumbnail(video: Path, at: float, out: Path) -> Path:
    ff(["-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)])
    return out
