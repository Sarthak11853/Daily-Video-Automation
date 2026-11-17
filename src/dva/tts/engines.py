"""Local, free text-to-speech engines with automatic fallback:
Piper (neural, best quality) -> macOS `say` (Lekha, Hindi) -> espeak-ng.
All produce a 48 kHz mono WAV, trimmed and lightly mastered."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from ..core.errors import ProviderUnavailable

log = logging.getLogger(__name__)

POLISH = ("silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,"
          "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.08,areverse,"
          "highpass=f=70,equalizer=f=3200:t=q:w=1.2:g=2.5,acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
          "aresample=48000")


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def finalize(raw: Path, out: Path) -> float:
    _ffmpeg(["-i", str(raw), "-af", POLISH, "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)])
    return wav_duration(out)


class TTSEngine:
    name = "base"

    def __init__(self, cfg: dict, root: Path):
        self.cfg = cfg
        self.root = root

    def available(self) -> bool:
        raise NotImplementedError

    def _raw(self, text: str, raw_path: Path) -> None:
        raise NotImplementedError

    def synthesize(self, text: str, out: Path) -> float:
        raw = out.with_suffix(".raw.wav") if self.name != "macos_say" else out.with_suffix(".raw.aiff")
        self._raw(text, raw)
        dur = finalize(raw, out)
        raw.unlink(missing_ok=True)
        return dur


class PiperTTS(TTSEngine):
    name = "piper"

    def model_path(self) -> Path:
        voice = self.cfg.get("voice", "hi_IN-pratham-medium")
        p = Path(voice)
        if p.suffix == ".onnx" and p.exists():
            return p
        return self.root / "assets" / "voices" / f"{voice}.onnx"

    def _cmd(self) -> list[str] | None:
        exe = shutil.which("piper")
        if exe:
            return [exe]
        try:
            import piper  # noqa: F401
            return [sys.executable, "-m", "piper"]
        except ImportError:
            return None

    def available(self) -> bool:
        return self._cmd() is not None and self.model_path().exists()

    def _raw(self, text: str, raw_path: Path) -> None:
        cmd = self._cmd()
        if not cmd:
            raise ProviderUnavailable("piper not installed")
        cmd += ["-m", str(self.model_path()), "-f", str(raw_path),
                "--length-scale", str(self.cfg.get("length_scale", 0.92)),
                "--noise-scale", str(self.cfg.get("noise_scale", 0.6)),
                "--sentence-silence", str(self.cfg.get("sentence_silence", 0.15))]
        subprocess.run(cmd, input=text.encode("utf-8"), check=True, capture_output=True, timeout=300)


class MacSayTTS(TTSEngine):
    name = "macos_say"

    def available(self) -> bool:
        if sys.platform != "darwin" or not shutil.which("say"):
            return False
        voice = self.cfg.get("voice", "Lekha")
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        return any(line.startswith(voice) for line in out.splitlines())

    def _raw(self, text: str, raw_path: Path) -> None:
        subprocess.run(["say", "-v", self.cfg.get("voice", "Lekha"), "-r", str(self.cfg.get("rate", 185)),
                        "-o", str(raw_path), text], check=True, timeout=300)


class EspeakTTS(TTSEngine):
    name = "espeak"

    def _exe(self) -> str | None:
        return shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self) -> bool:
        return self._exe() is not None

    def _raw(self, text: str, raw_path: Path) -> None:
        subprocess.run([self._exe(), "-v", self.cfg.get("voice", "hi"), "-s", str(self.cfg.get("speed", 160)),
                        "-w", str(raw_path), text], check=True, timeout=300)


class ToneTTS(TTSEngine):
    """Test-only engine: speech-like modulated tone whose length follows the text."""
    name = "tone"

    def available(self) -> bool:
        return True

    def _raw(self, text: str, raw_path: Path) -> None:
        sr = 22050
        dur = max(0.8, len(text.split()) / 2.6)
        t = np.arange(int(sr * dur)) / sr
        env = (0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)) ** 2
        sig = 0.3 * env * np.sin(2 * np.pi * (180 + 30 * np.sin(2 * np.pi * 0.7 * t)) * t)
        with wave.open(str(raw_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((sig * 32767).astype(np.int16).tobytes())


ENGINES = {"piper": PiperTTS, "macos_say": MacSayTTS, "espeak": EspeakTTS, "tone": ToneTTS}


def pick_engine(cfg, root: Path) -> TTSEngine:
    for name in cfg.get("tts.engine_order", ["piper", "macos_say", "espeak"]):
        cls = ENGINES.get(name)
        if not cls:
            continue
        eng = cls(cfg.section(f"tts.{name}"), root)
        if eng.available():
            log.info("TTS engine: %s", name)
            return eng
    raise ProviderUnavailable("No TTS engine available (install piper voice via scripts/setup_mac.sh)")


def voice_scenes(script: dict, engine: TTSEngine, out_dir: Path) -> list[dict]:
    """Synthesize every scene; returns [{id, wav, duration}]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    res = []
    for s in script["scenes"]:
        wav = out_dir / f"scene_{s['id']:02d}.wav"
        text = s.get("narration_tts") or s["narration_display"]
        dur = engine.synthesize(text, wav)
        res.append({"id": s["id"], "wav": str(wav), "duration": round(dur, 3)})
    return res
