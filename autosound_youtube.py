#!/usr/bin/env python3
"""autosound_youtube.py — Download YouTube channel playlists and upload to SoundCloud.

One command to run the full pipeline:
  1. fetch_youtube.py  — download playlists from a YouTube channel as MP3s
  2. soundcloud_uploader.py — upload the MP3 folders to SoundCloud as albums

Usage:
    python autosound_youtube.py --channel @SomeChannel
    python autosound_youtube.py --channel @SomeChannel --playlist "My Playlist"
    python autosound_youtube.py --channel @SomeChannel --skip-download
    python autosound_youtube.py --channel @SomeChannel --skip-upload
    python autosound_youtube.py --channel @SomeChannel --dry-run

Run  python fetch_youtube.py --help  or  python soundcloud_uploader.py --help
for the full option list of each individual step.
"""

import argparse
import sys

from fetch_youtube import main as _yt_main
from soundcloud_uploader import load_config, get_access_token, upload_all


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download YouTube channel playlists and upload to SoundCloud.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline — download then upload
  python autosound_youtube.py --channel @SomeChannel

  # Only download (no SoundCloud upload)
  python autosound_youtube.py --channel @SomeChannel --skip-upload

  # Only upload what's already downloaded
  python autosound_youtube.py --channel @SomeChannel --skip-download

  # Single playlist, verbose, 5 concurrent upload workers
  python autosound_youtube.py --channel @SomeChannel \\
      --playlist "My Playlist" --upload-workers 5

  # Dry-run both steps (nothing is downloaded or uploaded)
  python autosound_youtube.py --channel @SomeChannel --dry-run
        """,
    )

    # ---- YouTube download options ----------------------------------------
    yt = parser.add_argument_group("YouTube download options")
    yt.add_argument(
        "--channel", required=True,
        help="YouTube channel URL, @handle, or /c/name.",
    )
    yt.add_argument(
        "--playlist", default=None,
        help="Process only this playlist (exact title). Applies to both steps.",
    )
    yt.add_argument(
        "--output-dir", default="youtube_output",
        help="Directory for downloaded audio (default: youtube_output).",
    )
    yt.add_argument(
        "--cookies", default=None,
        help="Netscape cookies file for age-restricted videos.",
    )
    yt.add_argument(
        "--cookies-from-browser", default=None,
        dest="cookies_from_browser",
        help="Browser to extract cookies from (e.g. chrome, firefox, safari).",
    )
    yt.add_argument(
        "--yt-workers", type=int, default=3,
        help="Concurrent fragment downloads per playlist (default: 3).",
    )
    yt.add_argument(
        "--format", default="mp3",
        choices=["mp3", "m4a", "opus", "flac"],
        help="Audio format to extract (default: mp3).",
    )
    yt.add_argument(
        "--quality", default="192",
        help="Audio bitrate (default: 192).",
    )
    yt.add_argument(
        "--no-archive", action="store_true",
        help="Disable yt-dlp download archive (re-download everything).",
    )

    # ---- SoundCloud upload options ----------------------------------------
    sc = parser.add_argument_group("SoundCloud upload options")
    sc.add_argument(
        "--artwork", default=None,
        help="Path to artwork image file (overrides .env SOUNDCLOUD_ARTWORK_PATH).",
    )
    sc.add_argument(
        "--upload-workers", type=int, default=3,
        help="Concurrent SoundCloud track uploads (default: 3).",
    )

    # ---- Pipeline control ------------------------------------------------
    pipe = parser.add_argument_group("Pipeline control")
    pipe.add_argument(
        "--skip-download", action="store_true",
        help="Skip the YouTube download step.",
    )
    pipe.add_argument(
        "--skip-upload", action="store_true",
        help="Skip the SoundCloud upload step.",
    )
    pipe.add_argument(
        "--dry-run", action="store_true",
        help="Dry-run both steps (list what would happen, no downloads/uploads).",
    )
    pipe.add_argument(
        "--quiet", action="store_true",
        help="Suppress yt-dlp progress output.",
    )

    args = parser.parse_args()

    # ── Step 1: Download ───────────────────────────────────────────────────
    if not args.skip_download:
        print("\n" + "=" * 60)
        print("STEP 1 — Download playlists from YouTube")
        print("=" * 60 + "\n")

        # Build sys.argv for fetch_youtube's argparse, then call it
        yt_argv = [
            "--channel", args.channel,
            "--output-dir", args.output_dir,
            "--workers", str(args.yt_workers),
            "--format", args.format,
            "--quality", args.quality,
        ]
        if args.playlist:
            yt_argv += ["--playlist", args.playlist]
        if args.cookies:
            yt_argv += ["--cookies", args.cookies]
        if args.cookies_from_browser:
            yt_argv += ["--cookies-from-browser", args.cookies_from_browser]
        if args.no_archive:
            yt_argv.append("--no-archive")
        if args.dry_run:
            yt_argv.append("--dry-run")
        if args.quiet:
            yt_argv.append("--quiet")

        import sys as _sys
        old_argv, _sys.argv = _sys.argv, ["fetch_youtube.py"] + yt_argv
        try:
            _yt_main()
        except SystemExit as e:
            if e.code and e.code != 0:
                print(f"\nDownload step exited with code {e.code}. Aborting.")
                sys.exit(e.code)
        finally:
            _sys.argv = old_argv
    else:
        print("\nSkipping YouTube download step.\n")

    # ── Step 2: Upload ─────────────────────────────────────────────────────
    if not args.skip_upload:
        print("\n" + "=" * 60)
        print("STEP 2 — Upload to SoundCloud")
        print("=" * 60 + "\n")

        config = load_config()
        config["output_dir"] = args.output_dir
        config["workers"] = args.upload_workers

        if args.artwork:
            from pathlib import Path
            path = str(Path(args.artwork).resolve())
            if not Path(path).exists():
                print(f"Artwork file not found: {args.artwork}")
                sys.exit(1)
            config["artwork_path"] = path

        upload_all(config, dry_run=args.dry_run, playlist_filter=args.playlist)
    else:
        print("\nSkipping SoundCloud upload step.\n")


if __name__ == "__main__":
    main()
