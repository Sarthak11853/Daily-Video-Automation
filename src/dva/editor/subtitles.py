"""Animated word-by-word subtitles (ASS) + SRT export."""
from __future__ import annotations

from pathlib import Path

from .timeline import Timeline


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _srt_ts(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def chunks(words: list[dict], size: int) -> list[list[dict]]:
    out, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= size or w["w"][-1:] in ".!?,":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def build_ass(tl: Timeline, cfg, width: int = 1080, height: int = 1920) -> tuple[str, list[dict]]:
    sub = cfg.section("subtitles")
    font = sub.get("font", "Poppins ExtraBold")
    size = int(sub.get("font_size", 92))
    hi = sub.get("highlight_color", "&H0021D4FF")
    prim = sub.get("primary_color", "&H00FFFFFF")
    outline = sub.get("outline_color", "&H00201010")
    margin_v = int(sub.get("margin_v", 430))
    per = int(sub.get("words_per_chunk", 3))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{font},{size},{prim},{hi},{outline},&H80000000,-1,0,0,0,100,100,1,0,1,7,3,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events, cues = [], []
    for sc in tl.scenes:
        groups = chunks(sc.words, per)
        for gi, g in enumerate(groups):
            g_end = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else g[-1]["end"] + 0.15
            cues.append({"start": g[0]["start"], "end": g_end, "text": " ".join(w["w"] for w in g)})
            for wi, w in enumerate(g):
                st = w["start"]
                en = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
                parts = []
                for wj, other in enumerate(g):
                    word = _esc(other["w"]).upper()
                    if wj == wi:
                        parts.append(f"{{\\c{hi}\\fscx112\\fscy112}}{word}{{\\c{prim}\\fscx100\\fscy100}}")
                    else:
                        parts.append(word)
                pop = "{\\fscx70\\fscy70\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
                events.append(f"Dialogue: 0,{_ts(st)},{_ts(en)},Word,,0,0,0,,{pop}{' '.join(parts)}")
    return header + "\n".join(events) + "\n", cues


def build_srt(cues: list[dict]) -> str:
    return "\n".join(f"{i}\n{_srt_ts(c['start'])} --> {_srt_ts(c['end'])}\n{c['text']}\n"
                     for i, c in enumerate(cues, 1))


def write_subtitles(tl: Timeline, cfg, out_dir: Path) -> tuple[Path, Path, list[dict]]:
    w, h = int(cfg.get("video.width", 1080)), int(cfg.get("video.height", 1920))
    ass, cues = build_ass(tl, cfg, w, h)
    ass_p, srt_p = out_dir / "subtitles.ass", out_dir / "subtitles.srt"
    ass_p.write_text(ass, encoding="utf-8")
    srt_p.write_text(build_srt(cues), encoding="utf-8")
    return ass_p, srt_p, cues
