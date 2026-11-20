# Cloud mode: phone only, Mac stays off

Everything runs on **GitHub Actions** (free for public repositories). You use only **Telegram** (and
optionally the **Google Drive** app) on your phone.

```
GitHub Actions, every ~10 min (08:30–01:30 IST), hourly at night
  ├─ after 09:00 IST: find trending fact → verify → script → prompts → Telegram
  ├─ your Telegram messages: your video → publish with automatic caption/hashtags/tags → links
  ├─ your Google Drive "DVA Inbox" folder: videos > 20 MB → publish
  ├─ reminders if something waits for you
  └─ every 6 h: views/likes/shares → better topics
Encrypted database ↔ branch "dva-state" (AES-256, key only in GitHub Secrets)
```

**What changes for you compared with running on the Mac:**
- The bot answers within **~10 minutes**, not instantly, because each run is a short job started every
  10 minutes by cron-job.org (step A9).
- Your Mac is needed **once** (~45 minutes) for logins. After that it can stay off.

---

## Part A — One-time setup (on your Mac)

### A1. Get the code and merge it into `main`
Scheduled GitHub jobs only run from the default branch (`main`). Merge the pull request first,
on github.com or in the GitHub phone app. Then:
```bash
cd ~ && git clone https://github.com/Sarthak11853/Daily-Video-Automation.git
cd Daily-Video-Automation
cp .env.example .env && chmod 600 .env && open -e .env
```

### A2. Gemini key → `.env`
<https://aistudio.google.com/apikey> → **Create API key** → paste after `GEMINI_API_KEY=`.
Don't enable billing on that project.

