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


import re
import unicodedata

# Prayer keywords (Arabic) mapped to short English labels
_PRAYER_MAP = {
    "الفجر": "Fajr", "فجر": "Fajr",
    "الظهر": "Dhuhr", "ظهر": "Dhuhr",
    "العصر": "Asr", "عصر": "Asr",
    "المغرب": "Maghrib", "مغرب": "Maghrib",
    "العشاء": "Isha", "عشاء": "Isha",
    "الجمعة": "Jumuah", "جمعة": "Jumuah",
    "الجمع": "Jumuah",
    "التراويح": "Tarawih", "تراويح": "Tarawih",
    "التهجد": "Tahajjud", "تهجد": "Tahajjud",
    "القيام": "Qiyam", "قيام": "Qiyam",
    "الوتر": "Witr", "وتر": "Witr",
}

# Regex to extract Surah names after common Arabic connectors
# Also handles bare "سورة X" without a preceding "من"
_SURAH_AFTER = re.compile(
    r'(?:(?:من\s+)?سور(?:ة|تي|ت)\s*|تلاوة\s+(?:من\s+)?سور(?:ة|تي|ت)\s*|لسور(?:ة|تي|ت)\s*)'
    r'(.+)',
    re.UNICODE,
)

# Noise suffixes to strip from the info line (URLs, app promo, sharing text)
_NOISE = re.compile(
    r'\s*https?://\S+|\s*تمت المشاركة.*|'
    r'\s*\|\s*$|\s*\.\s*$',
    re.UNICODE,
)

# Unicode directional / invisible marks that appear in the raw text
_INVISIBLE = re.compile(r'[\u200f\u200e\u200b\u2069\u2068\u202c\u202d\u202e]')


def _clean(s: str) -> str:
    """Strip invisible marks and extra whitespace."""
    s = _INVISIBLE.sub("", s).strip()
    return re.sub(r'\s{2,}', ' ', s)  # collapse multiple spaces


def _extract_reciter(lines: list[str]) -> str | None:
    """Return reciter name from 'للشيخ/للشيخة X' pattern."""
    for line in lines:
        m = re.search(r'للشيخ[ة]?\s+(.+)', line)
        if m:
            name = _clean(m.group(1))
            return re.sub(r'_', ' ', re.sub(r'#', '', name)).strip()
    return None


def _extract_prayer(text: str) -> str | None:
    """Return short prayer label if a known prayer keyword is found."""
    text = _clean(text)  # strip invisible chars before matching
    for ar, en in _PRAYER_MAP.items():
        if ar in text:
            return en
    return None


def _extract_surahs(info_line: str) -> str | None:
    """
    Try to pull just the Surah name(s) from the info line.
    Returns only the Arabic surah name(s), stopping before any English/date/URL.
    """
    m = _SURAH_AFTER.search(info_line)
    if not m:
        return None
    raw = m.group(1)
    # Cut at: URL, pipe, slash, date pattern, or first run of Latin characters
    raw = re.split(
        r'\s*https?://|'           # URL
        r'\s*\|\s*|'               # pipe separator
        r'\s*/\s*|'                # slash separator
        r'\s*\d{1,2}[-/]\d{1,2}[-/]\d{3,4}|'  # date dd-mm-yyyy (3+ digit year)
        r'\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}|'    # date yyyy-mm-dd
        r'\s*\d{1,2}\s+(?:محرم|صفر|ربيع|جمادى|رجب|شعبان|رمضان|شوال|ذو)|'  # hijri date
        r'(?<=[^\x00-\x7F])\s+[A-Za-z]',      # Arabic then space then Latin
        raw
    )[0]
    raw = _NOISE.sub("", raw)
    # Strip trailing punctuation / spaces
    raw = re.sub(r'[\s\.,،؛:]+$', '', raw)
    return _clean(raw) or None


def _extract_info_line(lines: list[str], reciter_line_idx: int) -> str | None:
    """Return the first non-empty, non-URL line after the reciter line."""
    for line in lines[reciter_line_idx + 1:]:
        if not line.startswith("http"):
            return _clean(line)
    return None


