# Daily AI Animated Facts — ₹0/month Shorts & Reels factory

Every morning at **9:00 AM IST** this system finds a trending or evergreen *interesting fact*,
verifies it against trusted sources and writes a short script in **easy, simple English** (Hinglish is
available as a setting). What happens next depends on
`workflow.mode` in `config/settings.yaml`:

### Prompt mode (default): you make the video and voiceover; the system does everything else

1. **9:00 AM, on Telegram:** today's trending topic, the **verified facts and sources**, an **easy-English
   voiceover script**, a **background-music brief** (genre, mood, tempo, instruments, energy curve, words to
   search in CapCut / YouTube Audio Library, with a music cue for each scene), and **copy-paste prompts for your AI video
   tool**: one prompt per clip (≤ 8 s each by default), each repeating the channel style and the recurring host
   character so separately generated clips still match. You also get a single long prompt and a negative prompt.
2. **You make the video:** clips, edit, voiceover, subtitles, music.
3. **Send it back:** just the video, **as a file**, to the Telegram bot (≤ 20 MB), or to the Google Drive / inbox
   folder for bigger files.
4. **The system publishes it** to **YouTube Shorts** and **Instagram Reels** exactly as you made it, with an
   **automatically written caption, hashtags and YouTube tags** (shown to you in the morning message). To use
   your own caption instead, type it with the video. The file is only re-encoded if a platform would reject it.
   A silent video is refused (missing voiceover?). The bot replies with the links.

Buttons on the daily message: *🔁 New script & prompts* and *⏭ Skip this topic*. Optional extras (TTS voiceover,
auto subtitles, watermark, a preview before publishing) are in `prompt_mode` in `config/settings.yaml`, all off.

### Full mode: fully automatic 3D rendering

`workflow.mode: full`: the system renders the video itself in Blender (procedural 3D scenes with the
mascot *Gyaanu*), voices it with Piper, edits it with FFmpeg and sends it for **Approve / Regenerate /
Reject** before publishing.

Both modes use free and open-source tools only. No paid APIs, no hosting. Run it on your Mac,
or in **cloud mode** on free GitHub Actions so your Mac can stay off and you only need your phone:
see **[docs/CLOUD.md](docs/CLOUD.md)**.

```
 n8n / launchd (09:00 IST)
        │
        ▼
 Discover (Trends, RSS, Wikipedia, history) ─▶ Research & verify (quotes must match sources)
        ─▶ easy-English script + fact audit
                     │
      prompt mode ───┴──────────────────────────── full mode
        ▼                                             ▼
 AI-video prompts + voiceover ─▶ Telegram        Blender 3D render ─▶ FFmpeg edit ─▶ QC
        ▼                                             ▼
 you send your video (Telegram / inbox)          Telegram Approve / Regenerate / Reject
        ▼                                             │
 conform 9:16 + voice/music/subs (if silent) + QC     │
        └──────────────────────┬──────────────────────┘
                               ▼
          YouTube Shorts + Instagram Reels ─▶ metrics every 6 h ─▶ topic & hook weights
```

## What's in the box

| Stage | Implementation | Free because |
|---|---|---|
| Topic discovery | Google Trends India RSS, NASA/ScienceDaily/BBC/ESA/… RSS, Wikipedia most-read & on-this-day, curated evergreen pool (`src/dva/discovery`) | public feeds |
| De-duplication | SQLite history (365 days), keyword similarity, blacklist on reject | local |
| Research & verification | Wikipedia API + its citations on trusted domains + the trend's own article; LLM must quote sources **verbatim**; quotes are string-matched, numbers cross-checked, core fact needs 2 independent domains; judge LLM checks entailment (`src/dva/research`) | Gemini free tier / local Ollama |
| Script | Gemini writes hook (≤3 s) → story → reveal → ending + CTA, easy English by default (Hinglish optional), plus automatic title/caption/hashtags/YouTube tags; validator + second-pass fact audit, auto-rewrite (`src/dva/script`) | Gemini free tier |
| 3D animation | Blender scene builder: 12 environments, 41 procedural props, the mascot **Gyaanu** (consistent channel identity), 9 camera moves, 3D keyword text, ambient particles; EEVEE on Mac GPU, Cycles fallback (`src/dva/render/blender`) | Blender (GPL) |
| Voice | Piper neural TTS, Hindi voices (fallbacks: macOS *Lekha*, espeak-ng) | open source, offline |
| Music & SFX | Generated procedurally per video with numpy — zero copyright risk (`src/dva/audio`) | local |
| Edit | FFmpeg: xfade transitions, word-by-word highlighted subtitles (ASS), progress bar, watermark, sidechain ducking, loudness normalised to −14 LUFS | FFmpeg |
| QC | resolution, codec, fps, duration, file size, decode integrity, black frames, long silences, per-scene voice presence, subtitle/voice sync, subtitle overlap/coverage, facts verified | local |
| Approval | Telegram bot (long polling: no public server) — video + full script + facts + sources + captions | Telegram Bot API |
| Publishing | YouTube Data API v3 resumable upload; Instagram Graph API resumable Reels upload (no public hosting needed) | official free APIs |
| Analytics | YouTube Data + Analytics API (views, likes, comments, shares, retention), Instagram insights → category weights + "what worked" hooks fed into the next script | official free APIs |
| Fallbacks | Ollama (local LLM) when Gemini quota is exhausted; Pillow 2.5D renderer when Blender fails; **production package (zip)** sent to Telegram when any automatic stage fails | local |

