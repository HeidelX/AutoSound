#!/usr/bin/env python3
"""fetch_youtube.py — Download playlists from a YouTube channel as MP3s.

Downloads every playlist from a given YouTube channel URL (or channel handle)
and saves each playlist's audio files under youtube_output/<playlist_title>/.
Skips already-downloaded tracks via yt-dlp's built-in download archive.

Usage:
    python fetch_youtube.py --channel https://www.youtube.com/@SomeChannel
    python fetch_youtube.py --channel @SomeChannel --output-dir my_output
    python fetch_youtube.py --channel URL --playlist "Playlist Title" --workers 4
    python fetch_youtube.py --channel URL --cookies cookies.txt
    python fetch_youtube.py --channel URL --dry-run
    python fetch_youtube.py --channel URL --no-archive  # re-download everything

Required:
    yt-dlp  (pip install yt-dlp)

After running this script, run soundcloud_uploader.py to upload the results.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

try:
    import yt_dlp
except ImportError:
    print("ERROR: yt-dlp is not installed. Run: pip install yt-dlp")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_folder(name: str) -> str:
    """Turn a playlist title into a safe directory name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "playlist"


def _resolve_channel_url(channel: str) -> str:
    """Accept @handle, /c/name, /channel/ID, or a full URL."""
    if channel.startswith("http"):
        return channel
    if channel.startswith("@"):
        return f"https://www.youtube.com/{channel}"
    # bare handle without @
    return f"https://www.youtube.com/@{channel}"


def _fetch_playlist_entries(channel_url: str, cookies: str | None,
                             cookies_from_browser: str | None,
                             quiet: bool) -> list[dict]:
    """Return a list of {title, url} for every playlist in the channel."""
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": quiet,
        "no_warnings": quiet,
        "ignoreerrors": True,
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url + "/playlists", download=False)

    if not info or "entries" not in info:
        return []

    playlists = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        url = entry.get("url") or entry.get("webpage_url", "")
        title = entry.get("title") or entry.get("id") or "unknown"
        if url:
            playlists.append({"title": title, "url": url})
    return playlists


