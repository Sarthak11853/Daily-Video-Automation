# Setup guide (all free)

Run `./scripts/setup_mac.sh` first (Blender and the Piper voice are only installed when the system has to
render or voice videos itself), then fill in `.env` (it is created with `chmod 600`).
After each step, `.venv/bin/python -m dva doctor` shows what is ready.

> Prefer the macOS Keychain? Store a secret with
> `security add-generic-password -s dva-gemini -a $USER -w 'YOUR_KEY'`
> and write `GEMINI_API_KEY=keychain:dva-gemini` in `.env`.

---

## 1. Gemini API key (research + scripts)

1. Open <https://aistudio.google.com/apikey> → **Create API key** (free tier, no billing needed).
2. `.env`: `GEMINI_API_KEY=...`
3. **Do not enable billing** on that Google Cloud project — then the key can never incur charges.
   The free tier has per-minute and per-day limits that Google changes from time to time; the
   system caps itself at `llm.gemini.daily_request_limit` (default 40/day; a run uses ~5).
4. Model names: defaults are the aliases `gemini-flash-latest` / `gemini-flash-lite-latest`.
   If Google retires them, set `llm.gemini.model` in `config/settings.yaml`.

**Optional offline fallback:** install [Ollama](https://ollama.com) and `ollama pull qwen2.5:7b`.
It is used automatically when Gemini is unavailable or out of quota (slower, lower quality).

## 2. Telegram bot (approval)

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token.
2. `.env`: `TELEGRAM_BOT_TOKEN=...`
3. Send `/start` to your new bot, then run `.venv/bin/python -m dva telegram-whoami` and put the
   number shown in `.env` as `TELEGRAM_CHAT_ID=...`. Only this chat can approve videos.
4. Start the listener: `.venv/bin/python -m dva bot` (or `./scripts/setup_mac.sh --services`
   to keep it running in the background with launchd).

Bot uploads are limited to 50 MB; the encoder targets ≤ 49 MB (QC checks it).

## 3. YouTube (Shorts publishing + stats)

1. <https://console.cloud.google.com> → create a project (no billing).
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **OAuth consent screen**: External, add yourself as a *test user*; scopes are requested by the app.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download JSON →
   save as `secrets/youtube_client_secret.json`.
5. Run `.venv/bin/python -m dva auth-youtube`, sign in with the channel's Google account.
   The refresh token is stored in `secrets/youtube_token.json` (chmod 600).
6. **Important — audit:** YouTube locks videos uploaded through *unverified* API projects
   (created after 28 July 2020) to **private**. To publish publicly via the API, submit the free
   *YouTube API Services audit/compliance form* for your project. Until then, approved videos are
   uploaded as private and the bot tells you; you can switch them to public in YouTube Studio.
7. While the consent screen is in "Testing" mode Google may expire refresh tokens after 7 days;
   publishing the consent screen (in-house/personal use) avoids weekly re-auth.

Quota: the default 10,000 units/day is plenty (an upload costs ~1,600; a stats call 1).

## 4. Instagram (Reels publishing + insights)

Requirements: an Instagram **Professional** account (Business or Creator — switching is free).

**Instagram API with Instagram Login** (simplest, no Facebook Page needed):

1. <https://developers.facebook.com> → *My Apps* → **Create app** → use case
   *"Manage messaging & content on Instagram"*.
2. In the app's Instagram product, add your Instagram account (as tester if the app is in
   development mode — that's fine for publishing to your own account).
3. Grant permissions `instagram_business_basic`, `instagram_business_content_publish`,
   `instagram_business_manage_insights`, then **generate a token** for your account.
4. `.env`: `INSTAGRAM_ACCESS_TOKEN=...` and `INSTAGRAM_USER_ID=...` (the numeric IG user id shown
   next to the token; or call `GET https://graph.instagram.com/me?fields=user_id,username`).
5. The token is long-lived (60 days); `dva` refreshes it automatically before it expires.

If you already use **Facebook Login** with a linked Page, set
`publish.instagram.api: facebook_login` and use a Page-scoped token + the IG business account id.

Upload uses Meta's *resumable* upload (`rupload.facebook.com`), so the video never needs a public URL.

## 5. Scheduling

**Option A — n8n (as requested):**
```bash
npm install -g n8n          # free, self-hosted
./scripts/setup_mac.sh --services
```
Open <http://localhost:5678>, *Import from file* → `n8n/daily_facts_workflow.local.json`
(paths already point at this folder), then **Activate**. The workflow timezone is Asia/Kolkata.
n8n 2.x hides the *Execute Command* node by default; the provided launchd file starts n8n with
`NODES_EXCLUDE=[]` to enable it. If you run n8n another way, set that env var yourself.

**Option B — launchd only:** if n8n isn't installed, `--services` installs `com.dva.daily`
(09:00 *Mac local time* — set the Mac's time zone to India).

**Keep the Mac awake:** `sudo pmset repeat wakeorpoweron MTWRFSU 08:50:00`, and keep it on power.
A missed run is executed when the Mac wakes; runs are idempotent per day.

## 6. Your daily routine (prompt mode)

1. ~9:00 AM the bot sends today's video #N: topic, **verified facts + sources**, **one message per scene
   with tap-to-copy clip prompts**, a negative prompt, a single long prompt, a suggested Hinglish script,
   a suggested caption and `PROMPTS.md` (everything in one file).
2. Make the video in your tools: clips, editing, **your voiceover, your subtitles**. Export a vertical
   9:16 MP4 (1080×1920, H.264/AAC is ideal; anything else is converted automatically).
3. Deliver it with **your caption** (first line = YouTube title; the whole text = YouTube description +
   Instagram caption; hashtags included):
   - **Telegram:** send the video *as a file* (≤ 20 MB) and put the caption in the same message, or send the
     caption as a text message right after (or even before) the video.
   - **Inbox folder:** `N.mp4` + `N.txt` with the caption (any size).
   - **Terminal:** `dva submit video.mp4 --caption-file caption.txt`.
4. It's uploaded **exactly as you made it**. The bot replies with the YouTube and Instagram links, or says
   what's wrong (e.g. "no sound") so you can resend.

No caption sent? The bot asks for one. Tap **💬 Use suggested caption** to post the system's version.
Don't like the topic? Tap **⏭ Skip this topic** (never suggested again) and send `/new`.
Want different prompts for the same facts? Tap **🔁 New script & prompts**.

Options in `config/settings.yaml` → `prompt_mode` (all off by default):
`confirm_before_publish` (preview + Publish button), `generate_voiceover` (TTS voiceover + .srt with the
prompts), `post_process` (auto subtitles/voiceover), `add_branding` (watermark + progress bar),
`allow_silent_video`, `caption_source` (`yours_only` / `yours_or_generated` / `generated`),
`append_sources_to_caption`.

**Inbox on your phone:** set `prompt_mode.inbox_dir` to an iCloud Drive folder, e.g.
`~/Library/Mobile Documents/com~apple~CloudDocs/DVA Inbox`, then save videos there from the iPhone
Files app. The Mac picks them up once they have finished syncing.

## 7. Make it yours

`config/settings.yaml`:
- `channel.name`, `channel.handle`, `channel.mascot_name`, hashtags
- `render.style` colours (mascot, accent, keyword text) — keep them fixed for brand recognition
- `tts.piper.voice` (`hi_IN-pratham-medium`, `hi_IN-priyamvada-medium`, …; re-run setup with
  `DVA_VOICE=<name> ./scripts/setup_mac.sh` to download another)
- `topics.rss_feeds`, `topics.blocked_keywords`, `research.trusted_domains`
- `prompt_mode.style_bible`, `mascot_description`, `negative_prompt`, `target_clip_seconds` (match your
  video tool's maximum clip length), `include_mascot`
- `workflow.mode: full` + `render.blender.*` if you want the system to render the 3D video itself