## Quick start (Mac)

```bash
git clone <this repo> ~/Daily-Video-Automation && cd ~/Daily-Video-Automation
./scripts/setup_mac.sh                 # ffmpeg, venv, Piper Hindi voice, OFL fonts, .env, inbox/
.venv/bin/python -m dva demo           # keyless demo prompt package -> output/<date>_run1/PROMPTS.md
.venv/bin/python -m dva submit ~/Downloads/my_video.mp4   # publish it (automatic caption + hashtags + tags)
.venv/bin/python -m dva demo --mode full --fast            # (full mode) demo 3D render, needs Blender
```

Then add credentials (all free) — step by step in **[docs/SETUP.md](docs/SETUP.md)**:

1. `GEMINI_API_KEY` (Google AI Studio)
2. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (@BotFather)
3. YouTube OAuth client → `dva auth-youtube`
4. `INSTAGRAM_ACCESS_TOKEN` + `INSTAGRAM_USER_ID`

```bash
.venv/bin/python -m dva doctor          # shows what's ready / missing
.venv/bin/python -m dva run --force     # one real end-to-end run now
./scripts/setup_mac.sh --services       # bot + n8n (or launchd) schedule at 09:00
```

## Commands

| Command | What it does |
|---|---|
| `dva run [--force] [--json] [--mode prompt\|full]` | the daily pipeline (idempotent per day; `--json` for n8n) |
| `dva submit <video> [--caption "…" \| --caption-file f.txt] [--video-id N]` | prompt mode: hand in your finished video (caption optional: automatic otherwise) |
| `dva caption <id> "…"` / `dva caption <id> --suggested` | set the caption for a video that's waiting for one |
| `dva watch` | prompt mode without Telegram: watch the inbox folder |
| `dva demo [--mode prompt\|full] [--fast] [--fallback] [--tts espeak]` | built-in octopus demo without API keys |
| `dva bot` | Telegram listener: receives your videos, watches the inbox, buttons, `/status`, `/new`, `/pending`, `/prompts <id>`, `/skip <id>`, `/metrics` |
| `dva approve|reject|regen <id>` | same as the Telegram buttons |
| `dva publish <id>` | retry publishing an approved video (never re-uploads a published platform) |
| `dva mark-published <id> <platform> <url>` / `dva reset-publication <id> <platform>` | resolve an ambiguous upload by hand |
| `dva metrics` / `dva report` | collect and print performance |
| `dva auth-youtube` | one-time YouTube OAuth consent |
| `dva poll [--check]` | cloud mode: one pass, then exit (what GitHub Actions runs) |
| `dva doctor` | health check: tools, voices, fonts, credentials, today's LLM quota use |
| `dva cleanup --days 14` | delete heavy intermediates of old runs |

## Reliability & safety

