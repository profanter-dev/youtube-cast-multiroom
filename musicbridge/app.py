"""
musicbridge — a small web remote that plays YouTube Music (via your account)
into the Snapcast FIFO, so every Snapcast client (your Google TVs running the
Snapcast app) plays it in sync.

Why this exists: YouTube Music will not cast to a self-built Cast receiver
(first-party Cast device authentication requires a Google-signed hardware cert).
So instead of pretending to be a Cast target, we drive your YTM account directly
with ytmusicapi, resolve the audio stream with yt-dlp, decode it to raw PCM with
ffmpeg, and write that into the same FIFO Snapcast already reads from.

Control surface: a password-protected web UI (Traefik basic-auth in front).
"""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve
from ytmusicapi import YTMusic

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

AUTH_FILE = os.environ.get("YTM_AUTH_FILE", "/data/browser.json")
SNAPFIFO = os.environ.get("SNAPFIFO", "/snapfifo/snapfifo")
PORT = int(os.environ.get("PORT", "8080"))
# Optional Netscape cookies.txt for yt-dlp, used if present. YouTube increasingly
# demands cookies ("confirm you're not a bot") for stream extraction.
COOKIES_FILE = os.environ.get("YTDLP_COOKIES", "/data/cookies.txt")

# Snapcast stream format — MUST match snapserver.conf (sampleformat=48000:16:2).
SAMPLE_RATE = "48000"
CHANNELS = "2"

# --------------------------------------------------------------------------- #
# YouTube Music account
# --------------------------------------------------------------------------- #


def make_ytmusic() -> YTMusic:
    """Authenticated client if browser.json is present, else search-only."""
    if os.path.exists(AUTH_FILE):
        print(f"[ytm] using account auth from {AUTH_FILE}")
        return YTMusic(AUTH_FILE)
    print(f"[ytm] no auth file at {AUTH_FILE} — running unauthenticated "
          "(public search only, no library/playlists)")
    return YTMusic()


ytmusic = make_ytmusic()


def normalize_track(item: dict) -> Optional[dict]:
    """Reduce a ytmusicapi result to the fields the UI and player need."""
    video_id = item.get("videoId")
    if not video_id:
        return None
    artists = item.get("artists") or []
    artist = ", ".join(a["name"] for a in artists if a.get("name"))
    thumbs = item.get("thumbnails") or []
    thumb = thumbs[-1]["url"] if thumbs else None
    album = item.get("album")
    return {
        "videoId": video_id,
        "title": item.get("title", "Unknown"),
        "artist": artist,
        "album": album.get("name") if isinstance(album, dict) else None,
        "thumb": thumb,
    }


# --------------------------------------------------------------------------- #
# Playback engine
# --------------------------------------------------------------------------- #


