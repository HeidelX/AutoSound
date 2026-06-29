#!/usr/bin/env python3
"""Upload downloaded MP3 playlists to SoundCloud as albums.

Scans youtube_output/ for playlist folders and uploads each folder's MP3 files
as tracks, then groups them into a SoundCloud album. Supports incremental
uploads via an archive file to avoid duplicates.

Requires a registered SoundCloud app (https://soundcloud.com/you/apps) with
OAuth 2.1 Authorization Code + PKCE flow.
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOKEN_FILE = ".soundcloud_token.json"  # stored at repo root, committed by CI
UPLOAD_ARCHIVE = ".soundcloud_upload_archive.txt"
ALBUM_ARCHIVE = ".soundcloud_album_archive.txt"
MAX_ALBUM_TRACKS = 500

SC_AUTH_URL = "https://secure.soundcloud.com/authorize"
SC_TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
SC_API = "https://api.soundcloud.com"

# Rate-limit safety: seconds between track uploads
UPLOAD_SLEEP = 2
MAX_RETRIES = 5
DEFAULT_WORKERS = 3

_archive_lock = threading.Lock()


def load_config() -> dict:
    load_dotenv()
    return {
        "client_id": os.getenv("SOUNDCLOUD_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("SOUNDCLOUD_CLIENT_SECRET", "").strip(),
        "redirect_uri": os.getenv("SOUNDCLOUD_REDIRECT_URI",
                                  "http://localhost:8765/callback").strip(),
        "artwork_path": os.getenv("SOUNDCLOUD_ARTWORK_PATH", "").strip(),
        "genre": os.getenv("SOUNDCLOUD_GENRE", "Quran").strip(),
        "output_dir": os.getenv("YOUTUBE_OUTPUT_DIR", "youtube_output").strip(),
    }


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# OAuth 2.1 Authorization Code flow
# ---------------------------------------------------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Tiny handler that captures the authorization code from the redirect."""

    code: str | None = None
    state: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        _CallbackHandler.code = params.get("code", [None])[0]
        _CallbackHandler.state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorization successful!</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, format, *args):
        pass  # suppress noisy HTTP logs


def authorize(config: dict) -> dict:
    """Run the full OAuth 2.1 Authorization Code + PKCE flow.

    Opens the user's browser, waits for the callback, exchanges the code for
    tokens, and returns the token dict.
    """
    client_id = config["client_id"]
    client_secret = config["client_secret"]
    redirect_uri = config["redirect_uri"]

    if not client_id or not client_secret:
        print("ERROR: SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET must "
              "be set in your .env file.")
        print("Register your app at https://soundcloud.com/you/apps")
        sys.exit(1)

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    # Build authorization URL
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    auth_url = f"{SC_AUTH_URL}?{params}"

    # Parse redirect URI to get the port
    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    port = parsed_redirect.port or 8765

    # Start local server
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 120  # 2 minutes to complete auth

    print(f"Opening browser for SoundCloud authorization...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Wait for callback
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    while _CallbackHandler.code is None:
        server.handle_request()

    server.server_close()

    if _CallbackHandler.state != state:
        print("ERROR: State mismatch — possible CSRF attack.")
        sys.exit(1)

    code = _CallbackHandler.code
    print("Authorization code received. Exchanging for tokens...")

    # Exchange code for tokens
    resp = requests.post(SC_TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "code": code,
    }, headers={"Accept": "application/json; charset=utf-8"}, timeout=30)
    resp.raise_for_status()

    token_data = resp.json()
    token_data["obtained_at"] = time.time()
    _save_tokens(token_data, config)
    print("Tokens saved successfully.\n")
    return token_data


def _token_path() -> Path:
    """Token file lives at the repo root (next to main.py), not in output_dir."""
    return Path(__file__).parent / TOKEN_FILE