def _track_title(message, filename: str) -> str:
    """Build title: <Surah> - <Prayer AR> - <Reciter> - <Hijri date>"""
    text = (getattr(message, "message", "") or "").strip()
    if not text:
        stem = Path(filename).stem
        return re.sub(r'^#+', '', stem).strip()[:200]

    lines = [_clean(l) for l in text.splitlines() if _clean(l)]

    reciter = _extract_reciter(lines)
    reciter_idx = 0
    if reciter:
        for i, l in enumerate(lines):
            if re.search(r'للشيخ[ة]?', l):
                reciter_idx = i
                break

    info = _extract_info_line(lines, reciter_idx)
    prayer_ar = _extract_prayer_ar(info) if info else None
    surahs = _extract_surahs(info) if info else None
    hijri = _extract_hijri_date(text)

    parts = []
    if surahs:
        parts.append(surahs)
    if prayer_ar:
        parts.append(prayer_ar)
    if reciter:
        parts.append(reciter)
    if hijri:
        parts.append(hijri)

    if parts:
        return " - ".join(parts)[:200]

    stem = Path(filename).stem
    return re.sub(r'^#+', '', stem).strip()[:200]


def _extract_hijri_date(text: str) -> str | None:
    m = re.search(r'(\d{1,2}-\d{1,2}-\d{4}هـ)', text)
    return m.group(1) if m else None


def _extract_prayer_ar(text: str) -> str | None:
    text = _clean(text)
    for ar in _PRAYER_MAP:
        if ar in text:
            return ar
    return None


async def iter_new_audio(
    client: TelegramClient, channel: str, min_id: int, limit: int | None = None
) -> AsyncIterator[tuple]:
    """
    Yield (message_id, file_bytes, filename, title) for each audio message
    with id > min_id, in ascending order.

    limit: max messages to scan (None = unlimited, useful for CI daily runs).
    """
    messages = []
    async for msg in client.iter_messages(channel, min_id=min_id, reverse=True, limit=limit):
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

    Reads TELEGRAM_FETCH_LIMIT from the environment to cap the number of
    messages scanned per run (useful for local testing). Defaults to unlimited.
    """
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_str = os.environ.get("TELEGRAM_SESSION", "").strip()

    if session_str:
        # CI / GitHub Actions: StringSession string injected via env var
        session = StringSession(session_str)
    else:
        # Local: use a persistent file session; Telethon handles login on first run
        session = str(Path(__file__).parent / "autosound")

    fetch_limit_env = os.environ.get("TELEGRAM_FETCH_LIMIT", "").strip()
    fetch_limit: int | None = int(fetch_limit_env) if fetch_limit_env else None

    state = _load_state()
    min_id = state["last_message_id"]
    logger.info("Fetching messages from channel %s with id > %d (limit=%s)",
                channel, min_id, fetch_limit or "unlimited")

    results = []
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Re-generate TELEGRAM_SESSION.")
        async for msg_id, file_bytes, filename, title in iter_new_audio(
            client, channel, min_id, limit=fetch_limit
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
    finally:
        await client.disconnect()

    logger.info("Downloaded %d new audio file(s)", len(results))
    return results


async def iter_last_audio(
    client: TelegramClient, channel: str, n: int, skip_ids: set[int] | None = None
) -> AsyncIterator[tuple]:
    """Yield (message_id, file_bytes, filename, title) for audio among the last n messages."""
    messages = []
    async for msg in client.iter_messages(channel, limit=n):
        if not isinstance(msg.media, MessageMediaDocument):
            continue
        doc = msg.media.document
        if not _is_audio(doc):
            continue
        if skip_ids and msg.id in skip_ids:
            logger.info("Skipping already-downloaded message %d", msg.id)
            continue
        messages.append(msg)

    for msg in reversed(messages):
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


async def download_last_audio(channel: str, n: int) -> list[dict]:
    """Download audio from the last n messages, skipping already-downloaded IDs."""
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_str = os.environ.get("TELEGRAM_SESSION", "").strip()

    session = StringSession(session_str) if session_str else str(Path(__file__).parent / "autosound")

    logger.info("Fetching audio from last %d messages in channel %s", n, channel)

    state = _load_state()
    skip_ids = set(state.get("downloaded_ids", []))
    results = []
    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Re-generate TELEGRAM_SESSION.")
        async for msg_id, file_bytes, filename, title in iter_last_audio(client, channel, n, skip_ids=skip_ids):
            results.append({"message_id": msg_id, "file_bytes": file_bytes, "filename": filename, "title": title})
            state["last_message_id"] = max(state.get("last_message_id", 0), msg_id)
            state.setdefault("downloaded_ids", [])
            if msg_id not in skip_ids:
                state["downloaded_ids"].append(msg_id)
                skip_ids.add(msg_id)
            _save_state(state)
    finally:
        await client.disconnect()

    logger.info("Downloaded %d audio file(s)", len(results))
    return results
