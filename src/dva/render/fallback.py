"""Fallback renderer (no Blender needed): draws a colourful 2.5D still per
scene with Pillow — gradient world, glowing bokeh, the hero prop as a
badge, the mascot and the keyword — then animates it with a Ken-Burns move.
Lower quality than Blender, but keeps the pipeline producing a video."""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .runner import still_to_clip

ENV_COLORS = {
    "space": ((10, 5, 40), (40, 15, 90)), "ocean": ((0, 40, 90), (40, 180, 220)),
    "lab": ((150, 190, 255), (235, 245, 255)), "city": ((255, 140, 90), (110, 130, 240)),
    "ancient": ((255, 180, 90), (120, 180, 255)), "jungle": ((40, 140, 60), (110, 200, 255)),
    "arctic": ((200, 230, 255), (90, 140, 240)), "mind": ((40, 10, 80), (210, 100, 210)),
    "earth": ((140, 200, 255), (40, 90, 230)), "mystery": ((8, 8, 25), (50, 30, 100)),
    "desert": ((255, 190, 100), (130, 190, 255)), "studio": ((25, 50, 140), (130, 80, 240)),
}
PROP_GLYPH = {"question": "?", "exclamation": "!", "heart": "♥", "star": "★", "lightning": "⚡"}


def _font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for cand in [path, "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]:
        if cand and Path(cand).exists():
            return ImageFont.truetype(cand, size)
    return ImageFont.load_default()


def draw_scene(scene: dict, w: int, h: int, seed: int, style: dict, font_path: str | None = None) -> Image.Image:
    r = random.Random(seed)
    top, bottom = ENV_COLORS.get(scene.get("environment", "studio"), ENV_COLORS["studio"])
    img = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        px.line([(0, y), (w, y)], fill=tuple(int(bottom[i] * t + top[i] * (1 - t)) for i in range(3)))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for _ in range(40):
        x, y, rad = r.randint(0, w), r.randint(0, h), r.randint(6, 40)
        g.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 255, 255, r.randint(25, 90)))
    img.paste(glow.filter(ImageFilter.GaussianBlur(6)), (0, 0), glow.filter(ImageFilter.GaussianBlur(6)))
    d = ImageDraw.Draw(img)
    # ground ellipse
    d.ellipse([-w * 0.3, h * 0.72, w * 1.3, h * 1.25], fill=tuple(int(c * 0.7) for c in bottom))
    # hero badge(s)
    props = scene.get("props") or ["question"]
    cx, cy, R = w * 0.55, h * 0.40, w * 0.26
    acc = tuple(int(c * 255) for c in style.get("accent_color", (1, 0.78, 0.1)))
    d.ellipse([cx - R, cy - R, cx + R, cy + R], fill=acc, outline=(20, 20, 40), width=10)
    glyph = PROP_GLYPH.get(props[0], props[0][:1].upper())
    f = _font(int(R * 1.1), font_path)
    d.text((cx, cy), glyph, font=f, fill=(30, 20, 60), anchor="mm")
    d.text((cx, cy + R + 50), props[0].upper(), font=_font(56, font_path), fill=(255, 255, 255), anchor="mm",
           stroke_width=5, stroke_fill=(20, 20, 40))
    # mascot (simplified Gyaanu)
    if scene.get("mascot_action") != "none":
        body = tuple(int(c * 255) for c in style.get("mascot_color", (0.1, 0.78, 0.82)))
        mx, my = w * 0.2, h * 0.6
        d.ellipse([mx - 130, my - 160, mx + 130, my + 160], fill=body, outline=(10, 30, 60), width=8)
        d.rounded_rectangle([mx - 95, my - 110, mx + 95, my + 20], 50, fill=(15, 20, 60))
        for s in (-1, 1):
            d.ellipse([mx + s * 40 - 18, my - 75, mx + s * 40 + 18, my - 25], fill=(180, 255, 255))
        d.arc([mx - 30, my - 40, mx + 30, my + 0], 20, 160, fill=(180, 255, 255), width=6)
        d.line([mx, my - 160, mx, my - 210], fill=(140, 140, 150), width=8)
        d.ellipse([mx - 20, my - 235, mx + 20, my - 195], fill=acc)
    text = scene.get("on_screen_text") or ""
    if text:
        tc = tuple(int(c * 255) for c in style.get("text_color", (1, 0.93, 0.25)))
        size = 150
        while size > 60 and d.textlength(text, font=_font(size, font_path)) > w * 0.88:
            size -= 8
        d.text((w / 2, h * 0.14), text, font=_font(size, font_path), fill=tc, anchor="mm",
               stroke_width=10, stroke_fill=(20, 15, 40))
    return img


class FallbackRenderer:
    name = "fallback"

    def __init__(self, cfg):
        self.cfg = cfg

    def available(self) -> bool:
        return True

    def render_all(self, scenes, durations, work: Path, seed_base: str) -> list[Path]:
        work.mkdir(parents=True, exist_ok=True)
        w, h = int(self.cfg.get("video.width", 1080)), int(self.cfg.get("video.height", 1920))
        fps = int(self.cfg.get("video.fps", 30))
        font = self.cfg.path("render.blender.font_path", "")
        out = []
        for s, dur in zip(scenes, durations):
            seed = int(hashlib.sha1(f"{seed_base}:{s['id']}".encode()).hexdigest()[:8], 16)
            still = work / f"still_{s['id']:02d}.png"
            draw_scene(s, w, h, seed, self.cfg.section("render.style"),
                       str(font) if font.is_file() else None).save(still)
            clip = work / f"scene_{s['id']:02d}.mp4"
            still_to_clip(still, dur, fps, clip, w, h, s.get("camera", "push_in"))
            out.append(clip)
        return out