def _save_tokens(token_data: dict, config: dict) -> None:
    path = _token_path()
    path.write_text(json.dumps(token_data, indent=2), encoding="utf-8")


def _load_tokens(config: dict) -> dict | None:
    path = _token_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _refresh_token(config: dict, token_data: dict) -> dict:
    """Use the refresh token to obtain new access + refresh tokens."""
    resp = requests.post(SC_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": token_data["refresh_token"],
    }, headers={"Accept": "application/json; charset=utf-8"}, timeout=30)
    resp.raise_for_status()

    new_data = resp.json()
    new_data["obtained_at"] = time.time()
    _save_tokens(new_data, config)
    return new_data


def get_access_token(config: dict) -> str:
    """Return a valid access token, refreshing via refresh_token if expired.

    The token file (.soundcloud_token.json) lives at the repo root and is
    committed back to the repo by CI after each run, so the refresh_token
    stays valid across daily runs.
    """
    token_data = _load_tokens(config)
    if token_data is None:
        print("No saved tokens found. Running authorization flow...")
        token_data = authorize(config)

    expires_in = token_data.get("expires_in", 3600)
    obtained_at = token_data.get("obtained_at", 0)
    if time.time() > obtained_at + expires_in - 300:
        print("Access token expired, refreshing...")
        token_data = _refresh_token(config, token_data)

    return token_data["access_token"]


# ---------------------------------------------------------------------------
# Upload archive helpers
# ---------------------------------------------------------------------------

def _archive_path(output_dir: str) -> Path:
    return Path(output_dir) / UPLOAD_ARCHIVE


def _album_archive_path(output_dir: str) -> Path:
    return Path(output_dir) / ALBUM_ARCHIVE


def _load_upload_archive(output_dir: str) -> dict[str, str]:
    """Return {relative_path: track_urn} from the archive file."""
    path = _archive_path(output_dir)
    if not path.exists():
        return {}
    archive = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            archive[parts[0]] = parts[1]
    return archive


def _append_upload_archive(output_dir: str, rel_path: str, track_urn: str):
    with _archive_lock:
        path = _archive_path(output_dir)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{rel_path}\t{track_urn}\n")


def _load_album_archive(output_dir: str) -> dict[str, str]:
    """Return {folder_name: playlist_urn} from the album archive."""
    path = _album_archive_path(output_dir)
    if not path.exists():
        return {}
    archive = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            archive[parts[0]] = parts[1]
    return archive


