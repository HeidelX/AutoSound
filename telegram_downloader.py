"""
telegram_downloader.py

Downloads new audio files from a Telegram channel (MTProto / Telethon).
Reads/writes state.json to track the last processed message ID so each
run only fetches messages newer than the previous run.

Required environment variables:
  TELEGRAM_API_ID      — from https://my.telegram.org/apps
  TELEGRAM_API_HASH    — from https://my.telegram.org/apps
  TELEGRAM_CHANNEL     — channel username or invite link, e.g. @mychannel

Optional:
  TELEGRAM_SESSION     — base64-encoded Telethon StringSession (for CI/GitHub Actions).
                         If not set, a local file session ("autosound.session") is used
                         instead — Telethon will prompt for phone + OTP on first run
                         and persist the session automatically.
"""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    MessageMediaDocument,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "state.json"
AUDIO_MIME_PREFIXES = ("audio/",)
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a", ".opus"}


def _load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"last_message_id": 0}


def _save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)
    logger.info("State saved: last_message_id=%d", state["last_message_id"])


def _is_audio(document) -> bool:
    """Return True if a Telegram document looks like an audio file."""
    if document is None:
        return False
    mime = getattr(document, "mime_type", "") or ""
    if any(mime.startswith(p) for p in AUDIO_MIME_PREFIXES):
        return True
    # Fallback: check file name extension
    for attr in getattr(document, "attributes", []):
        if hasattr(attr, "file_name"):
            ext = Path(attr.file_name).suffix.lower()
            if ext in AUDIO_EXTENSIONS:
                return True
        if isinstance(attr, DocumentAttributeAudio):
            return True
    return False


def _filename_for(document, message_id: int) -> str:
    """Derive a safe filename for the document."""
    for attr in getattr(document, "attributes", []):
        if hasattr(attr, "file_name") and attr.file_name:
            return attr.file_name
    ext = ".mp3"
    mime = getattr(document, "mime_type", "")
    if mime:
        ext = "." + mime.split("/")[-1].split(";")[0].strip()
    return f"audio_{message_id}{ext}"


def _track_title(message, filename: str) -> str:
    """
    Parse reciter and surah from the message text.

    Typical message format:
        تلاوةٌ للشيخ <Reciter Name>
        صلاة <Prayer> من <Surah info>
        https://...

    Returns "<Reciter> - <Surah line>" if parseable, else filename stem.
    """
    import re
    text = (getattr(message, "message", "") or "").strip()
    if not text:
        return Path(filename).stem

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    reciter = None
    surah = None

    for line in lines:
        # Match "للشيخ <Name>" or "للشيخة <Name>"
        if reciter is None:
            m = re.search(r'للشيخ[ة]?\s+(.+)', line)
            if m:
                reciter = m.group(1).strip()
                continue
        # Second meaningful line (not a URL) is usually the surah/prayer info
        if surah is None and not line.startswith("http") and line != lines[0]:
            surah = line[:100]

    if reciter and surah:
        return f"{reciter} - {surah}"
    if reciter:
        return reciter
    # Fall back to first non-URL line
    for line in lines:
        if not line.startswith("http"):
            return line[:150]
    return Path(filename).stem


async def iter_new_audio(
    client: TelegramClient, channel: str, min_id: int
) -> AsyncIterator[tuple]:
    """
    Yield (message_id, file_bytes, filename, title) for each audio message
    with id > min_id, in ascending order.
    """
    messages = []
    async for msg in client.iter_messages(channel, min_id=min_id, reverse=True, limit=10):
        if not isinstance(msg.media, MessageMediaDocument):
            continue
        doc = msg.media.document
        if not _is_audio(doc):
            continue
        messages.append(msg)

    for msg in messages:
        doc = msg.media.document
        filename = _filename_for(doc, msg.id)
        title = _track_title(msg, filename)
        logger.info("Downloading message %d — %s", msg.id, filename)
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await client.download_media(msg, file=str(tmp_path))
        file_bytes = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
        yield msg.id, file_bytes, filename, title


async def download_new_audio(channel: str) -> list[dict]:
    """
    Connect to Telegram, download all new audio since last run.
    Returns list of dicts with keys: message_id, file_bytes, filename, title.
    """
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_b64 = os.environ.get("TELEGRAM_SESSION", "").strip()

    if session_b64:
        # CI / GitHub Actions: use StringSession from env var
        session = StringSession(session_b64)
    else:
        # Local: use a persistent file session; Telethon handles login on first run
        session = str(Path(__file__).parent / "autosound")

    state = _load_state()
    min_id = state["last_message_id"]
    logger.info("Fetching messages from channel %s with id > %d", channel, min_id)

    results = []
    async with TelegramClient(session, api_id, api_hash) as client:
        async for msg_id, file_bytes, filename, title in iter_new_audio(
            client, channel, min_id
        ):
            results.append(
                {
                    "message_id": msg_id,
                    "file_bytes": file_bytes,
                    "filename": filename,
                    "title": title,
                }
            )
            # Update state incrementally so a crash mid-run saves partial progress
            state["last_message_id"] = max(state["last_message_id"], msg_id)
            _save_state(state)

    logger.info("Downloaded %d new audio file(s)", len(results))
    return results
