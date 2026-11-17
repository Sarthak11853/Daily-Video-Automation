"""Script generation (easy English by default, Hinglish optional) + validation + fact audit,
including the automatic YouTube/Instagram title, caption, hashtags and tags."""
from __future__ import annotations

import json
import logging
import re

from ..core.errors import ScriptInvalid
from ..core.models import FactSheet
from ..core.text import is_latin_display, numbers
from ..llm.base import LLMChain
from ..render.vocab import CAMERA_MOVES, ENVIRONMENTS, MASCOT_ACTIONS, PROPS, SFX, closest

log = logging.getLogger(__name__)

LANG = {
    "english": {
        "voice": ("easy, simple English that a 12-year-old or a non-native speaker understands instantly: short "
                  "sentences (max ~12 words), everyday words, no idioms, slang or jargon; if a science word is "
                  "needed, explain it in a few simple words. Energetic, warm and curious, like a friendly teacher."),
        "cta": "Follow for more amazing facts!",
        "display": "- \"narration_display\": the spoken line in easy English (also used for subtitles).",
        "tts": "- \"narration_tts\": exactly the same text as narration_display.",
        "meta_lang": "simple English",
    },
    "hinglish": {
        "voice": ("natural Hinglish (Hindi + English mix, the way young Indians actually talk), energetic, warm "
                  "and witty, like a smart elder sibling talking to friends."),
        "cta": "Follow karo aise aur facts ke liye!",
        "display": "- \"narration_display\": Hinglish in ROMAN script (for subtitles), short punchy sentences.",
        "tts": ("- \"narration_tts\": the SAME sentence in DEVANAGARI script for a Hindi text-to-speech voice. "
                "Write English words phonetically in Devanagari (e.g. \"साइंटिस्ट्स\"), numbers as Hindi words."),
        "meta_lang": "Hinglish",
    },
}

SYSTEM = """You are the head writer of "{channel}", a facts channel for YouTube Shorts and Instagram Reels.
Recurring host: {mascot}, a cute curious 3D cartoon robot. Language and tone: {voice}
Never cringe, never clickbait-lie. You may ONLY state facts given to you in the verified fact list; never add
outside facts, numbers, dates or names. You are also an expert in Shorts/Reels SEO and discoverability."""

PROMPT = """VERIFIED FACTS (the only facts you may use):
{claims}

Topic: {topic}
Storyline angle: {angle}
{learnings}
Write a ~{seconds}-second vertical video script, {min_scenes}-{max_scenes} scenes, {min_words}-{max_words} words total.
Structure:
1. Scene 1 = HOOK: max 12 words, spoken in under 3 seconds, a surprising question or claim that creates curiosity.
2. Middle scenes = storytelling: build curiosity, one idea per scene, surprising reveal(s), simple comparisons.
3. Final scene = memorable ending line + soft call to action (e.g. "{cta}").

For every scene provide:
{display}
{tts}
- "visual_description": 1-2 English sentences describing what the viewer SEES in this shot (subject, setting,
   action, mood) — vivid and cinematic, faithful to the facts, no on-screen text.
- "claim_ids": ids of the verified facts this scene states (empty list only if it states no fact).
- "environment": one of {envs}
- "props": 1-3 items from {props}
- "mascot_action": one of {actions}
- "camera": one of {cameras}
- "on_screen_text": 0-3 punchy English words in CAPS, e.g. "3 HEARTS?!" (or "")
- "sfx": one of {sfx}

Also write the publishing metadata (all in {meta_lang}; it is posted automatically, so make it excellent):
- "title": short catchy title (max 60 chars)
- "youtube_title": max 90 chars, curiosity-driven but true, main keyword near the start, max 1 emoji
- "youtube_description": 2-3 short lines: what the viewer learns + a question to drive comments
- "instagram_caption": 3-5 short lines: a hook line, 1-2 lines of value, a question to drive comments, a
   follow/save call to action; tasteful emojis allowed; no hashtags inside (they are added separately)
- "hashtags": 10-14 hashtags: 3-4 broad (e.g. #facts #didyouknow), 4-6 topic-specific, 2-3 niche; no spaces,
   no banned or misleading tags, no #fyp spam
- "youtube_tags": 10-15 search phrases people would type to find this video (no "#")
- "hook_style": one of ["question","shock","challenge","mystery","myth_buster"]

Return ONLY JSON: {{"title":..., "hook_style":..., "scenes":[...], "youtube_title":..., "youtube_description":...,
"instagram_caption":..., "hashtags":[...], "youtube_tags":[...]}}
{feedback}"""

