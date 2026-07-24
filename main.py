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

Optional flags:
  --download-only         Download to --staging-dir without uploading to SoundCloud.
  --staging-dir PATH      Directory to save files when using --download-only.
                          Defaults to telegram_staging/ next to this script.
  --last N                Download audio from the last N messages (ignores state watermark).
"""

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from soundcloud_uploader import get_access_token, load_config, upload_track
from telegram_downloader import download_last_audio, download_new_audio

load_dotenv()  # no-op in CI where env vars are injected directly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("autosound")


async def run(download_only: bool = False, staging_dir: Path | None = None, last: int | None = None) -> None:
    channel = os.environ["TELEGRAM_CHANNEL"]
    logger.info("=== AutoSound starting (channel: %s) ===", channel)

    if last is not None:
        tracks = await download_last_audio(channel, last)
    else:
        tracks = await download_new_audio(channel)

    if not tracks:
        logger.info("No new audio files found. Nothing to upload.")
        return

    if download_only:
        staging_dir = staging_dir or (Path(__file__).parent / "telegram_staging")
        staging_dir.mkdir(parents=True, exist_ok=True)
        for track in tracks:
            dest = staging_dir / track["filename"]
            dest.write_bytes(track["file_bytes"])
            logger.info("Saved: %s", dest)
        logger.info("=== Download-only: %d file(s) saved to %s ===", len(tracks), staging_dir)
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
                filename=track["filename"],
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
    parser = argparse.ArgumentParser(description="AutoSound — Telegram → SoundCloud")
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Download audio to --staging-dir without uploading to SoundCloud",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=None,
        help="Directory for downloaded files when using --download-only "
             "(default: telegram_staging/ next to this script)",
    )
    parser.add_argument(
        "--last",
        type=int,
        metavar="N",
        default=None,
        help="Download audio from the last N messages (ignores state watermark)",
    )
    args = parser.parse_args()
    asyncio.run(run(download_only=args.download_only, staging_dir=args.staging_dir, last=args.last))