def _save_album_archive(output_dir: str, folder: str, playlist_urn: str):
    path = _album_archive_path(output_dir)
    # Load existing, update, rewrite
    archive = _load_album_archive(output_dir)
    archive[folder] = playlist_urn
    lines = [f"{k}\t{v}" for k, v in archive.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# SoundCloud API helpers
# ---------------------------------------------------------------------------

def _api_headers(access_token: str) -> dict:
    return {
        "Authorization": f"OAuth {access_token}",
        "Accept": "application/json; charset=utf-8",
    }


def _request_with_retry(method: str, url: str, access_token: str,
                         config: dict, **kwargs) -> requests.Response:
    """Make an API request with automatic token refresh and 429 backoff."""
    for attempt in range(MAX_RETRIES):
        headers = _api_headers(access_token)
        headers.update(kwargs.pop("extra_headers", {}))
        resp = requests.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            # Token might have expired mid-batch
            print("  Token expired mid-session, refreshing...")
            access_token = get_access_token(config)
            continue

        if resp.status_code == 429:
            wait = 2 ** (attempt + 1) * 5
            print(f"  Rate limited (429). Waiting {wait}s...")
            time.sleep(wait)
            continue

        if resp.status_code in (500, 502, 503, 504):
            wait = 2 ** attempt * 3
            print(f"  Server error ({resp.status_code}). Retrying in {wait}s...")
            time.sleep(wait)
            continue

        return resp

    # Return last response even if failed
    return resp


def upload_track(mp3_path: Path, title: str, genre: str, tag_list: str,
                 description: str, artwork_path: str | None,
                 access_token: str, config: dict) -> str | None:
    """Upload a single MP3 to SoundCloud. Returns the track URN or None."""
    files = {
        "track[asset_data]": (mp3_path.name, open(mp3_path, "rb"),
                              "audio/mpeg"),
    }
    data = {
        "track[title]": title,
        "track[genre]": genre,
        "track[tag_list]": tag_list,
        "track[description]": description,
        "track[sharing]": "public",
        "track[downloadable]": "false",
        "track[streamable]": "true",
        "track[commentable]": "true",
        "track[license]": "cc-by-nc",
    }

    if artwork_path and Path(artwork_path).exists():
        files["track[artwork_data]"] = (
            Path(artwork_path).name,
            open(artwork_path, "rb"),
            f"image/{Path(artwork_path).suffix.lstrip('.').replace('jpg', 'jpeg')}",
        )

    resp = _request_with_retry(
        "POST", f"{SC_API}/tracks", access_token, config,
        data=data, files=files, timeout=300,
    )

    # Close file handles
    for f in files.values():
        f[1].close()

    if resp.status_code == 201:
        track = resp.json()
        urn = track.get("urn", track.get("id", ""))
        print(f"    Uploaded: {title}  ->  {urn}")
        return str(urn)
    else:
        print(f"    FAILED to upload {title}: {resp.status_code} {resp.text[:200]}")
        return None


def create_or_update_album(folder_name: str, track_urns: list[str],
                           genre: str, description: str,
                           artwork_path: str | None,
                           access_token: str, config: dict) -> str | None:
    """Create a new album or update an existing one with the given tracks."""
    output_dir = config["output_dir"]
    album_archive = _load_album_archive(output_dir)
    existing_urn = album_archive.get(folder_name)

    tracks_payload = [{"urn": urn} for urn in track_urns]

    if existing_urn:
        # Update existing album with full track list
        print(f"  Updating existing album: {existing_urn}")
        payload = {
            "playlist": {
                "tracks": tracks_payload,
            }
        }
        resp = _request_with_retry(
            "PUT", f"{SC_API}/playlists/{existing_urn}",
            access_token, config,
            json=payload, timeout=60,
        )
        if resp.status_code == 200:
            print(f"  Album updated: {folder_name}")
            return existing_urn
        else:
            print(f"  FAILED to update album: {resp.status_code} {resp.text[:200]}")
            # Fall through to create a new one
            existing_urn = None

    if not existing_urn:
        # Create new album — always use JSON for track list
        print(f"  Creating album: {folder_name}")

        payload = {
            "playlist": {
                "title": folder_name,
                "description": description,
                "sharing": "public",
                "set_type": "album",
                "genre": genre,
                "tag_list": f"{genre} تلاوة قرآن",
                "tracks": tracks_payload,
            }
        }
        resp = _request_with_retry(
            "POST", f"{SC_API}/playlists",
            access_token, config,
            json=payload, timeout=120,
        )

        if resp.status_code != 201:
            print(f"  FAILED to create album: {resp.status_code} {resp.text[:200]}")
            return None

        playlist = resp.json()
        urn = str(playlist.get("urn", playlist.get("id", "")))
        _save_album_archive(output_dir, folder_name, urn)
        print(f"  Album created: {folder_name}  ->  {urn}")

        # Upload artwork separately if provided
        if artwork_path and Path(artwork_path).exists():
            print(f"  Uploading album artwork...")
            suffix = Path(artwork_path).suffix.lstrip('.').replace('jpg', 'jpeg')
            files = {
                "playlist[artwork_data]": (
                    Path(artwork_path).name,
                    open(artwork_path, "rb"),
                    f"image/{suffix}",
                ),
            }
            art_resp = _request_with_retry(
                "PUT", f"{SC_API}/playlists/{urn}",
                access_token, config,
                files=files, timeout=60,
            )
            files["playlist[artwork_data]"][1].close()
            if art_resp.status_code == 200:
                print(f"  Artwork uploaded.")
            else:
                print(f"  WARNING: Artwork upload failed: {art_resp.status_code}")

        return urn


# ---------------------------------------------------------------------------
# Batch upload orchestration
# ---------------------------------------------------------------------------

def upload_playlist_folder(folder_path: Path, config: dict,
                           access_token: str, dry_run: bool = False,
                           max_workers: int = DEFAULT_WORKERS) -> dict:
    """Upload all MP3s from a single playlist folder and create an album.

    Returns a stats dict: {uploaded, skipped, failed, album_created}.
    """
    folder_name = folder_path.name
    output_dir = config["output_dir"]
    genre = config["genre"]
    artwork_path = config["artwork_path"]

    mp3_files = sorted(folder_path.glob("*.mp3"))
    if not mp3_files:
        print(f"  No MP3 files in {folder_name}/, skipping.")
        return {"uploaded": 0, "skipped": 0, "failed": 0, "album_created": False}

    upload_archive = _load_upload_archive(output_dir)
    stats = {"uploaded": 0, "skipped": 0, "failed": 0, "album_created": False}

    # Reciter name = folder name (the YouTube playlist title)
    reciter = folder_name
    tag_list = f'"{reciter}" {genre} تلاوة قرآن'

    print(f"\n{'='*60}")
    print(f"Playlist: {folder_name}")
    print(f"  Reciter: {reciter}")
    print(f"  Tracks:  {len(mp3_files)}")
    print(f"  Workers: {max_workers}")
    print(f"{'='*60}")

    all_track_urns = []  # (index, urn) to preserve order
    to_upload = []       # (index, mp3_path, rel_path, title)

    # Separate already-uploaded from pending
    for i, mp3 in enumerate(mp3_files):
        rel_path = str(mp3.relative_to(output_dir))
        title = mp3.stem

        if rel_path in upload_archive:
            urn = upload_archive[rel_path]
            print(f"  [{i+1}/{len(mp3_files)}] Already uploaded: {title}")
            all_track_urns.append((i, urn))
            stats["skipped"] += 1
        elif dry_run:
            print(f"  [{i+1}/{len(mp3_files)}] Would upload: {title}")
            stats["uploaded"] += 1
        else:
            to_upload.append((i, mp3, rel_path, title))

    # Upload pending files concurrently
    if to_upload and not dry_run:
        print(f"\n  Uploading {len(to_upload)} track(s) with {max_workers} workers...")

        def _do_upload(item):
            idx, mp3, rel_path, title = item
            description = (
                f"{reciter} - {title}\n{genre}\n\n"
                "تلاوة من القرآن الكريم - Holy Quran Recitation\n"
                "This is a recitation of the Holy Quran. "
                "The Quran is not copyrighted material.\n"
                "هذه تلاوة من القرآن الكريم وهي ليست محمية بحقوق النشر"
            )
            urn = upload_track(
                mp3_path=mp3,
                title=title,
                genre=genre,
                tag_list=tag_list,
                description=description,
                artwork_path=artwork_path,
                access_token=access_token,
                config=config,
            )
            if urn:
                _append_upload_archive(output_dir, rel_path, urn)
            return idx, urn, title

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_do_upload, item): item for item in to_upload}
            for future in as_completed(futures):
                idx, urn, title = future.result()
                if urn:
                    all_track_urns.append((idx, urn))
                    stats["uploaded"] += 1
                else:
                    stats["failed"] += 1

    # Sort by original index to preserve playlist order
    all_track_urns.sort(key=lambda x: x[0])
    ordered_urns = [urn for _, urn in all_track_urns]

    # Create or update album(s) — split into chunks if > MAX_ALBUM_TRACKS
    if ordered_urns and not dry_run:
        description = f"{reciter}\n{genre}"
        chunks = [
            ordered_urns[i:i + MAX_ALBUM_TRACKS]
            for i in range(0, len(ordered_urns), MAX_ALBUM_TRACKS)
        ]
        need_parts = len(chunks) > 1
        albums_ok = 0
        for part_num, chunk in enumerate(chunks, 1):
            part_name = (
                f"{folder_name} (Part {part_num})"
                if need_parts else folder_name
            )
            result = create_or_update_album(
                folder_name=part_name,
                track_urns=chunk,
                genre=genre,
                description=description,
                artwork_path=artwork_path,
                access_token=access_token,
                config=config,
            )
            if result:
                albums_ok += 1
        stats["album_created"] = albums_ok > 0

    return stats


