import argparse
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

from main import find_sessions_path, list_wav_sessions, get_setlist, build_title, select_sessions_multi

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Mixcloud config ──────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("MC_CLIENT_ID")
CLIENT_SECRET = os.getenv("MC_CLIENT_SECRET")

REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_CACHE  = Path.home() / ".mixcloud_tokens.json"

AUTH_URL   = "https://www.mixcloud.com/oauth/authorize"
TOKEN_URL  = "https://www.mixcloud.com/oauth/access_token"
UPLOAD_URL = "https://api.mixcloud.com/upload/"

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "uploaded_sessions_mixcloud.json")

# ── OAuth flow ────────────────────────────────────────────────────────────────
# Mixcloud's flow has no PKCE and access tokens don't expire, so there's no
# refresh dance here — just cache the token and re-auth if it's ever rejected.

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


def _load_token():
    if TOKEN_CACHE.exists():
        with open(TOKEN_CACHE) as f:
            return json.load(f).get("access_token")
    return None


def _save_token(access_token):
    with open(TOKEN_CACHE, "w") as f:
        json.dump({"access_token": access_token}, f, indent=2)
    TOKEN_CACHE.chmod(0o600)


def _authorize():
    global _auth_code
    _auth_code = None

    url = AUTH_URL + "?" + urlencode({
        "client_id":    CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    })

    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    server.timeout = 120
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nOpening Mixcloud authorization in your browser…")
    print(f"If it doesn't open automatically, visit:\n  {url}\n")
    webbrowser.open(url)
    thread.join(timeout=120)

    if not _auth_code:
        sys.exit("Did not receive an authorization code within 2 minutes.")

    print("Authorization code received — exchanging for token…")
    resp = requests.get(TOKEN_URL, params={
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "client_secret": CLIENT_SECRET,
        "code":          _auth_code,
    })
    resp.raise_for_status()
    access_token = resp.json()["access_token"]
    _save_token(access_token)
    print(f"Token cached at {TOKEN_CACHE}\n")
    return access_token


def get_access_token():
    return _load_token() or _authorize()


# ── Upload ────────────────────────────────────────────────────────────────────


def build_sections(setlist):
    """Maps a setlist into Mixcloud's sections-X-{artist,song,start_time} fields."""
    fields = {}
    for i, track in enumerate(setlist):
        fields[f"sections-{i}-artist"] = track["artist"] or "Unknown Artist"
        fields[f"sections-{i}-song"] = track["title"] or "Unknown Title"
        fields[f"sections-{i}-start_time"] = track["offset_seconds"]
    return fields


def upload_track(file_path, name, setlist, unlisted=False, retry_on_auth_failure=True):
    access_token = get_access_token()
    path = Path(file_path)

    if not path.exists():
        sys.exit(f"File not found: {file_path}")

    data = {"name": name, **build_sections(setlist)}
    if unlisted:
        data["unlisted"] = "1"

    print(f"Uploading '{path.name}' as '{name}'…")

    with open(path, "rb") as audio_file:
        resp = requests.post(
            UPLOAD_URL,
            params={"access_token": access_token},
            data=data,
            files={"mp3": (path.name, audio_file)},
        )

    if resp.status_code == 200 and resp.json().get("result", {}).get("success"):
        result = resp.json()["result"]
        print(f"  Uploaded: https://www.mixcloud.com{result['key']}")
        return result

    if resp.status_code in (401, 403) and retry_on_auth_failure:
        print("  Access token rejected — re-authenticating…")
        TOKEN_CACHE.unlink(missing_ok=True)
        return upload_track(file_path, name, setlist, unlisted, retry_on_auth_failure=False)

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


def upload_session(file_path, name, ctime, db_path, unlisted):
    setlist = get_setlist(file_path, db_path)
    title = build_title(name, ctime)
    return upload_track(file_path, title, setlist, unlisted)


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
            "key": None,
            "url": None,
            "skipped_initial_setup": True,
        }

    save_manifest(manifest)
    print(f"\nDone. {len(skipped)} session(s) marked — future runs will only upload sessions recorded after this point.")


def main():
    parser = argparse.ArgumentParser(description="Upload new DJ sessions to Mixcloud.")
    parser.add_argument("--unlisted", action="store_true",
                         help="Upload as unlisted instead of public (requires Mixcloud Pro)")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt before uploading")
    parser.add_argument("--dry-run", action="store_true",
                         help="Only show which sessions would be uploaded, don't upload")
    parser.add_argument("--setup", action="store_true",
                         help="First-time setup: mark all current sessions as already uploaded, "
                              "without uploading them, so only future sessions get uploaded")
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
            "Set your Mixcloud app credentials first:\n"
            "  export MC_CLIENT_ID=your_client_id\n"
            "  export MC_CLIENT_SECRET=your_client_secret"
        )

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
            result = upload_session(file_path, name, ctime, db_path, args.unlisted)
        except SystemExit as e:
            print(f"  [skip] Upload failed for {name}: {e}")
            continue

        manifest[name] = {
            "uploaded_at": datetime.now().isoformat(),
            "key":         result.get("key"),
            "url":         f"https://www.mixcloud.com{result['key']}" if result.get("key") else None,
        }
        save_manifest(manifest)


if __name__ == "__main__":
    main()
