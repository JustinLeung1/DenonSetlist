import os
import sqlite3
import wave
import audioread
from datetime import datetime

SESSIONS_FOLDER_NAME = "Sessions"
DB_PATH = os.path.join(os.path.dirname(__file__), 'hm.db')
SESSION_BUFFER_SECONDS = 120  # widen the match window so tracks right at the start/end edge aren't cut off


def find_sessions_path():
    """Scans /Volumes for any mounted drive containing a Sessions folder.
    Returns (sessions_path, db_path) or (None, None)."""
    volumes_root = "/Volumes"
    if not os.path.isdir(volumes_root):
        return None, None

    for volume in os.listdir(volumes_root):
        volume_root = os.path.join(volumes_root, volume)
        sessions_candidate = os.path.join(volume_root, SESSIONS_FOLDER_NAME)
        db_candidate = os.path.join(volume_root, "Engine Library", "Database2", "hm.db")
        if os.path.isdir(sessions_candidate) and os.path.isfile(db_candidate):
            return sessions_candidate, db_candidate

    return None, None


def get_wav_duration(file_path):
    """Returns duration in seconds using audioread, falls back to wave module."""
    try:
        with audioread.audio_open(file_path) as f:
            return f.duration
    except audioread.NoBackendError:
        pass
    except Exception:
        pass

    try:
        with wave.open(file_path, 'r') as f:
            return f.getnframes() / float(f.getframerate())
    except Exception as e:
        print(f"  [error] Could not read duration: {e}")
        return None


def is_valid_wav(filename):
    return not filename.startswith("._") and filename.lower().endswith(".wav")


def list_wav_sessions(sessions_path):
    """Returns a sorted list of (file_path, creation_time) for all valid WAV sessions."""
    sessions = []
    if not os.path.isdir(sessions_path):
        return sessions

    for name in os.listdir(sessions_path):
        if not is_valid_wav(name):
            continue
        file_path = os.path.join(sessions_path, name)
        if not os.path.isfile(file_path):
            continue
        ctime = os.path.getctime(file_path)
        sessions.append((file_path, name, ctime))

    sessions.sort(key=lambda x: x[2], reverse=True)
    return sessions


def get_setlist(file_path, db_path):
    """Queries the Engine DJ DB for tracks played during the session recorded in the WAV file."""
    # getctime() and the DB's startTime are both real (already-local) epoch seconds, no offset needed
    end_time = os.path.getctime(file_path)
    duration = get_wav_duration(file_path)

    if duration is None:
        print("  [error] Could not determine duration, skipping.")
        return []

    start_time = end_time - duration

    buffered_start = start_time - SESSION_BUFFER_SECONDS
    buffered_end = end_time + SESSION_BUFFER_SECONDS

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    query = """
        SELECT t.title, t.artist
        FROM Track t
        JOIN HistorylistEntity hle ON hle.trackId = t.id
        WHERE hle.startTime > ? AND hle.startTime < ?
        ORDER BY hle.startTime ASC
    """
    cursor.execute(query, (buffered_start, buffered_end))
    results = cursor.fetchall()
    conn.close()

    return [{"title": row[0], "artist": row[1]} for row in results]


def format_duration(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def print_setlist(file_path, name, ctime, db_path):
    duration = get_wav_duration(file_path)
    date_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %I:%M %p")
    dur_str = format_duration(duration) if duration else "unknown"

    print(f"\n{'='*50}")
    print(f"  Session : {name}")
    print(f"  Date    : {date_str}")
    print(f"  Duration: {dur_str}")
    print(f"{'='*50}")

    setlist = get_setlist(file_path, db_path)

    if not setlist:
        print("  No tracks found in database for this session.")
        return

    print(f"  {len(setlist)} track(s) played:\n")
    for i, track in enumerate(setlist, 1):
        title = track["title"] or "Unknown Title"
        artist = track["artist"] or "Unknown Artist"
        print(f"  {i:>3}. {artist} - {title}")

    print()


def select_session(sessions):
    """Prompts the user to pick a session from the list."""
    print("\nAvailable WAV Sessions:")
    print("-" * 50)
    for i, (file_path, name, ctime) in enumerate(sessions, 1):
        date_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %I:%M %p")
        print(f"  {i:>3}. [{date_str}]  {name}")
    print("-" * 50)
    print("   a. Show all sessions")
    print("   q. Quit")
    print()

    while True:
        choice = input("Select a session (number, 'a' for all, 'q' to quit): ").strip().lower()

        if choice == 'q':
            return None
        if choice == 'a':
            return 'all'
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(sessions):
                return sessions[idx - 1]
            print(f"  Please enter a number between 1 and {len(sessions)}.")
        else:
            print("  Invalid input. Enter a number, 'a', or 'q'.")


def main():
    sessions_path, db_path = find_sessions_path()
    if not sessions_path:
        print("No drive with a 'Sessions' folder and Engine Library database found in /Volumes.")
        print("Make sure your USB drive is connected.")
        return

    print(f"Found sessions at: {sessions_path}")
    sessions = list_wav_sessions(sessions_path)

    if not sessions:
        print(f"No WAV sessions found in: {sessions_path}")
        return

    selection = select_session(sessions)

    if selection is None:
        print("Exiting.")
        return

    if selection == 'all':
        for file_path, name, ctime in sessions:
            print_setlist(file_path, name, ctime, db_path)
    else:
        file_path, name, ctime = selection
        print_setlist(file_path, name, ctime, db_path)


if __name__ == "__main__":
    main()
