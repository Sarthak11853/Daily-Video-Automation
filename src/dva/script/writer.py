"""Hinglish script generation + validation + fact audit."""
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

SYSTEM = """You are the head writer of "{channel}", a Hinglish facts channel for YouTube Shorts and Instagram Reels.
Host: {mascot}, a cute curious 3D cartoon robot. Tone: energetic, warm, witty, like a smart elder sibling
talking to friends — natural Hindi + English mix (the way young Indians actually talk). Never cringe, never
clickbait-lie. You may ONLY state facts given to you in the verified fact list; never add outside facts,
numbers, dates or names."""

PROMPT = """VERIFIED FACTS (the only facts you may use):
{claims}

Topic: {topic}
Storyline angle: {angle}
{learnings}
Write a ~{seconds}-second vertical video script, {min_scenes}-{max_scenes} scenes, {min_words}-{max_words} words total.
Structure:
1. Scene 1 = HOOK: max 12 words, spoken in under 3 seconds, a shocking question or claim that creates curiosity.
2. Middle scenes = storytelling: build curiosity, one idea per scene, surprising reveal(s), simple analogies.
3. Final scene = memorable ending line + soft CTA (e.g. "Follow karo aise aur facts ke liye!").

For every scene provide:
- "narration_display": Hinglish in ROMAN script (for subtitles), short punchy sentences.
- "narration_tts": the SAME sentence in DEVANAGARI script for a Hindi text-to-speech voice. Write English words
   phonetically in Devanagari (e.g. "साइंटिस्ट्स", "ब्लड"), write numbers as Hindi words.
- "claim_ids": ids of the verified facts this scene states (empty list only if it states no fact).
- "environment": one of {envs}
- "props": 1-3 items from {props}
- "mascot_action": one of {actions}
- "camera": one of {cameras}
- "on_screen_text": 0-3 punchy words in English/Roman letters, CAPS, e.g. "3 HEARTS?!" (or "")
- "sfx": one of {sfx}

Also return platform metadata:
- "title": short catchy title (max 60 chars, Roman Hinglish/English)
- "youtube_title": max 90 chars, curiosity-driven, no false claims
- "youtube_description": 2-3 lines Hinglish summary
- "instagram_caption": 2-4 lines Hinglish with emojis + a question to drive comments
- "hashtags": 8-12 relevant hashtags
- "hook_style": one of ["question","shock","challenge","mystery","myth_buster"]

Return ONLY JSON: {{"title":..., "hook_style":..., "scenes":[...], "youtube_title":..., "youtube_description":...,
"instagram_caption":..., "hashtags":[...]}}
{feedback}"""

AUDIT_SYSTEM = "You are a strict fact-checking editor."
AUDIT_PROMPT = """VERIFIED FACTS:
{claims}

SCRIPT (Hinglish):
{script}

List every factual assertion made in the script (numbers, names, dates, causes, comparisons, superlatives).
For each, say whether it is fully supported by one of the verified facts. Rhetorical questions, opinions
("kitna cool hai na?") and calls to action are not factual assertions.

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
        })
    tags = []
    for h in data.get("hashtags") or []:
        h = "#" + re.sub(r"[^\w]", "", str(h).lstrip("#"))
        if len(h) > 1 and h.lower() not in (t.lower() for t in tags):
            tags.append(h)
    return {
        "title": str(data.get("title") or "").strip()[:80],
        "hook_style": data.get("hook_style") or "question",
        "scenes": scenes,
        "youtube_title": str(data.get("youtube_title") or data.get("title") or "").strip(),
        "youtube_description": str(data.get("youtube_description") or "").strip(),
        "instagram_caption": str(data.get("instagram_caption") or "").strip(),
        "hashtags": tags[:15],
    }


def validate_script(script: dict, sheet: FactSheet, cfg) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    scenes = script.get("scenes", [])
    lo, hi = int(cfg.get("video.min_scenes", 6)), int(cfg.get("video.max_scenes", 10))
    if not lo <= len(scenes) <= hi:
        problems.append(f"need {lo}-{hi} scenes, got {len(scenes)}")
    words = sum(len(s["narration_display"].split()) for s in scenes)
    wmin, wmax = int(cfg.get("script.min_words", 120)), int(cfg.get("script.max_words", 175))
    if not wmin <= words <= wmax:
        problems.append(f"total words {words} outside {wmin}-{wmax}")
    if scenes and len(scenes[0]["narration_display"].split()) > 14:
        problems.append("scene 1 hook is too long (max ~12 words, must be < 3 seconds)")
    verified = {c.id: c for c in sheet.verified_claims}
    for s in scenes:
        if not s["narration_display"] or not s["narration_tts"]:
            problems.append(f"scene {s['id']}: missing narration")
            continue
        if not DEVANAGARI.search(s["narration_tts"]):
            problems.append(f"scene {s['id']}: narration_tts must be in Devanagari script")
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
    def __init__(self, cfg, llm: LLMChain):
        self.cfg = cfg
        self.llm = llm

    def _prompt(self, sheet: FactSheet, feedback: str, learnings: str) -> tuple[str, str]:
        c = self.cfg
        system = SYSTEM.format(channel=c.get("channel.name"), mascot=c.get("channel.mascot_name"))
        prompt = PROMPT.format(
            claims=_fmt_claims(sheet), topic=sheet.topic, angle=sheet.angle, learnings=learnings,
            seconds=c.get("video.target_duration", 60), min_scenes=c.get("video.min_scenes", 6),
            max_scenes=c.get("video.max_scenes", 10), min_words=c.get("script.min_words", 120),
            max_words=c.get("script.max_words", 175), envs=json.dumps(list(ENVIRONMENTS)),
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
            problems = validate_script(script, sheet, self.cfg)
            if not problems:
                problems = self.audit(script, sheet)
            if not problems:
                script["attempts"] = attempt
                return script
            log.warning("script attempt %d rejected: %s", attempt, "; ".join(problems)[:500])
            feedback = "\n".join(f"- {p}" for p in problems)
        raise ScriptInvalid(f"Script failed validation after {attempts} attempts: {feedback}")
