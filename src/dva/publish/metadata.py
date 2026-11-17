"""Platform-specific titles, descriptions, captions and hashtags."""
from __future__ import annotations

import re

from ..core.models import FactSheet

DISCLOSURE = "Animation, music and voice are generated with open-source tools; facts are checked against the sources above."
DISCLOSURE_PROMPT = "Visuals made with AI video tools; facts are checked against the sources above."


def _clean_tags(tags: list[str], extra: list[str]) -> list[str]:
    out: list[str] = []
    for t in [*tags, *extra]:
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 2 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def build_metadata(script: dict, sheet: FactSheet, cfg, mode: str | None = None) -> dict:
    mode = mode or cfg.get("workflow.mode", "full")
    disclosure = DISCLOSURE_PROMPT if mode == "prompt" else DISCLOSURE
    tags = _clean_tags(script.get("hashtags", []), cfg.get("channel.default_hashtags", []))
    src_lines = "\n".join(f"• {s.title} — {s.url}" for s in sheet.sources)
    src_domains = ", ".join(dict.fromkeys(s.domain for s in sheet.sources))

    yt_title = (script.get("youtube_title") or script.get("title") or sheet.topic).strip()
    yt_title = re.sub(r"\s+", " ", yt_title).replace("<", "").replace(">", "")
    if "#shorts" not in yt_title.lower() and len(yt_title) <= 91:
        yt_title += " #Shorts"
    yt_title = yt_title[:100]
    yt_desc = (f"{script.get('youtube_description', '').strip()}\n\n"
               f"📚 Sources:\n{src_lines}\n\n{disclosure}\n\n{' '.join(tags[:12])}")
    yt_desc = yt_desc.replace("<", "").replace(">", "")[:4900]
    yt_tags, total = [], 0
    for t in tags + [sheet.topic, sheet.category, "hindi facts", "hinglish"]:
        t = t.lstrip("#")
        if total + len(t) + 1 > 450:
            break
        yt_tags.append(t)
        total += len(t) + 1

    ig_caption = (f"{script.get('instagram_caption', '').strip()}\n\n"
                  f"📚 Sources: {src_domains}\n\n{' '.join(tags[:25])}")[:2150]
    return {
        "youtube": {"title": yt_title, "description": yt_desc, "tags": yt_tags,
                    "contains_synthetic_media": bool(cfg.get("prompt_mode.contains_synthetic_media", True))
                    if mode == "prompt" else bool(cfg.get("publish.youtube.contains_synthetic_media", False))},
        "instagram": {"caption": ig_caption},
        "title": script.get("title") or sheet.topic,
        "hashtags": tags,
    }


def metadata_from_caption(caption: str, fallback: dict, cfg, sources: list | None = None) -> dict:
    """Use YOUR caption as-is. The first line becomes the YouTube title (max 100 chars); the whole text is
    the YouTube description and the Instagram caption. Hashtags in your text become YouTube tags."""
    text = caption.strip().replace("\r\n", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    first = next((ln for ln in lines if re.sub(r"#\w+", "", ln).strip()), "")
    title = re.sub(r"\s+", " ", first).replace("<", "").replace(">", "").strip()
    if len(title) > 100:
        title = title[:99].rsplit(" ", 1)[0] + "…"
    if not title:
        title = fallback["youtube"]["title"]
    tags = re.findall(r"#(\w+)", text)
    yt_tags, total = [], 0
    for t in dict.fromkeys(tags):
        if total + len(t) + 1 > 450:
            break
        yt_tags.append(t)
        total += len(t) + 1
    desc = text
    if sources and cfg.get("prompt_mode.append_sources_to_caption", False):
        desc += "\n\n📚 Sources:\n" + "\n".join(f"• {s['title']} — {s['url']}" for s in sources)
    warnings = []
    if len(text) > 2200:
        warnings.append("caption longer than Instagram's 2,200 characters, so it was cut")
    if len(set(tags)) > 30:
        warnings.append("more than 30 hashtags: Instagram may reject the post")
    return {
        "youtube": {"title": title, "description": desc.replace("<", "").replace(">", "")[:4900], "tags": yt_tags,
                    "contains_synthetic_media": fallback["youtube"].get("contains_synthetic_media", False)},
        "instagram": {"caption": text[:2200]},
        "title": fallback.get("title"), "hashtags": ["#" + t for t in tags],
        "caption_source": "yours", "warnings": warnings,
    }