def upload_all(config: dict, dry_run: bool = False,
               playlist_filter: str | None = None) -> None:
    """Upload all playlist folders from the output directory."""
    output_dir = config["output_dir"]
    base = Path(output_dir)

    if not base.exists():
        print(f"Output directory not found: {output_dir}/")
        print("Run fetch_youtube.py first to download playlists.")
        sys.exit(1)

    # Collect playlist folders (skip hidden files/dirs)
    folders = sorted(
        p for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if playlist_filter:
        folders = [f for f in folders if f.name == playlist_filter]
        if not folders:
            print(f"Playlist folder not found: {playlist_filter}")
            print(f"Available folders in {output_dir}/:")
            for p in sorted(base.iterdir()):
                if p.is_dir() and not p.name.startswith("."):
                    mp3_count = len(list(p.glob("*.mp3")))
                    print(f"  {p.name}/ ({mp3_count} MP3s)")
            sys.exit(1)

    if not folders:
        print(f"No playlist folders found in {output_dir}/")
        sys.exit(1)

    print(f"Found {len(folders)} playlist folder(s) in {output_dir}/\n")

    if dry_run:
        print("DRY RUN — no files will be uploaded.\n")
        access_token = "dry-run"
    else:
        access_token = get_access_token(config)

    total = {"uploaded": 0, "skipped": 0, "failed": 0, "albums": 0}

    for folder in folders:
        stats = upload_playlist_folder(folder, config, access_token, dry_run,
                                       max_workers=config.get("workers", DEFAULT_WORKERS))
        total["uploaded"] += stats["uploaded"]
        total["skipped"] += stats["skipped"]
        total["failed"] += stats["failed"]
        if stats["album_created"]:
            total["albums"] += 1

    # Summary
    print(f"\n{'='*60}")
    print("Upload complete!")
    print(f"  Playlists processed : {len(folders)}")
    print(f"  Albums created/updated : {total['albums']}")
    print(f"  Tracks uploaded     : {total['uploaded']}")
    print(f"  Tracks skipped      : {total['skipped']}")
    print(f"  Tracks failed       : {total['failed']}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload downloaded MP3 playlists to SoundCloud as albums."
    )
    parser.add_argument(
        "--auth", action="store_true",
        help="Run only the OAuth authorization flow (first-time setup).",
    )
    parser.add_argument(
        "--playlist", type=str, default=None,
        help="Upload only this specific playlist folder name.",
    )
    parser.add_argument(
        "--artwork", type=str, default=None,
        help="Path to album/track artwork image (overrides .env).",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Number of concurrent uploads (default: {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be uploaded without actually uploading.",
    )
    args = parser.parse_args()

    config = load_config()

    if args.artwork:
        path = os.path.abspath(args.artwork)
        if not Path(path).exists():
            print(f"Artwork file not found: {args.artwork}")
            sys.exit(1)
        config["artwork_path"] = path

    config["workers"] = args.workers

    if args.auth:
        authorize(config)
        return

    upload_all(config, dry_run=args.dry_run, playlist_filter=args.playlist)


if __name__ == "__main__":
    main()
