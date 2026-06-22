import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

from main import find_sessions_path, list_wav_sessions, get_setlist, get_wav_duration, build_title, select_sessions_multi

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── SoundCloud config ────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("SC_CLIENT_ID")
CLIENT_SECRET = os.getenv("SC_CLIENT_SECRET")

REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_CACHE  = Path.home() / ".soundcloud_tokens.json"

AUTH_URL  = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_BASE  = "https://api.soundcloud.com"

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "uploaded_sessions.json")

# How close a SoundCloud track's duration must be (in seconds) to a local WAV's
# duration to count as the same recording when verifying against the manifest.
DURATION_TOLERANCE_SECONDS = 3

# ── PKCE helpers ──────────────────────────────────────────────────────────────


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Token cache ───────────────────────────────────────────────────────────────


def _load_tokens():
    if TOKEN_CACHE.exists():
        with open(TOKEN_CACHE) as f:
            return json.load(f)
    return {}


def _save_tokens(tokens):
    with open(TOKEN_CACHE, "w") as f:
        json.dump(tokens, f, indent=2)
    TOKEN_CACHE.chmod(0o600)


# ── OAuth flow ────────────────────────────────────────────────────────────────

_auth_code = None  # written by the callback handler


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [None])[0]
        _auth_code = code

        if code:
            body = b"<h2>Authorized! You can close this tab.</h2>"
        else:
            body = b"<h2>Authorization failed. Check the terminal.</h2>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # silence request logs
        pass


def _exchange_code(code, verifier):
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
        "code":          code,
    })
    resp.raise_for_status()
    return resp.json()


def _refresh_tokens(refresh_token):
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    return resp.json()


def get_access_token():
    """Return a valid access token, refreshing or re-authing as needed."""
    tokens = _load_tokens()

    if tokens.get("refresh_token"):
        expires_at = tokens.get("expires_at", 0)
        if time.time() >= expires_at - 60:
            print("Access token expired — refreshing…")
            try:
                fresh = _refresh_tokens(tokens["refresh_token"])
                tokens = {
                    "access_token":  fresh["access_token"],
                    "refresh_token": fresh.get("refresh_token", tokens["refresh_token"]),
                    "expires_at":    time.time() + fresh.get("expires_in", 3600),
                }
                _save_tokens(tokens)
                return tokens["access_token"]
            except requests.HTTPError as e:
                print(f"Refresh failed ({e}), re-authenticating…")
        elif tokens.get("access_token"):
            return tokens["access_token"]

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    url = AUTH_URL + "?" + urlencode({
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "response_type":         "code",
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    })

    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    server.timeout = 120
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nOpening SoundCloud authorization in your browser…")
    print(f"If it doesn't open automatically, visit:\n  {url}\n")
    webbrowser.open(url)
    thread.join(timeout=120)

    if not _auth_code:
        sys.exit("Did not receive an authorization code within 2 minutes.")

    print("Authorization code received — exchanging for tokens…")
    token_data = _exchange_code(_auth_code, verifier)

    tokens = {
        "access_token":  token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at":    time.time() + token_data.get("expires_in", 3600),
    }
    _save_tokens(tokens)
    print(f"Tokens cached at {TOKEN_CACHE}\n")
    return tokens["access_token"]


# ── Upload ────────────────────────────────────────────────────────────────────


def upload_track(file_path, title, description="", tags="", privacy="private"):
    access_token = get_access_token()
    path = Path(file_path)

    if not path.exists():
        sys.exit(f"File not found: {file_path}")

    print(f"Uploading '{path.name}' as '{title}'…")

    with open(path, "rb") as audio_file:
        resp = requests.post(
            f"{API_BASE}/tracks",
            headers={"Authorization": f"OAuth {access_token}"},
            data={
                "track[title]":       title,
                "track[description]": description,
                "track[tag_list]":    tags,
                "track[sharing]":     privacy,
            },
            files={"track[asset_data]": (path.name, audio_file)},
        )

    if resp.status_code == 201:
        track = resp.json()
        print(f"  Uploaded: {track.get('permalink_url')}")
        return track

    sys.exit(f"Upload failed ({resp.status_code}): {resp.text}")


# ── Session manifest ─────────────────────────────────────────────────────────


