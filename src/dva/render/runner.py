"""Render orchestration: turn script scenes + voice durations into one MP4
clip per scene, using Blender (preferred) or the Pillow fallback renderer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.errors import ProviderUnavailable, RenderFailed

log = logging.getLogger(__name__)
BUILDER = Path(__file__).parent / "blender" / "scene_builder.py"
MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


def find_blender(setting: str = "auto") -> list[str] | None:
    """Return the command prefix that runs scene_builder.py, or None."""
    if setting and setting != "auto":
        if setting == "bpy":
            return [sys.executable]
        return [setting, "-b", "--factory-startup", "--python"] if Path(setting).exists() or shutil.which(setting) else None
    for cand in (MAC_BLENDER, shutil.which("blender")):
        if cand and Path(cand).exists():
            return [cand, "-b", "--factory-startup", "--python"]
    try:
        r = subprocess.run([sys.executable, "-c", "import bpy"], capture_output=True, timeout=120)
        if r.returncode == 0:
            return [sys.executable]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def encode_frames(frames_dir: Path, render_fps: int, out_fps: int, out: Path, w: int, h: int) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(render_fps),
                    "-i", str(frames_dir / "frame_%04d.png"),
                    "-vf", f"scale={w}:{h}:flags=lanczos,fps={out_fps},format=yuv420p",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "14", str(out)], check=True)


def still_to_clip(still: Path, duration: float, fps: int, out: Path, w: int, h: int, move: str = "push_in") -> None:
    """Animate a single still with a smooth Ken-Burns move (used by 'stills' mode and the fallback)."""
    n = max(2, int(round(duration * fps)))
    zoom = {"pull_out": f"1.12-0.10*on/{n}", "static": f"1.03+0.02*on/{n}"}.get(move, f"1.0+0.12*on/{n}")
    x = {"dolly_side": f"(iw-iw/zoom)*on/{n}", "orbit_left": f"(iw-iw/zoom)*(1-on/{n})"}.get(move, "(iw-iw/zoom)/2")
    y = {"pan_up": f"(ih-ih/zoom)*(1-on/{n})", "pan_down": f"(ih-ih/zoom)*on/{n}",
         "crane_down": f"(ih-ih/zoom)*on/{n}"}.get(move, "(ih-ih/zoom)/2")
    vf = (f"scale={w * 2}:{h * 2}:flags=lanczos,zoompan=z='{zoom}':x='{x}':y='{y}':d={n}:s={w}x{h}:fps={fps},"
          "format=yuv420p")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still),
                    "-vf", vf, "-frames:v", str(n), "-c:v", "libx264", "-preset", "medium", "-crf", "14", str(out)],
                   check=True)


class BlenderRenderer:
    name = "blender"

    def __init__(self, cfg):
        self.cfg = cfg
        self.b = cfg.section("render.blender")
        self.cmd = find_blender(self.b.get("executable", "auto"))
        self._engine: str | None = None

    def available(self) -> bool:
        return self.cmd is not None

    def _run(self, spec_path: Path, extra: list[str], timeout: int) -> subprocess.CompletedProcess:
        assert self.cmd
        args = [*self.cmd, str(BUILDER), "--", "--spec", str(spec_path), *extra] if len(self.cmd) > 1 else \
            [*self.cmd, str(BUILDER), "--spec", str(spec_path), *extra]
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def _spec(self, scene: dict, duration: float, out_dir: Path, seed: int, engine: str) -> dict:
        v = self.cfg.section("video")
        font = self.cfg.path("render.blender.font_path", "")
        return {
            "scene": scene, "duration": duration, "fps": int(self.b.get("render_fps", 24)),
            "width": v.get("width", 1080), "height": v.get("height", 1920),
            "resolution_percentage": int(self.b.get("resolution_percentage", 100)),
            "engine": engine,
            "samples": int(self.b.get("eevee_samples" if engine == "BLENDER_EEVEE" else "cycles_samples", 24)),
            "cycles_device": self.b.get("cycles_device", "METAL" if sys.platform == "darwin" else "CPU"),
            "seed": seed, "style": self.cfg.section("render.style"),
            "font_path": str(font) if font.is_file() else None, "output_dir": str(out_dir),
            "dof": bool(self.b.get("dof", True)),
        }

    def engine(self, work: Path) -> str:
        """Pick EEVEE when it works on this machine (fast, GPU), else Cycles."""
        if self._engine:
            return self._engine
        want = self.b.get("render_engine", "auto")
        if want in ("BLENDER_EEVEE", "CYCLES"):
            self._engine = want
            return want
        probe = work / "_probe"
        probe.mkdir(parents=True, exist_ok=True)
        spec = self._spec({"environment": "studio", "props": ["star"], "mascot_action": "idle",
                           "camera": "static", "on_screen_text": ""}, 0.2, probe, 1, "BLENDER_EEVEE")
        spec["resolution_percentage"] = 10
        (probe / "spec.json").write_text(json.dumps(spec))
        try:
            r = self._run(probe / "spec.json", ["--still", "1"], 300)
            ok = "DVA_RENDER_OK" in r.stdout and (probe / "still_0001.png").exists()
        except subprocess.TimeoutExpired:
            ok = False
        shutil.rmtree(probe, ignore_errors=True)
        self._engine = "BLENDER_EEVEE" if ok else "CYCLES"
        log.info("Blender engine selected: %s", self._engine)
        return self._engine

    def render_scene(self, scene: dict, duration: float, work: Path, seed: int, out_clip: Path) -> Path:
        frames = work / f"frames_{scene['id']:02d}"
        shutil.rmtree(frames, ignore_errors=True)
        frames.mkdir(parents=True)
        engine = self.engine(work)
        spec = self._spec(scene, duration, frames, seed, engine)
        spec_path = frames / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False))
        mode = self.b.get("mode", "animated")
        timeout = int(self.b.get("timeout_per_scene", 2400))
        extra = ["--still", str(max(1, int(duration * spec["fps"] * 0.6)))] if mode == "stills" else []
        r = self._run(spec_path, extra, timeout)
        if "DVA_RENDER_OK" not in r.stdout:
            raise RenderFailed(f"Blender scene {scene['id']} failed: {(r.stderr or r.stdout)[-1500:]}")
        w, h = spec["width"], spec["height"]
        fps = int(self.cfg.get("video.fps", 30))
        if mode == "stills":
            still = next(frames.glob("still_*.png"))
            still_to_clip(still, duration, fps, out_clip, w, h, scene.get("camera", "push_in"))
        else:
            encode_frames(frames, spec["fps"], fps, out_clip, w, h)
        if not self.b.get("keep_frames", False):
            shutil.rmtree(frames, ignore_errors=True)
        return out_clip

    def render_all(self, scenes: list[dict], durations: list[float], work: Path, seed_base: str) -> list[Path]:
        work.mkdir(parents=True, exist_ok=True)
        self.engine(work)
        jobs = []
        for s, d in zip(scenes, durations):
            seed = int(hashlib.sha1(f"{seed_base}:{s['id']}".encode()).hexdigest()[:8], 16)
            jobs.append((s, d, seed, work / f"scene_{s['id']:02d}.mp4"))
        par = max(1, int(self.b.get("parallel_scenes", 2)))
        with ThreadPoolExecutor(par) as ex:
            futs = [ex.submit(self.render_scene, s, d, work, seed, out) for s, d, seed, out in jobs]
            return [f.result() for f in futs]


def build_renderers(cfg) -> list:
    from .fallback import FallbackRenderer
    out = []
    for name in cfg.get("render.engine_order", ["blender", "fallback"]):
        r = BlenderRenderer(cfg) if name == "blender" else FallbackRenderer(cfg) if name == "fallback" else None
        if r and r.available():
            out.append(r)
    if not out:
        raise ProviderUnavailable("No renderer available")
    return out


def render_video_scenes(cfg, scenes, durations, work: Path, seed_base: str) -> tuple[list[Path], str]:
    """Try renderers in order; returns (clips, renderer_name)."""
    errors = []
    for r in build_renderers(cfg):
        try:
            log.info("Rendering %d scenes with %s", len(scenes), r.name)
            return r.render_all(scenes, durations, work, seed_base), r.name
        except Exception as exc:  # noqa: BLE001
            log.exception("renderer %s failed", r.name)
            errors.append(f"{r.name}: {exc}")
    raise RenderFailed(" | ".join(errors))


os.environ.setdefault("PYTHONUNBUFFERED", "1")
