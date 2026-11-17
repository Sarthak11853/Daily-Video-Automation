"""`dva doctor`: report what is installed/configured and what will be used."""
from __future__ import annotations

import shutil
import subprocess


def doctor(pipe) -> int:
    cfg, db = pipe.cfg, pipe.db
    rows: list[tuple[str, bool, str]] = []

    def add(name, ok, detail=""):
        rows.append((name, bool(ok), detail))

    mode = cfg.get("workflow.mode", "prompt")
    inbox = cfg.path("prompt_mode.inbox_dir", "inbox").expanduser()
    add("mode", True, f"{mode} — " + ("you generate the video from daily prompts" if mode == "prompt"
                                       else "Blender renders the video here"))
    if mode == "prompt":
        add("inbox", True, str(inbox))
    ff = shutil.which("ffmpeg")
    ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n")[0] if ff else ""
    add("ffmpeg", ff, ver)
    from .render.runner import BlenderRenderer
    br = BlenderRenderer(cfg)
    add("blender", br.available() or mode == "prompt",
        " ".join(br.cmd or []) or ("not needed in prompt mode" if mode == "prompt"
                                   else "not found — fallback renderer will be used"))
    from .tts.engines import ENGINES
    needs_tts = mode == "full" or cfg.get("prompt_mode.generate_voiceover", False)
    for name in cfg.get("tts.engine_order", []) if needs_tts else []:
        eng = ENGINES[name](cfg.section(f"tts.{name}"), cfg.root)
        extra = str(eng.model_path()) if name == "piper" else ""
        add(f"tts:{name}", eng.available(), extra)
    if not needs_tts:
        add("tts", True, "not needed: you record the voiceover")
    llm = pipe.llm
    for p in llm.providers:
        used = db.quota_used(f"llm:{p.name}", pipe.today())
        add(f"llm:{p.name}", p.available(), f"{used}/{p.cfg.get('daily_request_limit')} requests today")
    add("telegram", pipe.telegram is not None, "approval via Telegram" if pipe.telegram else "set TELEGRAM_* in .env")
    from .publish.instagram import Instagram
    from .publish.youtube import YouTube
    add("youtube", YouTube(cfg).configured(), "secrets/youtube_token.json" if YouTube(cfg).configured()
        else "add client secret + run `dva auth-youtube`")
    add("instagram", Instagram(cfg, db).configured(), "" if Instagram(cfg, db).configured() else "set INSTAGRAM_* in .env")
    fonts = list(cfg.path("subtitles.fonts_dir", "assets/fonts").glob("*.ttf"))
    add("fonts", bool(fonts), ", ".join(f.name for f in fonts) or "run scripts/setup_mac.sh to fetch OFL fonts")
    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"{'✅' if ok else '❌'} {name.ljust(width)}  {detail}")
    essential = {"ffmpeg"}
    return 0 if all(ok for n, ok, _ in rows if n in essential) else 1
