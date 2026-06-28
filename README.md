# AutoSound

Downloads new audio files from a private/public Telegram channel daily and uploads them to SoundCloud as public tracks. Runs entirely on GitHub Actions — no server needed.

---

## How it works

```
GitHub Actions (daily cron)
  └─ main.py
       ├─ telegram_downloader.py  — Telethon MTProto, incremental (state.json)
       └─ soundcloud_uploader.py  — SoundCloud REST API
```

`state.json` (committed to the repo) remembers the last processed Telegram message ID so each run only fetches new audio.

---

## Setup

### 1. Fork / clone this repository

### 2. Get Telegram API credentials

1. Go to <https://my.telegram.org/apps> and create an app.
2. Note your **API ID** and **API Hash**.

### 3. Generate a Telethon StringSession

Run this once locally:

```bash
pip install telethon
python - <<'EOF'
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import base64, os

api_id   = int(input("API ID: "))
api_hash = input("API Hash: ")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session = client.session.save()
    b64 = base64.b64encode(session.encode()).decode()
    print("\nTELEGRAM_SESSION (base64):\n", b64)
EOF
```

This logs you in interactively (phone number + OTP). The printed base64 value is your `TELEGRAM_SESSION` secret. **Keep it secret — it is equivalent to your account login.**

### 4. Get a SoundCloud OAuth token

**Option A (easiest) — legacy token from SoundCloud for Developers:**

If you have developer access, obtain an OAuth token from your app's dashboard and set `SOUNDCLOUD_OAUTH_TOKEN`.

**Option B — password credentials flow:**

Set `SOUNDCLOUD_CLIENT_ID`, `SOUNDCLOUD_CLIENT_SECRET`, `SOUNDCLOUD_USERNAME`, `SOUNDCLOUD_PASSWORD` and uncomment those lines in `.github/workflows/autosound.yml`.

### 5. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `TELEGRAM_API_ID` | numeric API ID |
| `TELEGRAM_API_HASH` | API hash string |
| `TELEGRAM_SESSION` | base64 StringSession from step 3 |
| `TELEGRAM_CHANNEL` | e.g. `@mychannel` or the invite link |
| `SOUNDCLOUD_OAUTH_TOKEN` | your SoundCloud OAuth token |

### 6. Push to GitHub

The workflow runs automatically at **06:00 UTC every day**. You can also trigger it manually from the **Actions** tab.

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a .env file with your secrets (never commit this)
cp .env.example .env
# edit .env …

python main.py
```

### .env.example

```
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
TELEGRAM_SESSION=<base64 StringSession>
TELEGRAM_CHANNEL=@mychannel
SOUNDCLOUD_OAUTH_TOKEN=<token>
```

---

## File structure

```
.
├── main.py                        # Entry point
├── telegram_downloader.py         # Telethon downloader
├── soundcloud_uploader.py         # SoundCloud uploader
├── requirements.txt
├── state.json                     # Persists last message ID (committed by CI)
├── .gitignore
└── .github/
    └── workflows/
        └── autosound.yml          # Daily GitHub Actions workflow
```