def load_manifest():
    if os.path.isfile(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


def build_description(setlist):
    if not setlist:
        return "Tracklist unavailable."
    lines = [
        f"{i}. {t['artist'] or 'Unknown Artist'} - {t['title'] or 'Unknown Title'}"
        for i, t in enumerate(setlist, 1)
    ]
    return "\n".join(lines)


def upload_session(file_path, name, ctime, db_path, privacy):
    setlist = get_setlist(file_path, db_path)
    description = build_description(setlist)
    title = build_title(name, ctime)

    return upload_track(
        file_path=file_path,
        title=title,
        description=description,
        tags="",
        privacy=privacy,
    )


# ── SoundCloud track verification ────────────────────────────────────────────


def fetch_my_tracks():
    """Returns every track on the authenticated SoundCloud account."""
    access_token = get_access_token()
    tracks = []
    url = f"{API_BASE}/me/tracks"
    params = {"linked_partitioning": 1, "limit": 200}

    while url:
        resp = requests.get(url, headers={"Authorization": f"OAuth {access_token}"}, params=params)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            tracks.extend(data.get("collection", []))
            url = data.get("next_href")
        else:
            tracks.extend(data)
            url = None
        params = None

    return tracks


def _parse_sc_time(value):
    """Parses a SoundCloud API timestamp, returning epoch seconds or None."""
    for fmt in ("%Y/%m/%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            continue
    return None


def find_matching_track(duration, ctime, tracks, used_track_ids):
    """Finds the SoundCloud track that matches a local session's duration.

    Title is user-editable and SoundCloud doesn't expose the original filename,
    so duration is the only stable fingerprint; created_at (upload time) just
    breaks ties between sessions of near-identical length.
    """
    if duration is None:
        return None

    candidates = [
        t for t in tracks
        if t["id"] not in used_track_ids
        and abs((t.get("duration") or 0) / 1000.0 - duration) <= DURATION_TOLERANCE_SECONDS
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def time_gap(t):
        created = _parse_sc_time(t.get("created_at", ""))
        return abs(created - ctime) if created is not None else float("inf")

    candidates.sort(key=time_gap)
    return candidates[0]


def run_verify(sessions, manifest, db_path, privacy, apply_changes):
    """Audits local sessions against the SoundCloud account's actual tracks,
    rather than trusting the local manifest, then offers to upload anything
    that was never uploaded at all."""
    print("Fetching your SoundCloud tracks…")
    tracks = fetch_my_tracks()
    print(f"  Found {len(tracks)} track(s) on SoundCloud.\n")

    used_track_ids = set()
    matches = {}
    for file_path, name, ctime in sessions:
        duration = get_wav_duration(file_path)
        match = find_matching_track(duration, ctime, tracks, used_track_ids)
        if match:
            used_track_ids.add(match["id"])
        matches[name] = match

    stale_deleted = []  # had a real track_id, but no match found — maybe deleted on SoundCloud
    stale_skipped = []  # never actually uploaded (skipped during --setup), still not found
    discovered = []     # matching track found, but missing from the manifest
    missing = []        # no manifest entry and no matching track — never uploaded

    for file_path, name, ctime in sessions:
        match = matches[name]
        entry = manifest.get(name)
        if entry is not None and not match:
            (stale_deleted if entry.get("track_id") else stale_skipped).append(name)
        elif entry is None and match:
            discovered.append((name, match))
        elif entry is None and not match:
            missing.append((file_path, name, ctime))

    in_sync = len(sessions) - len(stale_deleted) - len(stale_skipped) - len(discovered) - len(missing)
    print(f"  In sync:                                     {in_sync}")
    print(f"  Uploaded before, no longer found (deleted?): {len(stale_deleted)}")
    for name in stale_deleted:
        print(f"    - {name}")
    print(f"  Skipped during setup, still not uploaded:    {len(stale_skipped)}")
    for name in stale_skipped:
        print(f"    - {name}")
    print(f"  Found on SoundCloud, missing from manifest:  {len(discovered)}")
    for name, match in discovered:
        print(f"    - {name}  ->  {match.get('permalink_url')}")
    print(f"  Never uploaded:                              {len(missing)}")
    for _, name, _ in missing:
        print(f"    - {name}")
    print()

    if discovered:
        if apply_changes:
            for name, match in discovered:
                manifest[name] = {
                    "uploaded_at": datetime.now().isoformat(),
                    "track_id": match.get("id"),
                    "url": match.get("permalink_url"),
                    "privacy": match.get("sharing"),
                    "discovered_via_verify": True,
                }
            save_manifest(manifest)
            print(f"Manifest updated with {len(discovered)} discovered track(s).\n")
        else:
            print("Run again with --apply to add these to the manifest.\n")

    if not missing:
        return

    confirm = input(
        f"Upload {len(missing)} session(s) that have never been uploaded, as '{privacy}'? [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print("Skipped upload.")
        return

    for file_path, name, ctime in missing:
        print(f"\nUploading {name}...")
        try:
            track = upload_session(file_path, name, ctime, db_path, privacy)
        except SystemExit as e:
            print(f"  [skip] Upload failed for {name}: {e}")
            continue

        manifest[name] = {
            "uploaded_at": datetime.now().isoformat(),
            "track_id":    track.get("id"),
            "url":         track.get("permalink_url"),
            "privacy":     privacy,
        }
        save_manifest(manifest)


# ── CLI ───────────────────────────────────────────────────────────────────────


def run_setup(sessions, manifest):
    """First-time setup: mark all currently-existing sessions as already handled,
    without uploading them, so only sessions recorded after this point count as new."""
    skipped = [s for s in sessions if s[1] not in manifest]

    if not skipped:
        print("Nothing to do — manifest already covers every session on the drive.")
        return

    print(f"Marking {len(skipped)} existing session(s) as already uploaded (first-time setup):")
    for _, name, ctime in skipped:
        date_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %I:%M %p")
        print(f"  - {name}  [{date_str}]")
        manifest[name] = {
            "uploaded_at": datetime.now().isoformat(),
            "track_id": None,
            "url": None,
            "privacy": None,
            "skipped_initial_setup": True,
        }

    save_manifest(manifest)
    print(f"\nDone. {len(skipped)} session(s) marked — future runs will only upload sessions recorded after this point.")


def main():
    parser = argparse.ArgumentParser(description="Upload new DJ sessions to SoundCloud.")
    parser.add_argument("--privacy", default="private", choices=["public", "private"],
                         help="Visibility for uploaded tracks (default: private)")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt before uploading")
    parser.add_argument("--dry-run", action="store_true",
                         help="Only show which sessions would be uploaded, don't upload")
    parser.add_argument("--setup", action="store_true",
                         help="First-time setup: mark all current sessions as already uploaded, "
                              "without uploading them, so only future sessions get uploaded")
    parser.add_argument("--verify", action="store_true",
                         help="Audit local sessions against your actual SoundCloud tracks "
                              "(matched by duration), instead of trusting the local manifest")
    parser.add_argument("--apply", action="store_true",
                         help="With --verify, write discovered SoundCloud tracks back into the manifest")
    args = parser.parse_args()

    sessions_path, db_path = find_sessions_path()
    if not sessions_path:
        print("No drive with a 'Sessions' folder and Engine Library database found in /Volumes.")
        print("Make sure your USB drive is connected.")
        return

    sessions = list_wav_sessions(sessions_path)
    if not sessions:
        print(f"No WAV sessions found in: {sessions_path}")
        return

    manifest = load_manifest()

    if args.setup:
        run_setup(sessions, manifest)
        return

    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit(
            "Set your SoundCloud app credentials first:\n"
            "  export SC_CLIENT_ID=your_client_id\n"
            "  export SC_CLIENT_SECRET=your_client_secret"
        )

    if args.verify:
        run_verify(sessions, manifest, db_path, args.privacy, args.apply)
        return

    new_sessions = [s for s in sessions if s[1] not in manifest]

    if not new_sessions:
        print("No new sessions to upload — everything is already in the manifest.")
        return

    if args.dry_run:
        print(f"Found {len(new_sessions)} new session(s):")
        for _, name, ctime in new_sessions:
            date_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %I:%M %p")
            print(f"  - {name}  [{date_str}]")
        print("\nDry run — nothing uploaded.")
        return

    if args.yes:
        to_upload = new_sessions
    else:
        to_upload = select_sessions_multi(new_sessions)
        if not to_upload:
            print("Aborted.")
            return

    for file_path, name, ctime in to_upload:
        print(f"\nUploading {name}...")
        try:
            track = upload_session(file_path, name, ctime, db_path, args.privacy)
        except SystemExit as e:
            print(f"  [skip] Upload failed for {name}: {e}")
            continue

        manifest[name] = {
            "uploaded_at": datetime.now().isoformat(),
            "track_id":    track.get("id"),
            "url":         track.get("permalink_url"),
            "privacy":     args.privacy,
        }
        save_manifest(manifest)


if __name__ == "__main__":
    main()
