# Denon Setlist

Pulls a setlist (tracklist) for your DJ sessions by matching the WAV recording on your Engine DJ USB drive against the play history in Engine DJ's own database — and optionally uploads the session straight to SoundCloud with the tracklist as the description.

## How it works

Engine DJ hardware (Denon/Engine OS) can record your set to a `Sessions` folder on the USB drive, and separately logs every track played to `Engine Library/Database2/hm.db`. This tool:

1. Finds the mounted USB drive containing both the `Sessions` folder and `hm.db`.
2. Reads the WAV file's creation time and duration to work out when the session started and ended.
3. Queries `hm.db` for every track played in that window and prints/uses it as a tracklist.

## Requirements

- Python 3.9+
- An Engine DJ USB drive mounted under `/Volumes` (macOS) with a `Sessions` folder and `Engine Library/Database2/hm.db`
- A [SoundCloud API app](https://soundcloud.com/you/apps) (client ID + secret) — only needed for uploading

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install audioread requests python-dotenv
```

If you plan to upload to SoundCloud, copy `.env.example` to `.env` and fill in your app credentials:

```bash
cp .env.example .env
```

```
SC_CLIENT_ID=your_soundcloud_client_id
SC_CLIENT_SECRET=your_soundcloud_client_secret
```

## Usage

### View setlists

```bash
python3 main.py
```

Plug in your USB drive, run the script, and pick a session from the list (or `a` for all, `q` to quit). It prints the date, duration, and full tracklist for the session(s) you select.

### Upload sessions to SoundCloud

```bash
python3 upload_sessions.py
```

The first time you run this, mark your existing sessions as already handled so they aren't all uploaded at once:

```bash
python3 upload_sessions.py --setup
```

After that, running `upload_sessions.py` will upload only **new** sessions (ones not yet recorded in `uploaded_sessions.json`), using the matched tracklist as the upload's description.

Useful flags:

| Flag | Description |
| --- | --- |
| `--privacy public\|private` | Visibility for uploaded tracks (default: `private`) |
| `--dry-run` | List sessions that would be uploaded without uploading them |
| `--yes` | Skip the confirmation prompt |
| `--setup` | First-time setup — marks existing sessions as handled without uploading |

The first time you upload, a browser window opens for you to authorize the app with your SoundCloud account. Tokens are cached at `~/.soundcloud_tokens.json` and refreshed automatically afterward.

## Files

- `main.py` — finds the drive, matches WAV sessions to play history, prints setlists
- `upload_sessions.py` — SoundCloud OAuth + upload, tracking uploaded sessions in `uploaded_sessions.json`
- `.env.example` — template for your SoundCloud app credentials
