"""Procedural, license-free background music and sound effects (numpy).
Every track is generated locally from a seed, so there is zero copyright risk
and each video gets its own (but stylistically consistent) theme."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
NOTE = {n: i for i, n in enumerate(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}
PROGRESSIONS = [[0, 7, 9, 5], [0, 5, 9, 7], [9, 5, 0, 7], [0, 9, 5, 7]]  # I-V-vi-IV and friends (semitones)


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> None:
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = np.stack([audio, audio], axis=1)
    peak = np.max(np.abs(audio)) or 1.0
    audio = audio / max(peak, 1.0) * 0.98 if peak > 0.98 else audio
    with wave.open(str(path), "wb") as w:
        w.setnchannels(audio.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path: Path) -> np.ndarray:
    """Mono float array at the file's rate (expects 48 kHz from the TTS stage)."""
    with wave.open(str(path), "rb") as w:
        n, ch, sw = w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = np.frombuffer(w.readframes(n), dtype="<i2" if sw == 2 else np.uint8).astype(np.float64)
    if sw == 2:
        raw /= 32768
    if ch > 1:
        raw = raw.reshape(-1, ch).mean(axis=1)
    return raw


def _freq(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1.0, t / max(attack, 1e-4)) * np.exp(-t / max(decay, 1e-4))


def music(duration: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    bpm = float(rng.choice([96, 100, 104, 108]))
    root = 48 + int(rng.choice([0, 2, 4, 5, 7, 9]))      # C3..A3
    prog = PROGRESSIONS[int(rng.integers(len(PROGRESSIONS)))]
    beat = 60.0 / bpm
    bar = beat * 4
    n = int((duration + 1.0) * SR)
    out = np.zeros((n, 2))
    t_all = np.arange(n) / SR
    pluck_pattern = rng.permutation([0, 4, 7, 12, 7, 4, 12, 16])
    nbars = int(np.ceil((duration + 1) / bar))
    for b in range(nbars):
        chord_root = root + prog[b % len(prog)]
        minor = prog[b % len(prog)] in (9, 4)
        third = 3 if minor else 4
        start = int(b * bar * SR)
        seg = min(int(bar * SR), n - start)
        if seg <= 0:
            break
        t = np.arange(seg) / SR
        # warm pad (detuned sines with slow swell)
        pad = np.zeros(seg)
        for iv in (0, third, 7, 12):
            f = _freq(chord_root + iv)
            pad += np.sin(2 * np.pi * f * t) + 0.5 * np.sin(2 * np.pi * f * 1.004 * t + 1.3)
        swell = np.minimum(1, t / 0.6) * np.minimum(1, (bar - t) / 0.3)
        out[start:start + seg, 0] += 0.035 * pad * swell
        out[start:start + seg, 1] += 0.035 * pad * swell
        # bass on beats 1 and 3
        for k in (0, 2):
            s = start + int(k * beat * SR)
            ln = min(int(beat * 1.6 * SR), n - s)
            if ln > 0:
                tt = np.arange(ln) / SR
                bass = np.sin(2 * np.pi * _freq(chord_root - 12) * tt) * _env(ln, 0.01, 0.35)
                out[s:s + ln] += 0.16 * bass[:, None]
        # plucky arpeggio in 8ths
        for k in range(8):
            s = start + int(k * beat / 2 * SR)
            ln = min(int(0.4 * SR), n - s)
            if ln <= 0:
                continue
            iv = pluck_pattern[k] if pluck_pattern[k] not in (4, 16) else pluck_pattern[k] - (4 - third)
            f = _freq(chord_root + 12 + iv)
            tt = np.arange(ln) / SR
            p = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * _env(ln, 0.003, 0.12)
            pan = 0.5 + 0.35 * np.sin(k)
            out[s:s + ln, 0] += 0.07 * p * (1 - pan)
            out[s:s + ln, 1] += 0.07 * p * pan
        # soft kick + hats
        for k in range(4):
            s = start + int(k * beat * SR)
            ln = min(int(0.25 * SR), n - s)
            if ln > 0 and b > 0:
                tt = np.arange(ln) / SR
                kick = np.sin(2 * np.pi * (50 + 90 * np.exp(-tt * 30)) * tt) * _env(ln, 0.002, 0.09)
                out[s:s + ln] += 0.22 * kick[:, None]
            hs = start + int((k + 0.5) * beat * SR)
            hl = min(int(0.05 * SR), n - hs)
            if hl > 0 and b > 0:
                hat = rng.standard_normal(hl) * _env(hl, 0.001, 0.015)
                hat = np.diff(hat, prepend=0)  # crude high-pass
                out[hs:hs + hl] += 0.05 * hat[:, None]
    fade = np.minimum(1, t_all / 1.0) * np.minimum(1, np.maximum(0, (duration + 1.0 - t_all)) / 2.0)
    out *= fade[:, None]
    return out[: int(duration * SR)]


def sfx(kind: str, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    if kind in ("whoosh", "swoosh_up"):
        n = int(0.55 * SR)
        t = np.arange(n) / SR
        noise = rng.standard_normal(n)
        sweep = np.sin(np.pi * t / t[-1]) ** 2
        # moving band: mix of differently smoothed noise approximates a filter sweep
        k = 6 if kind == "whoosh" else 3
        smooth = np.convolve(noise, np.ones(k) / k, mode="same")
        return 0.35 * smooth * sweep
    if kind == "pop":
        n = int(0.12 * SR)
        t = np.arange(n) / SR
        return 0.6 * np.sin(2 * np.pi * (900 - 5000 * t) * t) * _env(n, 0.001, 0.04)
    if kind == "ding":
        n = int(1.2 * SR)
        t = np.arange(n) / SR
        return 0.25 * sum(a * np.sin(2 * np.pi * 1318.5 * m * t) for m, a in ((1, 1), (2.76, .4), (5.4, .2))) \
            * _env(n, 0.002, 0.35)
    if kind == "boom":
        n = int(1.0 * SR)
        t = np.arange(n) / SR
        return 0.7 * np.sin(2 * np.pi * (40 + 60 * np.exp(-t * 6)) * t) * _env(n, 0.005, 0.3) \
            + 0.1 * rng.standard_normal(n) * _env(n, 0.001, 0.08)
    if kind == "sparkle":
        n = int(0.8 * SR)
        out = np.zeros(n)
        for _ in range(7):
            s = int(rng.uniform(0, 0.5) * SR)
            ln = int(0.25 * SR)
            tt = np.arange(ln) / SR
            out[s:s + ln] += 0.12 * np.sin(2 * np.pi * rng.uniform(2500, 4500) * tt) * _env(ln, 0.001, 0.06)
        return out
    return np.zeros(1)