def _download_playlist(playlist_url: str, folder: Path, archive_file: Path | None,
                        cookies: str | None, cookies_from_browser: str | None,
                        workers: int, quiet: bool,
                        audio_format: str, audio_quality: str) -> tuple[int, int]:
    """Download a playlist's audio into folder. Returns (downloaded, skipped)."""
    folder.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(folder / "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": audio_quality,
        }],
        "quiet": quiet,
        "no_warnings": quiet,
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": workers,
        "sleep_interval": 1,
        "max_sleep_interval": 5,
        "noprogress": False,
        "newline": True,
        "writeinfojson": False,
        "writethumbnail": False,
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        "remote_components": ["ejs:github"],
    }

    if archive_file is not None:
        ydl_opts["download_archive"] = str(archive_file)

    if cookies:
        ydl_opts["cookiefile"] = cookies
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    class _Counter:
        downloaded = 0
        skipped = 0

    def _progress_hook(d):
        if d["status"] == "finished":
            _Counter.downloaded += 1
        elif d["status"] == "already_downloaded":
            _Counter.skipped += 1

    ydl_opts["progress_hooks"] = [_progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([playlist_url])

    return _Counter.downloaded, _Counter.skipped


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> None:
    load_dotenv()

    channel_url = _resolve_channel_url(args.channel)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cookies = args.cookies or os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
    cookies_from_browser = getattr(args, "cookies_from_browser", None) or os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip() or None
    quiet = args.quiet

    print(f"Channel : {channel_url}")
    print(f"Output  : {output_dir.resolve()}")
    if args.dry_run:
        print("DRY RUN — listing playlists only, nothing will be downloaded.\n")

    # Discover playlists
    print("Fetching playlist list...")
    playlists = _fetch_playlist_entries(channel_url, cookies, cookies_from_browser, quiet)

    if not playlists:
        print("No playlists found. Check the channel URL or try --cookies.")
        sys.exit(1)

    # Filter to a single playlist if requested
    if args.playlist:
        playlists = [p for p in playlists if p["title"] == args.playlist]
        if not playlists:
            print(f"Playlist '{args.playlist}' not found on this channel.")
            print("Available playlists:")
            all_playlists = _fetch_playlist_entries(channel_url, cookies, cookies_from_browser, quiet)
            for p in all_playlists:
                print(f"  {p['title']}")
            sys.exit(1)

    print(f"Found {len(playlists)} playlist(s).\n")

    total_downloaded = 0
    total_skipped = 0

    for i, playlist in enumerate(playlists, 1):
        folder_name = _sanitize_folder(playlist["title"])
        folder = output_dir / folder_name

        print(f"[{i}/{len(playlists)}] {playlist['title']}")
        print(f"  -> {folder}")

        if args.dry_run:
            print("  (dry-run, skipping)\n")
            continue

        archive_file = None
        if not args.no_archive:
            archive_file = folder / ".yt_download_archive"

        downloaded, skipped = _download_playlist(
            playlist_url=playlist["url"],
            folder=folder,
            archive_file=archive_file,
            cookies=cookies,
            cookies_from_browser=cookies_from_browser,
            workers=args.workers,
            quiet=quiet,
            audio_format=args.format,
            audio_quality=args.quality,
        )
        total_downloaded += downloaded
        total_skipped += skipped

        mp3_count = len(list(folder.glob(f"*.{args.format}")))
        print(f"  Downloaded: {downloaded}, Skipped: {skipped}, "
              f"Total in folder: {mp3_count}\n")

        if i < len(playlists):
            time.sleep(2)  # polite pause between playlists

    if not args.dry_run:
        print("=" * 60)
        print(f"Done! Downloaded: {total_downloaded}, Skipped: {total_skipped}")
        print(f"Files saved to: {output_dir.resolve()}/")
        print("\nRun soundcloud_uploader.py to upload to SoundCloud.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all playlists from a YouTube channel as MP3s.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fetch_youtube.py --channel @SomeChannel
  python fetch_youtube.py --channel https://www.youtube.com/@SomeChannel --output-dir out
  python fetch_youtube.py --channel @SomeChannel --playlist "My Playlist" --workers 4
  python fetch_youtube.py --channel @SomeChannel --cookies cookies.txt
  python fetch_youtube.py --channel @SomeChannel --dry-run
  python fetch_youtube.py --channel @SomeChannel --format mp3 --quality 192
        """,
    )

    parser.add_argument(
        "--channel", required=True,
        help="YouTube channel URL, handle (@name), or /c/name path.",
    )
    parser.add_argument(
        "--output-dir", default="youtube_output",
        help="Root directory for downloaded playlists (default: youtube_output).",
    )
    parser.add_argument(
        "--playlist", default=None,
        help="Download only this playlist by exact title.",
    )
    parser.add_argument(
        "--cookies", default=None,
        help="Path to a Netscape-format cookies file (for age-restricted videos).",
    )
    parser.add_argument(
        "--cookies-from-browser", default=None,
        dest="cookies_from_browser",
        help="Browser to extract cookies from (e.g. chrome, firefox, safari).",
    )
    parser.add_argument(
        "--workers", type=int, default=3,
        help="Number of concurrent fragment downloads per playlist (default: 3).",
    )
    parser.add_argument(
        "--format", default="mp3", choices=["mp3", "m4a", "opus", "flac"],
        help="Audio format to extract (default: mp3).",
    )
    parser.add_argument(
        "--quality", default="192",
        help="Audio bitrate / quality (default: 192 for mp3, 0 for opus/flac).",
    )
    parser.add_argument(
        "--no-archive", action="store_true",
        help="Disable the download archive — re-download everything.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List playlists without downloading anything.",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress yt-dlp progress output.",
    )

    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