AUDIT_SYSTEM = "You are a strict fact-checking editor."
AUDIT_PROMPT = """VERIFIED FACTS:
{claims}

SCRIPT:
{script}

List every factual assertion made in the script (numbers, names, dates, causes, comparisons, superlatives).
For each, say whether it is fully supported by one of the verified facts. Rhetorical questions, opinions
("isn't that cool?") and calls to action are not factual assertions.

Return JSON: {{"assertions": [{{"scene": 1, "text": "...", "supported": true/false, "claim_id": "C1" or null}}]}}"""

DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def _fmt_claims(sheet: FactSheet) -> str:
    return "\n".join(f"{c.id}: {c.text}" for c in sheet.verified_claims)


def normalize_script(data: dict, cfg) -> dict:
    """Coerce LLM output onto the renderer vocabulary and clean fields."""
    scenes = []
    for i, s in enumerate(data.get("scenes") or [], 1):
        if not isinstance(s, dict):
            continue
        props = [closest(p, PROPS, "question") for p in (s.get("props") or [])][:3] or ["question"]
        ost = str(s.get("on_screen_text") or "").strip().upper()[:24]
        if not is_latin_display(ost):
            ost = ""
        scenes.append({
            "id": i,
            "narration_display": " ".join(str(s.get("narration_display", "")).split()),
            "narration_tts": " ".join(str(s.get("narration_tts", "")).split()),
            "claim_ids": [str(c) for c in (s.get("claim_ids") or [])],
            "environment": closest(s.get("environment"), ENVIRONMENTS, "studio"),
            "props": list(dict.fromkeys(props)),
            "mascot_action": closest(s.get("mascot_action"), MASCOT_ACTIONS, "explain"),
            "camera": closest(s.get("camera"), CAMERA_MOVES, "push_in"),
            "on_screen_text": ost,
            "sfx": s.get("sfx") if s.get("sfx") in SFX else "whoosh",
            "visual_description": " ".join(str(s.get("visual_description", "")).split())[:600],
        })
    tags = []
    for h in data.get("hashtags") or []:
        h = "#" + re.sub(r"[^\w]", "", str(h).lstrip("#"))
        if len(h) > 1 and h.lower() not in (t.lower() for t in tags):
            tags.append(h)
    ytags = []
    for t in data.get("youtube_tags") or []:
        t = " ".join(str(t).replace("#", "").split())[:60]
        if t and t.lower() not in (x.lower() for x in ytags):
            ytags.append(t)
    return {
        "youtube_tags": ytags[:20],
        "title": str(data.get("title") or "").strip()[:80],
        "hook_style": data.get("hook_style") or "question",
        "scenes": scenes,
        "youtube_title": str(data.get("youtube_title") or data.get("title") or "").strip(),
        "youtube_description": str(data.get("youtube_description") or "").strip(),
        "instagram_caption": str(data.get("instagram_caption") or "").strip(),
        "hashtags": tags[:15],
    }


def validate_script(script: dict, sheet: FactSheet, cfg, word_range: tuple[int, int] | None = None) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    scenes = script.get("scenes", [])
    lo, hi = int(cfg.get("video.min_scenes", 6)), int(cfg.get("video.max_scenes", 10))
    if not lo <= len(scenes) <= hi:
        problems.append(f"need {lo}-{hi} scenes, got {len(scenes)}")
    words = sum(len(s["narration_display"].split()) for s in scenes)
    wmin, wmax = word_range or (int(cfg.get("script.min_words", 120)), int(cfg.get("script.max_words", 175)))
    if not wmin <= words <= wmax:
        problems.append(f"total words {words} outside {wmin}-{wmax}")
    if scenes and len(scenes[0]["narration_display"].split()) > 14:
        problems.append("scene 1 hook is too long (max ~12 words, must be < 3 seconds)")
    verified = {c.id: c for c in sheet.verified_claims}
    lang = cfg.get("channel.language", "english")
    for s in scenes:
        if not s["narration_display"] or not s["narration_tts"]:
            problems.append(f"scene {s['id']}: missing narration")
            continue
        if lang == "hinglish":
            if not DEVANAGARI.search(s["narration_tts"]):
                problems.append(f"scene {s['id']}: narration_tts must be in Devanagari script")
        else:
            if DEVANAGARI.search(s["narration_display"] + s["narration_tts"]):
                problems.append(f"scene {s['id']}: use English only (no Hindi script)")
            long = [x for x in re.split(r"(?<=[.!?])\s+", s["narration_display"]) if len(x.split()) > 18]
            if long:
                problems.append(f"scene {s['id']}: sentence too long for easy English: '{long[0][:60]}…' "
                                "(keep sentences under ~12 words)")
        bad = [c for c in s["claim_ids"] if c not in verified]
        if bad:
            problems.append(f"scene {s['id']}: unknown/unverified claim ids {bad}")
        allowed = set().union(*(numbers(verified[c].text) | set().union(*(numbers(e["quote"]) for e in verified[c].evidence))
                                for c in s["claim_ids"] if c in verified)) if s["claim_ids"] else set()
        extra = numbers(s["narration_display"]) - allowed
        if extra:
            problems.append(f"scene {s['id']}: numbers {sorted(extra)} are not in its cited verified facts")
    if not script.get("youtube_title"):
        problems.append("missing youtube_title")
    return problems


