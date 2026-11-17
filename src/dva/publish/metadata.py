"""Platform-specific titles, descriptions, captions and hashtags."""
from __future__ import annotations

import re

from ..core.models import FactSheet

DISCLOSURE = "Animation, music and voice are generated with open-source tools; facts are checked against the sources above."


def _clean_tags(tags: list[str], extra: list[str]) -> list[str]:
    out: list[str] = []
    for t in [*tags, *extra]:
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 2 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def build_metadata(script: dict, sheet: FactSheet, cfg) -> dict:
    tags = _clean_tags(script.get("hashtags", []), cfg.get("channel.default_hashtags", []))
    src_lines = "\n".join(f"• {s.title} — {s.url}" for s in sheet.sources)
    src_domains = ", ".join(dict.fromkeys(s.domain for s in sheet.sources))

    yt_title = (script.get("youtube_title") or script.get("title") or sheet.topic).strip()
    yt_title = re.sub(r"\s+", " ", yt_title).replace("<", "").replace(">", "")
    if "#shorts" not in yt_title.lower() and len(yt_title) <= 91:
        yt_title += " #Shorts"
    yt_title = yt_title[:100]
    yt_desc = (f"{script.get('youtube_description', '').strip()}\n\n"
               f"📚 Sources:\n{src_lines}\n\n{DISCLOSURE}\n\n{' '.join(tags[:12])}")
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
        "youtube": {"title": yt_title, "description": yt_desc, "tags": yt_tags},
        "instagram": {"caption": ig_caption},
        "title": script.get("title") or sheet.topic,
        "hashtags": tags,
    }