@dataclass
class Engine:
    queue: list = field(default_factory=list)        # upcoming tracks
    history: list = field(default_factory=list)      # finished/played tracks
    current: Optional[dict] = None
    paused: bool = False
    _ytdlp: Optional[subprocess.Popen] = None
    _ffmpeg: Optional[subprocess.Popen] = None
    _stop: bool = False
    _skipped: bool = False

    def __post_init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        ensure_fifo()
        threading.Thread(target=self._run, daemon=True).start()

    # -- public controls ---------------------------------------------------- #

    def play_now(self, track: dict):
        with self._wake:
            self.queue.insert(0, track)
            self._interrupt()
            self._wake.notify()

    def enqueue(self, track: dict):
        with self._wake:
            self.queue.append(track)
            self._wake.notify()

    def skip(self):
        with self._wake:
            self._interrupt()

    def previous(self):
        with self._wake:
            # history[-1] is the track currently playing; the one before it is "prev"
            if len(self.history) >= 2:
                prev = self.history[-2]
                self.queue.insert(0, prev)
                self._interrupt()
                self._wake.notify()

    def pause(self):
        with self._wake:
            if self.current and not self.paused:
                self._signal_procs(signal.SIGSTOP)
                self.paused = True

    def resume(self):
        with self._wake:
            if self.current and self.paused:
                self._signal_procs(signal.SIGCONT)
                self.paused = False

    def stop(self):
        with self._wake:
            self.queue.clear()
            self._stop = True
            self._interrupt()

    def status(self) -> dict:
        with self._lock:
            return {
                "current": self.current,
                "paused": self.paused,
                "queue": list(self.queue),
                "playing": self.current is not None,
            }

    # -- internals ---------------------------------------------------------- #

    def _signal_procs(self, sig):
        for p in (self._ytdlp, self._ffmpeg):
            if p and p.poll() is None:
                try:
                    p.send_signal(sig)
                except ProcessLookupError:
                    pass

    def _interrupt(self):
        """Kill the current track's processes so the worker advances."""
        self._skipped = True
        if self.paused:                 # can't kill a stopped process cleanly
            self._signal_procs(signal.SIGCONT)
            self.paused = False
        for p in (self._ytdlp, self._ffmpeg):
            if p and p.poll() is None:
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

    def _run(self):
        while True:
            with self._wake:
                while not self.queue:
                    self.current = None
                    self._wake.wait()
                self.current = self.queue.pop(0)
                self.history.append(self.current)
                self.paused = False
                self._stop = False
                self._skipped = False
                track = self.current

            self._play_blocking(track)

    def _play_blocking(self, track: dict):
        video_id = track["videoId"]
        url = f"https://music.youtube.com/watch?v={video_id}"
        ytdlp_cmd = ["yt-dlp", "-f", "bestaudio", "-o", "-",
                     "--quiet", "--no-warnings", "--no-progress"]
        if os.path.exists(COOKIES_FILE):
            ytdlp_cmd += ["--cookies", COOKIES_FILE]
        ytdlp_cmd.append(url)

        started = time.monotonic()
        print(f"[player] ▶ {track.get('title', video_id)} ({video_id})", flush=True)
        try:
            # stderr is inherited (not silenced) so yt-dlp/ffmpeg errors land in
            # `docker compose logs musicbridge`.
            self._ytdlp = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE)
            self._ffmpeg = subprocess.Popen(
                # -y: don't refuse to "overwrite" the FIFO, which always exists.
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", "pipe:0", "-f", "s16le",
                 "-ar", SAMPLE_RATE, "-ac", CHANNELS, SNAPFIFO],
                stdin=self._ytdlp.stdout,
            )
            # Let ffmpeg own the read end so yt-dlp gets SIGPIPE if ffmpeg dies.
            self._ytdlp.stdout.close()
            ff_rc = self._ffmpeg.wait()
            yt_rc = self._ytdlp.wait()
            elapsed = time.monotonic() - started
            print(f"[player] ■ ended after {elapsed:.1f}s "
                  f"(ffmpeg={ff_rc}, yt-dlp={yt_rc})", flush=True)
            if elapsed < 2 and not self._skipped:
                print("[player] ⚠ ended almost instantly — yt-dlp could not get "
                      "the stream (see its error above). If YouTube asks to "
                      "'confirm you're not a bot', drop a Netscape cookies.txt at "
                      f"{COOKIES_FILE}.", flush=True)
        except Exception as exc:  # noqa: BLE001 — never let the worker die
            print(f"[player] error: {exc}", flush=True)
        finally:
            self._signal_procs(signal.SIGCONT)  # in case we were paused
            for p in (self._ytdlp, self._ffmpeg):
                if p and p.poll() is None:
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass
            self._ytdlp = self._ffmpeg = None


def ensure_fifo():
    if not os.path.exists(SNAPFIFO):
        os.makedirs(os.path.dirname(SNAPFIFO), exist_ok=True)
        os.mkfifo(SNAPFIFO)
        print(f"[fifo] created {SNAPFIFO}")


engine = Engine()

# --------------------------------------------------------------------------- #
# Web API
# --------------------------------------------------------------------------- #

app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    results = ytmusic.search(query, filter="songs", limit=25)
    return jsonify([t for t in (normalize_track(r) for r in results) if t])


@app.get("/api/library")
def api_library():
    try:
        songs = ytmusic.get_library_songs(limit=100)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"library needs account auth: {exc}"}), 400
    return jsonify([t for t in (normalize_track(s) for s in songs) if t])


@app.get("/api/playlists")
def api_playlists():
    try:
        pls = ytmusic.get_library_playlists(limit=100)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"playlists need account auth: {exc}"}), 400
    return jsonify([
        {
            "playlistId": p.get("playlistId"),
            "title": p.get("title"),
            "thumb": (p.get("thumbnails") or [{}])[-1].get("url"),
        }
        for p in pls if p.get("playlistId")
    ])


@app.get("/api/playlist/<playlist_id>")
def api_playlist(playlist_id):
    pl = ytmusic.get_playlist(playlist_id, limit=200)
    tracks = [t for t in (normalize_track(x) for x in pl.get("tracks", [])) if t]
    return jsonify({"title": pl.get("title"), "tracks": tracks})


@app.post("/api/play")
def api_play():
    engine.play_now(request.get_json(force=True))
    return jsonify(engine.status())


@app.post("/api/queue")
def api_queue():
    engine.enqueue(request.get_json(force=True))
    return jsonify(engine.status())


@app.post("/api/<action>")
def api_action(action):
    fn = {
        "pause": engine.pause,
        "resume": engine.resume,
        "next": engine.skip,
        "prev": engine.previous,
        "stop": engine.stop,
    }.get(action)
    if not fn:
        return jsonify({"error": "unknown action"}), 404
    fn()
    return jsonify(engine.status())


@app.get("/api/status")
def api_status():
    return jsonify(engine.status())


if __name__ == "__main__":
    if os.path.exists(COOKIES_FILE):
        print(f"[ytdlp] using cookies from {COOKIES_FILE}")
    else:
        print(f"[ytdlp] no cookies file at {COOKIES_FILE} — playback works "
              "until YouTube demands a 'not a bot' check")
    print(f"[musicbridge] serving on :{PORT}, FIFO={SNAPFIFO}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
