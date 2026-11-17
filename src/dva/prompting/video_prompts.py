"""Turn a verified Hinglish script into copy-paste prompts for any AI video
generator (Veo, Sora, Kling, Hailuo, Runway, Pika, Meta AI, …).

Every shot prompt is self-contained (style + recurring character are repeated)
so clips generated separately still look like one consistent video. Scene
lengths come from the real voiceover, and long scenes are split into clips
that fit typical 5-10 s generator limits."""
from __future__ import annotations

import json
import logging
import math

from ..core.models import FactSheet
from ..llm.base import LLMChain

log = logging.getLogger(__name__)

SYSTEM = ("You are a cinematographer and prompt engineer for AI text-to-video models. You write vivid, concrete, "
          "visual prompts (subject, setting, action, camera, lighting, mood) that produce consistent, family-friendly "
          "3D cartoon shots. You never ask the model to render text or logos, never depict real celebrities, and keep "
          "visuals faithful to the facts provided.")

PROMPT = """Channel style (must be identical in every shot):
{style}

Recurring host character (appears in most shots, always described identically):
{mascot}

Topic: {topic}
Verified facts (visuals must not contradict these):
{facts}

The narration is already recorded. For each scene, write the visual prompt for a {seconds_hint}-second vertical
9:16 clip that illustrates the narration line. Scenes:
{scenes}

Return JSON:
{{
 "scenes": [
   {{"id": 1,
     "shot": "60-90 word visual prompt: subject + setting + action + camera move + lighting + mood. Describe the
              host character's action if present. No text/letters in the image.",
     "camera": "one short phrase, e.g. 'slow dolly in', 'orbit left', 'crane down'",
     "transition_hint": "how this shot should end to cut smoothly into the next"}}
 ],
 "thumbnail_prompt": "a single striking still image prompt for the cover frame",
 "music_mood": "3-6 words describing background music"
}}
Return exactly {n} scenes with ids {ids}."""


def _fmt_facts(sheet: FactSheet) -> str:
    return "\n".join(f"- {c.text}" for c in sheet.verified_claims)


def _clip_splits(duration: float, max_clip: float) -> list[float]:
    n = max(1, math.ceil(duration / max_clip))
    return [round(duration / n, 2)] * n


class VideoPromptWriter:
    def __init__(self, cfg, llm: LLMChain | None):
        self.cfg = cfg
        self.llm = llm
        pm = cfg.section("prompt_mode")
        self.style = " ".join(str(pm.get("style_bible", "")).split())
        self.mascot = " ".join(str(pm.get("mascot_description", "")).split()) if pm.get("include_mascot", True) else ""
        self.negative = " ".join(str(pm.get("negative_prompt", "")).split())
        self.max_clip = float(pm.get("target_clip_seconds", 8))

    def _llm_shots(self, script: dict, sheet: FactSheet, durations: dict[int, float]) -> dict:
        scenes = "\n".join(
            f"{s['id']}. ({durations.get(s['id'], 6):.1f}s) narration: \"{s['narration_display']}\" | "
            f"idea: {s.get('visual_description') or ', '.join(s.get('props', []))} | host: {s.get('mascot_action')}"
            for s in script["scenes"])
        ids = [s["id"] for s in script["scenes"]]
        res = self.llm.json(PROMPT.format(style=self.style, mascot=self.mascot or "(no host character)",
                                          topic=sheet.topic, facts=_fmt_facts(sheet), scenes=scenes,
                                          seconds_hint=f"{self.max_clip:.0f}", n=len(ids), ids=ids),
                            system=SYSTEM, temperature=0.7)
        got = {int(s.get("id", -1)): s for s in res.get("scenes", []) if isinstance(s, dict) and s.get("shot")}
        if set(got) != set(ids):
            raise ValueError(f"prompt writer returned scenes {sorted(got)} for {ids}")
        return {"scenes": got, "thumbnail_prompt": res.get("thumbnail_prompt", ""),
                "music_mood": res.get("music_mood", "")}

    def _template_shot(self, s: dict) -> dict:
        """Deterministic fallback when no LLM is available."""
        from ..render.vocab import ENVIRONMENTS, PROPS
        props = [PROPS.get(p, p) for p in s.get("props", [])] or ["a curious glowing object"]
        env = ENVIRONMENTS.get(s.get("environment", ""), "a colorful stylised setting")
        idea = s.get("visual_description") or (
            f"Setting: {env}. Hero subject: {' and '.join(f'a {p}' for p in props)}, large and centered, "
            "animated with lively, bouncy motion")
        host = {"surprised": "jumps back in surprise with wide eyes", "wave": "waves hello",
                "point": "points excitedly at the subject", "think": "tilts its head, thinking",
                "celebrate": "jumps and spins happily", "explain": "gestures while explaining",
                "scared": "shivers nervously"}.get(s.get("mascot_action", ""), "watches with curiosity")
        cam = s.get("camera", "push_in").replace("_", " ")
        shot = f"{idea}."
        if self.mascot and s.get("mascot_action") != "none":
            shot += f" In the foreground, the host robot {host}."
        return {"shot": shot, "camera": f"smooth {cam}", "transition_hint": "end on a clean, centered composition"}

    def write(self, script: dict, sheet: FactSheet, durations: dict[int, float]) -> dict:
        source = "llm"
        try:
            if not self.llm or not self.llm.available_providers():
                raise RuntimeError("no LLM available")
            data = self._llm_shots(script, sheet, durations)
        except Exception as exc:  # noqa: BLE001 — prompts must always be delivered
            log.warning("video prompt LLM failed (%s); using template prompts", exc)
            source = "template"
            data = {"scenes": {s["id"]: self._template_shot(s) for s in script["scenes"]},
                    "thumbnail_prompt": "", "music_mood": "upbeat curious playful"}
        prefix = self.style + (f" Recurring character: {self.mascot}." if self.mascot else "")
        scenes, clip_no = [], 0
        for s in script["scenes"]:
            shot = data["scenes"][s["id"]]
            dur = float(durations.get(s["id"], 6.0))
            clips = []
            parts = _clip_splits(dur, self.max_clip)
            for k, d in enumerate(parts, 1):
                clip_no += 1
                cont = "" if len(parts) == 1 else (f" (Part {k}/{len(parts)} of this shot; continue the same action"
                                                   " and composition seamlessly.)")
                full = (f"{prefix} SHOT: {shot['shot']} Camera: {shot.get('camera', 'smooth motion')}."
                        f"{cont} Duration {d:.1f} seconds, vertical 9:16, no text on screen.")
                clips.append({"clip": clip_no, "seconds": d, "prompt": full})
            scenes.append({"id": s["id"], "seconds": round(dur, 2), "voiceover": s["narration_display"],
                           "voiceover_devanagari": s.get("narration_tts", ""), "on_screen_text": s.get("on_screen_text", ""),
                           "shot": shot["shot"], "camera": shot.get("camera", ""),
                           "transition_hint": shot.get("transition_hint", ""), "clips": clips})
        thumb = data.get("thumbnail_prompt") or (f"{prefix} Eye-catching cover image about {sheet.topic}: "
                                                  f"{script['scenes'][0].get('visual_description', '')}")
        return {
            "source": source, "style_bible": self.style, "character": self.mascot, "negative_prompt": self.negative,
            "total_seconds": round(sum(durations.get(s["id"], 6.0) for s in script["scenes"]), 1),
            "music_mood": data.get("music_mood", ""), "thumbnail_prompt": thumb, "scenes": scenes,
            "single_prompt": self.single_prompt(scenes, sheet),
        }

    def single_prompt(self, scenes: list[dict], sheet: FactSheet) -> str:
        """One long prompt for tools that generate a whole ~60 s video in one go."""
        lines = [f"{self.style}", f"Recurring character: {self.mascot}." if self.mascot else "",
                 f"A ~{sum(s['seconds'] for s in scenes):.0f}-second vertical 9:16 animated explainer about "
                 f"{sheet.topic}, in {len(scenes)} shots:"]
        for s in scenes:
            lines.append(f"Shot {s['id']} ({s['seconds']:.0f}s): {s['shot']} Camera: {s['camera']}.")
        lines.append("No text or logos on screen.")
        return " ".join(x for x in lines if x)


