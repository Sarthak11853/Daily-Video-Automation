# Privacy Policy — Facts Robot

_Last updated: 19 November 2026_

Facts Robot is a personal automation tool operated by one creator for their **own** YouTube channel and
Instagram account. It has no other users. This page explains what data it accesses and why.

## Data it accesses

| Service | What is accessed | Why |
|---|---|---|
| YouTube (Google) | Permission to upload videos; read the channel's own videos and their statistics (views, likes, comments, shares, watch time) | To publish the creator's videos and learn which topics perform best |
| Google Drive | Read-only access to files in **one folder** the creator chooses | To receive finished videos that are too large to send via Telegram |
| Instagram (Meta) | Permission to publish Reels and read insights for the creator's own account | To publish the creator's videos and read their performance |
| Telegram | Messages and files the creator sends to their own bot | To receive the finished video and commands |
| Google Gemini | Public facts and text used to write scripts and captions | To research and write content (no personal data is sent) |

## How data is stored

- Access keys are stored in the creator's private GitHub Actions Secrets.
- The tool's database (video history, publishing status, statistics) is stored **encrypted (AES-256)** in the
  creator's own GitHub repository and is readable only with a key held by the creator.
- Downloaded videos are deleted after upload.

## Sharing

Data is **not sold, shared or transferred** to anyone. It is used only to publish and measure the creator's
own content.

Facts Robot's use and transfer of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Removing access

The creator can revoke access at any time:

- Google: <https://myaccount.google.com/permissions>
- Instagram: Instagram app → Settings → Apps and websites
- Telegram: block the bot

## Contact

Questions: open an issue at <https://github.com/Sarthak11853/Daily-Video-Automation/issues>.
