"""Production package: everything needed to finish/publish the video by hand
when automatic rendering or publishing is unavailable."""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from ..core.models import FactSheet


def script_markdown(script: dict, sheet: FactSheet, meta: dict | None) -> str:
    lines = [f"# {script.get('title') or sheet.topic}", "", f"**Topic:** {sheet.topic}  ",
             f"**Category:** {sheet.category}  ", f"**Angle:** {sheet.angle}", "", "## Scenes", ""]
    for s in script["scenes"]:
        lines += [f"### Scene {s['id']}  ({s['environment']} · props: {', '.join(s['props'])} · "
                  f"mascot: {s['mascot_action']} · camera: {s['camera']} · sfx: {s['sfx']})",
                  f"- **Subtitle (Roman):** {s['narration_display']}",
                  f"- **Voice (Devanagari):** {s['narration_tts']}",
                  f"- **On-screen text:** {s['on_screen_text'] or '—'}",
                  f"- **Facts:** {', '.join(s['claim_ids']) or '—'}", ""]
    lines += ["## Verified facts", ""]
    for c in sheet.verified_claims:
        lines.append(f"- **{c.id}** {c.text}  ")
        for e in c.evidence:
            lines.append(f"  - [{e['source_id']}] “{e['quote']}”")
    lines += ["", "## Sources", ""] + [f"- [{s.id}] {s.title} — {s.url}" for s in sheet.sources]
    if meta:
        lines += ["", "## YouTube", "", f"**Title:** {meta['youtube']['title']}", "", "```",
                  meta["youtube"]["description"], "```", "", "## Instagram", "", "```",
                  meta["instagram"]["caption"], "```"]
    return "\n".join(lines) + "\n"


def build_package(out_dir: Path, script: dict, sheet: FactSheet, meta: dict | None, reason: str,
                  extra_files: list[Path] | None = None) -> Path:
    pkg = out_dir / "package"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SCRIPT.md").write_text(script_markdown(script, sheet, meta), encoding="utf-8")
    (pkg / "script.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    (pkg / "facts.json").write_text(json.dumps(sheet.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    if meta:
        (pkg / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (pkg / "README.txt").write_text(
        f"Production package — automatic step stopped: {reason}\n\n"
        "Contents: SCRIPT.md (scene-by-scene plan, voice text, facts with quotes, sources, captions),\n"
        "script.json / facts.json / metadata.json (machine-readable), and whatever media was produced\n"
        "(voice WAVs, subtitles, scene clips, final.mp4).\n\n"
        "To finish manually: re-run `dva resume <run-id>` after fixing the cause, or edit the clips in any\n"
        "editor using subtitles.srt and the voice files, then upload with the captions in metadata.json.\n",
        encoding="utf-8")
    for f in extra_files or []:
        if f and Path(f).exists():
            dest = pkg / Path(f).name
            if Path(f).is_dir():
                shutil.copytree(f, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(f, dest)
    zpath = out_dir / "production_package.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(pkg))
    return zpath
