"""Scene timing: derives clip lengths, xfade offsets, voice start times and
per-word subtitle timings from the measured TTS durations."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

TRANSITIONS = ["smoothleft", "circleopen", "slideup", "fadewhite", "radial", "smoothup", "zoomin", "wipeleft"]


@dataclass
class SceneTiming:
    id: int
    clip_len: float          # length of the rendered clip (incl. transition overlap)
    start: float             # clip start in the final video
    voice_start: float       # absolute time voice begins
    voice_dur: float
    transition: str = "fade"
    words: list[dict] = field(default_factory=list)   # [{w, start, end}]


@dataclass
class Timeline:
    scenes: list[SceneTiming]
    transition: float
    total: float

    def to_dict(self) -> dict:
        return {"transition": self.transition, "total": self.total, "scenes": [asdict(s) for s in self.scenes]}

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        return cls(scenes=[SceneTiming(**s) for s in d["scenes"]], transition=d["transition"], total=d["total"])


def word_timings(text: str, start: float, dur: float) -> list[dict]:
    """Distribute words across the voice duration, weighting by length and pauses."""
    words = text.split()
    if not words:
        return []
    weights = []
    for w in words:
        core = re.sub(r"[^\w]", "", w)
        wt = 0.55 + 0.11 * len(core)
        if re.search(r"[,;:]$", w):
            wt += 0.35
        elif re.search(r"[.!?…]$", w):
            wt += 0.6
        weights.append(wt)
    scale = dur / sum(weights)
    out, t = [], start
    for w, wt in zip(words, weights):
        d = wt * scale
        out.append({"w": w, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


def build_timeline(scenes: list[dict], voice: list[dict], transition: float = 0.35, lead: float = 0.12,
                   pad: float = 0.35, tail: float = 1.2) -> Timeline:
    n = len(scenes)
    out: list[SceneTiming] = []
    t = 0.0
    for i, (s, v) in enumerate(zip(scenes, voice)):
        enter = transition if i > 0 else 0.0
        exit_ = transition if i < n - 1 else tail
        clip_len = enter + lead + v["duration"] + pad + exit_
        start = t
        voice_start = start + enter + lead
        st = SceneTiming(id=s["id"], clip_len=round(clip_len, 3), start=round(start, 3),
                         voice_start=round(voice_start, 3), voice_dur=v["duration"],
                         transition=TRANSITIONS[i % len(TRANSITIONS)])
        st.words = word_timings(s["narration_display"], voice_start, v["duration"])
        out.append(st)
        t = start + clip_len - (transition if i < n - 1 else 0)
    return Timeline(scenes=out, transition=transition, total=round(t, 3))