- **Never invents facts:** a claim survives only if the LLM's evidence quote is found in the fetched source text, all its numbers appear in that evidence, and a separate judge call agrees. The script may only cite verified claim IDs, every number it speaks must belong to a cited claim, and a final audit flags any unsupported assertion. If too little is verified, the topic is dropped and the next candidate is tried.
- **No unlicensed content:** all 3D assets, music and SFX are generated procedurally by this code. Fonts are SIL-OFL. Optional music library mode only reads files you put in `assets/music/` (use CC0 tracks).
- **Duplicate-upload prevention:** one `publications` row per (video, platform) (UNIQUE), state is written before each remote call, YouTube uploads resume via the stored session URI, identical files (SHA-256) can't be registered twice, approvals are atomic compare-and-set (double-taps do nothing), and any ambiguous crash state becomes `unknown` and is escalated to you instead of retried.
- **Quotas are finite:** each LLM provider has a self-imposed daily request cap and minimum spacing (`llm.*.daily_request_limit`, `min_seconds_between_calls`), tracked in SQLite. A run uses about 5 LLM calls. Per-platform daily upload caps are enforced too.
- **Retries** with exponential backoff and `Retry-After` for network/5xx/429 errors. **Secrets** live in `.env` (chmod 600) or the macOS Keychain (`KEY=keychain:<service>`) and are redacted from logs.
- **Modular:** each stage sits behind a small interface (`LLMProvider`, `TTSEngine`, renderer with `render_all`, `Publisher`). Add a provider by adding a class and listing it in `config/settings.yaml`.

## Project layout

```
config/settings.yaml      all tunables (channel, schedule, quotas, style, QC thresholds)
src/dva/pipeline.py       orchestration (both modes), failure → production package
src/dva/prompting/        prompt mode: AI-video prompt writer + PROMPTS.md
src/dva/footage/          prompt mode: take your video + caption, QC, publish (optional extras); inbox watcher
src/dva/discovery/        trend/RSS/Wikipedia sources, selector (dedup + LLM rank + performance weights)
src/dva/research/         source fetching + fact verification
src/dva/script/           script writer (easy English / Hinglish), captions + hashtags + tags, validator, fact audit
src/dva/tts/              Piper / macOS say / espeak engines
src/dva/render/           Blender runner, scene_builder.py (runs inside Blender), Pillow fallback, vocab
src/dva/audio/            procedural music + SFX
src/dva/editor/           timeline, subtitles, FFmpeg compose, production package
src/dva/qc/               automated quality checks
src/dva/approval/         Telegram client + bot
src/dva/publish/          metadata, YouTube, Instagram, duplicate-safe publisher
src/dva/analytics/        metrics collection + learnings
n8n/                      importable n8n workflow (09:00 IST, metrics every 6 h, weekly cleanup)
.github/workflows/dva.yml cloud mode: GitHub Actions runner (every ~10 min), encrypted state branch
src/dva/cloud.py          cloud mode: one stateless pass (Telegram, Drive inbox, daily topic, metrics)
launchd/                  macOS services (bot, n8n, or n8n-free daily job)
tests/                    offline test-suite incl. a full end-to-end run with fakes
```

## Known limits (honest list)

- **Telegram file limit (prompt mode):** bots can only *download* files up to 20 MB. A 60 s 1080p video
  is often bigger, so use the inbox folder for those (the bot tells you when a file is too big).
- **Your file, your responsibility:** in prompt mode the system doesn't change your video, so
  facts you add beyond the verified list aren't checked. The daily message lists exactly which facts are verified.
- **Prompt quality** comes from Gemini; when it's unavailable you get simpler template prompts built from the
  scene plan (still complete and usable).

- **Render time (full mode):** a full-quality 60 s video is ~1,400 frames. On Apple Silicon with EEVEE expect roughly 10-40 min; set `render.blender.mode: stills` (one rendered frame per scene + camera motion) for ~2 min renders, or lower `resolution_percentage`.
- **YouTube:** uploads from an unverified Google Cloud project are locked to **private** until the project passes Google's (free) API compliance audit. The bot warns you when this happens.
- **Instagram:** requires a Professional (Business/Creator) account; Meta's app/permission setup is the most fiddly step — see docs/SETUP.md.
- **Hindi TTS quality:** Piper's Hindi voices are decent but not studio-grade, and English words are spoken via their Devanagari spelling. You can swap the voice in `tts.piper.voice`.
- **Free tiers change:** Google can change Gemini free-tier limits at any time; when it's exhausted the run falls back to Ollama (if installed) or stops with a notification — it never switches to a paid tier.