def prompts_markdown(pkg: dict, script: dict, sheet: FactSheet, meta: dict, video_id: int,
                     has_voice: bool = False) -> str:
    out = [f"# Video #{video_id}: {script.get('title') or sheet.topic}", "",
           f"**Topic:** {sheet.topic} · **Category:** {sheet.category} · **Length:** ~{pkg['total_seconds']:.0f}s "
           f"· **Scenes:** {len(pkg['scenes'])}", "", f"**Angle:** {sheet.angle}", "",
           "## How to deliver", "",
           "1. Generate each clip below (or use the single prompt if your tool makes long videos).",
           "2. Edit the clips together and add your voiceover and subtitles (the suggested script and the "
           "verified facts are at the end of this file)." + (" A ready TTS voiceover is in `voiceover.wav`."
                                                             if has_voice else ""),
           "3. Export a vertical 9:16 MP4 (H.264 + AAC, 1080x1920 ideally).",
           "4. Send it to the Telegram bot as a file (up to 20 MB) **with your caption**, or drop it in the "
           "inbox folder with the caption in a .txt file of the same name. It is published exactly as you made it.",
           "", "## Style (already included in every prompt)", "", pkg["style_bible"], "",
           f"**Character:** {pkg['character'] or '—'}", "", f"**Negative prompt:** {pkg['negative_prompt']}", "",
           f"**Music mood:** {pkg.get('music_mood', '')}", "", "## Clips", ""]
    for s in pkg["scenes"]:
        out += [f"### Scene {s['id']} · {s['seconds']:.1f}s · on-screen text: {s['on_screen_text'] or '—'}",
                f"> 🗣 {s['voiceover']}", ""]
        for c in s["clips"]:
            out += [f"**Clip {c['clip']} ({c['seconds']:.1f}s)**", "```", c["prompt"], "```", ""]
    out += ["## Single prompt (long-video tools)", "```", pkg["single_prompt"], "```", "",
            "## Thumbnail prompt", "```", pkg["thumbnail_prompt"], "```", "",
            "## Suggested Hinglish script (optional)", ""] + [f"{s['id']}. ({s['seconds']:.0f}s) {s['voiceover']}"
                                                              for s in pkg["scenes"]]
    out += ["", "## Verified facts", ""] + [f"- {c.text}" for c in sheet.verified_claims]
    out += ["", "## Sources", ""] + [f"- {s.title}: {s.url}" for s in sheet.sources]
    out += ["", "## Suggested captions (used only if you choose them)", "",
            f"**YouTube title:** {meta['youtube']['title']}", "",
            "```", meta["youtube"]["description"], "```", "", "**Instagram:**", "```", meta["instagram"]["caption"], "```"]
    return "\n".join(out) + "\n"


def save_package(out_dir, pkg: dict, script: dict, sheet: FactSheet, meta: dict, video_id: int):
    (out_dir / "prompts.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out_dir / "PROMPTS.md"
    md.write_text(prompts_markdown(pkg, script, sheet, meta, video_id, (out_dir / "voiceover.wav").exists()),
                  encoding="utf-8")
    return md
