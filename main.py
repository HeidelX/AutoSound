"""
main.py — AutoSound

Downloads new audio from a Telegram channel and uploads each track to SoundCloud.
Run daily by GitHub Actions.

Required environment variables — see telegram_downloader.py and soundcloud_uploader.py
for full details:

  TELEGRAM_API_ID
  TELEGRAM_API_HASH
  TELEGRAM_SESSION        (Telethon StringSession)
  TELEGRAM_CHANNEL        (e.g. @mychannel)
  SOUNDCLOUD_CLIENT_ID
  SOUNDCLOUD_CLIENT_SECRET
"""

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from soundcloud_uploader import get_access_token, load_config, upload_track
from telegram_downloader import download_new_audio

load_dotenv()  # no-op in CI where env vars are injected directly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("autosound")


async def run() -> None:
    channel = os.environ["TELEGRAM_CHANNEL"]
    logger.info("=== AutoSound starting (channel: %s) ===", channel)

    tracks = await download_new_audio(channel)

    if not tracks:
        logger.info("No new audio files found. Nothing to upload.")
        return

    config = load_config()
    access_token = get_access_token(config)

    uploaded = 0
    failed = 0
    for track in tracks:
        # Write bytes to a temp file so upload_track can open it
        suffix = Path(track["filename"]).suffix or ".mp3"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(track["file_bytes"])
            tmp_path = Path(tmp.name)

        # title is now parsed from the message (reciter + surah), truncated to 255
        safe_title = track["title"][:255]

        try:
            urn = upload_track(
                mp3_path=tmp_path,
                title=safe_title,
                genre=config["genre"],
                tag_list=f'{config["genre"]} تلاوة قرآن',
                description=track["title"],
                artwork_path=config["artwork_path"] or None,
                access_token=access_token,
                config=config,
            )
            if urn:
                logger.info("  [%d/%d] OK — %s", uploaded + 1, len(tracks), urn)
                uploaded += 1
            else:
                logger.error("  Upload returned no URN for '%s'", track["title"])
                failed += 1
        except Exception as exc:
            logger.error("  Failed to upload '%s': %s", track["title"], exc)
            failed += 1
        finally:
            tmp_path.unlink(missing_ok=True)

    logger.info(
        "=== Done: %d uploaded, %d failed out of %d ===",
        uploaded, failed, len(tracks),
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