class ScriptWriter:
    def __init__(self, cfg, llm: LLMChain, wps: float | None = None):
        self.cfg = cfg
        self.llm = llm
        self.wps = wps  # measured TTS words/second (None = use configured word range)

    def word_range(self, n_scenes: int = 8) -> tuple[int, int]:
        lo, hi = int(self.cfg.get("script.min_words", 120)), int(self.cfg.get("script.max_words", 175))
        if not self.wps:
            return lo, hi
        # spoken seconds available = target minus per-scene gaps/transitions and the outro tail
        overhead = n_scenes * 0.82 + 1.2
        target = float(self.cfg.get("video.target_duration", 60))
        mid = (target - overhead) * self.wps
        return max(40, int(mid * 0.9)), int(mid * 1.08)

    def _prompt(self, sheet: FactSheet, feedback: str, learnings: str) -> tuple[str, str]:
        c = self.cfg
        L = LANG.get(c.get("channel.language", "english"), LANG["english"])
        system = SYSTEM.format(channel=c.get("channel.name"), mascot=c.get("channel.mascot_name"), voice=L["voice"])
        prompt = PROMPT.format(cta=L["cta"], display=L["display"], tts=L["tts"], meta_lang=L["meta_lang"],
            claims=_fmt_claims(sheet), topic=sheet.topic, angle=sheet.angle, learnings=learnings,
            seconds=c.get("video.target_duration", 60), min_scenes=c.get("video.min_scenes", 6),
            max_scenes=c.get("video.max_scenes", 10), min_words=self.word_range()[0],
            max_words=self.word_range()[1], envs=json.dumps(list(ENVIRONMENTS)),
            props=json.dumps(list(PROPS)), actions=json.dumps(list(MASCOT_ACTIONS)),
            cameras=json.dumps(list(CAMERA_MOVES)), sfx=json.dumps(SFX),
            feedback=f"\nYOUR PREVIOUS DRAFT HAD THESE PROBLEMS — FIX THEM:\n{feedback}" if feedback else "")
        return system, prompt

    def audit(self, script: dict, sheet: FactSheet) -> list[str]:
        text = "\n".join(f"Scene {s['id']}: {s['narration_display']}" for s in script["scenes"])
        res = self.llm.json(AUDIT_PROMPT.format(claims=_fmt_claims(sheet), script=text), system=AUDIT_SYSTEM,
                            judge=True, temperature=0.0)
        return [f"scene {a.get('scene')}: unsupported assertion '{a.get('text')}'"
                for a in res.get("assertions", []) if isinstance(a, dict) and a.get("supported") is False]

    def write(self, sheet: FactSheet, learnings: str = "") -> dict:
        feedback = ""
        attempts = int(self.cfg.get("script.max_attempts", 3))
        for attempt in range(1, attempts + 1):
            system, prompt = self._prompt(sheet, feedback, learnings)
            raw = self.llm.json(prompt, system=system, temperature=float(self.cfg.get("llm.temperature", 0.8)))
            script = normalize_script(raw if isinstance(raw, dict) else {}, self.cfg)
            problems = validate_script(script, sheet, self.cfg, self.word_range(len(script["scenes"]) or 8))
            if not problems:
                problems = self.audit(script, sheet)
            if not problems:
                script["attempts"] = attempt
                return script
            log.warning("script attempt %d rejected: %s", attempt, "; ".join(problems)[:500])
            feedback = "\n".join(f"- {p}" for p in problems)
        raise ScriptInvalid(f"Script failed validation after {attempts} attempts: {feedback}")