### A3. Telegram bot → `.env`
1. Telegram → **@BotFather** → `/newbot` → copy the token → `TELEGRAM_BOT_TOKEN=`.
2. Telegram → **@userinfobot** → `/start` → it shows your numeric id → `TELEGRAM_CHAT_ID=`.
3. Open your new bot and press **Start** (bots can't message you first).

### A4. Google (YouTube + Drive) login file
1. <https://console.cloud.google.com> → create a project (no billing).
2. **APIs & Services → Library**: enable **YouTube Data API v3**, **YouTube Analytics API** and
   **Google Drive API**.
3. **OAuth consent screen** (may be called "Google Auth Platform"):
   - External; add an app name and your email.
   - Under **Audience**, add your Google account as a test user, then **Publish app**. If you stay in
     "Testing" mode, Google expires the login every 7 days.
4. **Clients → Create client → Desktop app** → **Download JSON** → save it as
   `secrets/youtube_client_secret.json` inside the project folder.
5. Apply for YouTube's free API audit ("YouTube API Services – Audit and Quota Extension Form").
   Until it's approved, API uploads stay **Private**; the bot warns you, and you can make each one
   Public in YouTube Studio.

### A5. Instagram → `.env`
1. Instagram app → **Settings → Account type and tools → Switch to professional account** (free).
2. <https://developers.facebook.com> → **Create app** → use case **"Manage messaging & content on
   Instagram"**.
3. **Instagram → API setup with Instagram business login → Generate access tokens → Add account**.
   If asked, accept the tester invite in the Instagram app (**Settings → Apps and websites**).
4. Copy the token → `INSTAGRAM_ACCESS_TOKEN=`.
5. Copy the account's numeric id → `INSTAGRAM_USER_ID=`.

### A6. Google Drive inbox (for videos over 20 MB)
In Google Drive, create a folder **DVA Inbox** and open it. Copy the last part of the link
(`drive.google.com/drive/folders/`**`THIS_PART`**) → `DVA_DRIVE_FOLDER_ID=` in `.env`. The setup script also asks for it.

### A7. Make it yours (optional)
Edit `config/settings.yaml`: `channel.name`, `channel.handle`, `channel.default_hashtags`, and the
look of the prompts (`prompt_mode.style_bible`, `mascot_description`). Commit and push, or edit the file
later on github.com (works from the phone too).

### A8. Upload everything to GitHub and start
```bash
./scripts/setup_cloud.sh
```
It installs the GitHub tool, opens two browser logins (GitHub, then Google), uploads your keys as
**GitHub Secrets** (encrypted, never visible in the repo), creates the database encryption key, stops any
bot running on the Mac, and starts the first cloud run.

**Check it worked:** github.com → your repo → **Actions** → the **dva** run should be green. Then send
`/status` to your bot; it should reply within ~10 minutes. Now you can shut the Mac.

### A9. Reliable wake-ups (cron-job.org, free)
GitHub's own scheduler is "best effort" and often skips runs (sometimes only a few per day). So a free
outside scheduler presses **Run workflow** every 10 minutes instead:

1. GitHub → profile photo → **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**:
   - Name: `dva-trigger`
   - Expiration: the longest offered (set a reminder to renew it)
   - Repository access: **Only select repositories** → `Daily-Video-Automation`
   - Permissions → Repository → **Actions: Read and write**
   - Click **Generate token** and copy it (`github_pat_…`)
2. On <https://cron-job.org>, sign up (free) → **Create cronjob**:
   - URL: `https://api.github.com/repos/Sarthak11853/Daily-Video-Automation/actions/workflows/dva.yml/dispatches`
   - Schedule: every 10 minutes
   - Advanced: request method **POST**
   - Headers:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your github_pat token>`
     - `X-GitHub-Api-Version: 2022-11-28`
   - Request body: `{"ref":"main","inputs":{"command":"poll"}}`
   - Save, then **Test run**: a result of **204** means it worked.

---

## Part B — Your daily routine (phone only)

1. **After 9:00 AM** Telegram shows today's video #N:
   - topic
   - verified facts and sources
   - an **easy-English voiceover script** with seconds per scene
   - a **🎵 background-music brief**: genre, mood, tempo, instruments, energy, and words to search in CapCut
   - one message per scene with a **music cue** and **tap-to-copy prompts**
   - a single long prompt and a negative prompt
   - the **caption, hashtags and YouTube tags** that will be posted automatically
2. Make the clips in your AI video app. Then, in an editor like CapCut:
   - put the clips in order
   - record or generate the voiceover from the script
   - add subtitles
   - add one royalty-free track matching the music brief (CapCut → Audio → search the given words), quieter
     than your voice

   Export **vertical 9:16, 1080p**.
3. Send **just the video** to the bot **as a file** (📎 → File, not Gallery, so Telegram doesn't compress it).
   No caption needed: the automatic caption, hashtags and tags are used. Type a caption with the video only if you
   want to replace the automatic one.
   - **Over 20 MB?** Upload it to the **DVA Inbox** folder in the Google Drive app instead. (Tip: 1080p at a
     normal bitrate usually stays under 20 MB for 60 seconds.)
4. Within ~10 minutes the bot replies with the YouTube and Instagram links.

Buttons and commands:
- **🔁 New script & prompts**, **⏭ Skip this topic**
- `/new`: a new topic now
- `/pending`: resend what's waiting
- `/status`
- `/metrics`

Want something done immediately instead of waiting up to 10 minutes? Open the GitHub app → your repo →
**Actions → dva → Run workflow**: `poll` handles your messages now, `run` starts a new topic.

## Part C — Safe first test
Before the first real post, edit `config/settings.yaml` on github.com:
- `prompt_mode.confirm_before_publish: true` (you get a preview with a **Publish** button)
- `publish.youtube.privacy_status: "private"`

After one good test, change them back.

## Part D — Maintenance (rare)

| Situation | What to do |
|---|---|
| Bot says YouTube login failed | On any computer: `./scripts/setup_cloud.sh --reauth` (Google logins expire if you change your password, or after 6 months unused) |
| Instagram token expired (only if the state was reset; normally it renews itself) | Generate a new token (A5) → github.com → Settings → Secrets → Actions → update `INSTAGRAM_ACCESS_TOKEN` |
| GitHub email: "scheduled workflow disabled" | Actions → dva → **Enable workflow** (GitHub pauses schedules in repos with no activity for 60 days; the daily state save normally counts as activity) |
| Change a setting | Edit `config/settings.yaml` on github.com |
| A run failed | Actions tab → open the red run → read the log, or send it to me |

**Security:** keys live only in GitHub Secrets. The database (including the auto-renewed Instagram
token) is stored AES-256-encrypted on the `dva-state` branch. Only scheduled runs and you
(**Run workflow**) can start jobs; pull requests from strangers never receive the secrets.
Keep `secrets/state_key.txt` from your Mac somewhere safe. Without it, the saved state can't be read,
and the system would start with a fresh history.
