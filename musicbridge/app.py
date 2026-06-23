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

import json
import os
import random
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, Response, jsonify, request, send_from_directory
from waitress import serve
from ytmusicapi import YTMusic
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# browser.json (ytmusicapi browser auth) is generated from cookies.txt; we never
# ask the user to craft it by hand.
AUTH_FILE = os.environ.get("YTM_AUTH_FILE", "/data/browser.json")
SNAPFIFO = os.environ.get("SNAPFIFO", "/snapfifo/snapfifo")
PORT = int(os.environ.get("PORT", "8080"))
# Netscape cookies.txt — the single credential. It powers BOTH yt-dlp stream
# extraction AND (via browser.json, built from it) account access in ytmusicapi.
# Uploaded through the web UI's Account button.
COOKIES_FILE = os.environ.get("YTDLP_COOKIES", "/data/cookies.txt")
YTM_ORIGIN = "https://music.youtube.com"

# yt-dlp tuning. Leave the player client to yt-dlp's own auto-fallback (it lands
# on android_vr, which serves plain audio without a JS runtime / PO token);
# forcing a client list can exclude the one that actually works. `bestaudio/best`
# falls back to a combined stream if no audio-only format is offered. Both
# overridable via env if YouTube shifts again (e.g. YTDLP_PLAYER_CLIENT=tv).
YTDLP_FORMAT = os.environ.get("YTDLP_FORMAT", "bestaudio/best")
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "")

# Snapcast stream format — MUST match snapserver.conf (sampleformat=48000:16:2).
SAMPLE_RATE = "48000"
CHANNELS = "2"

# --------------------------------------------------------------------------- #
# YouTube Music account — cookie-based browser auth
# --------------------------------------------------------------------------- #

_ytmusic: Optional[YTMusic] = None
_ytmusic_lock = threading.Lock()


def parse_cookies_txt(path: str) -> dict:
    """Read a Netscape cookies.txt into {name: value}, keeping #HttpOnly_ ones
    (the session cookies most naive parsers drop)."""
    cookies: dict = {}
    with open(path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or (line.startswith("#")
                            and not line.startswith("#HttpOnly_")):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def build_browser_json() -> bool:
    """Generate browser.json from cookies.txt. Returns True if written.

    ytmusicapi classifies a headers file as 'browser' only if it has an
    authorization header containing SAPISIDHASH; it regenerates the actual hash
    per request from the cookie, so this stays valid as long as the cookie does.
    """
    if not os.path.exists(COOKIES_FILE):
        return False
    cookies = parse_cookies_txt(COOKIES_FILE)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        authorization = get_authorization(sapisid_from_cookie(cookie_str)
                                          + " " + YTM_ORIGIN)
    except Exception as exc:  # noqa: BLE001 — bad/incomplete cookie
        print(f"[ytm] cannot build browser.json from cookies: {exc}", flush=True)
        return False
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "authorization": authorization,
        "x-goog-authuser": "0",
        "x-origin": YTM_ORIGIN,
        "origin": YTM_ORIGIN,
        "cookie": cookie_str,
    }
    with open(AUTH_FILE, "w") as f:
        json.dump(headers, f)
    print(f"[ytm] built {AUTH_FILE} from cookies.txt "
          f"({len(cookies)} cookies)", flush=True)
    return True


def auth_method() -> str:
    """Account auth state for the UI: 'browser' (have cookie) | 'none'."""
    return "browser" if os.path.exists(AUTH_FILE) else "none"


def get_ytmusic() -> YTMusic:
    """ytmusicapi client, built lazily. Uses browser auth if browser.json exists,
    else unauthenticated (public search only)."""
    global _ytmusic
    if _ytmusic is None:
        with _ytmusic_lock:
            if _ytmusic is None:
                if os.path.exists(AUTH_FILE):
                    print(f"[ytm] using browser auth from {AUTH_FILE}", flush=True)
                    _ytmusic = YTMusic(AUTH_FILE)
                else:
                    print("[ytm] no cookie — running unauthenticated "
                          "(public search only)", flush=True)
                    _ytmusic = YTMusic()
    return _ytmusic


def reset_ytmusic():
    """Drop the cached client so the next call rebuilds with current auth."""
    global _ytmusic
    with _ytmusic_lock:
        _ytmusic = None


def account_probe() -> dict:
    """Check whether the current cookie is a logged-in session and report counts."""
    try:
        ytm = get_ytmusic()
        playlists = ytm.get_library_playlists(limit=50)
        # get_liked_songs throws a specific error when the session is signed out;
        # if it returns, we're definitely authenticated.
        try:
            liked = ytm.get_liked_songs(limit=1)
            liked_ok = True
        except Exception:  # noqa: BLE001
            liked_ok = False
        authenticated = len(playlists) > 0 or liked_ok
        return {"authenticated": authenticated, "playlists": len(playlists)}
    except Exception as exc:  # noqa: BLE001
        return {"authenticated": False, "error": str(exc)[:200]}


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
        "duration": item.get("duration_seconds"),
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
    shuffle: bool = False
    repeat: str = "off"                              # "off" | "all" | "one"
    _ytdlp: Optional[subprocess.Popen] = None
    _ffmpeg: Optional[subprocess.Popen] = None
    _stop: bool = False
    _skipped: bool = False
    _natural_end: bool = False                       # last track finished on its own
    _started: float = 0.0                            # monotonic start of current track
    _paused_at: float = 0.0                          # monotonic time pause began
    _paused_accum: float = 0.0                       # total paused seconds this track

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

    def play_tracks(self, tracks: list, shuffle: bool = False):
        """Replace the queue with a list (a whole playlist/album) and start it."""
        tracks = [t for t in tracks if t and t.get("videoId")]
        if not tracks:
            return
        with self._wake:
            if shuffle:
                self.shuffle = True
                tracks = tracks[:]
                random.shuffle(tracks)
            self.queue = tracks
            self.history = []
            self._interrupt()
            self._wake.notify()

    def skip(self):
        with self._wake:
            self._interrupt()
            self._wake.notify()

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
                self._paused_at = time.monotonic()

    def resume(self):
        with self._wake:
            if self.current and self.paused:
                self._signal_procs(signal.SIGCONT)
                self.paused = False
                self._paused_accum += time.monotonic() - self._paused_at

    def stop(self):
        with self._wake:
            self.queue.clear()
            self._stop = True
            self._interrupt()
            self._wake.notify()

    def set_shuffle(self, on: bool):
        with self._wake:
            self.shuffle = on
            if on:
                random.shuffle(self.queue)

    def set_repeat(self, mode: str):
        if mode not in ("off", "all", "one"):
            return
        with self._wake:
            self.repeat = mode

    def remove_at(self, index: int):
        with self._wake:
            if 0 <= index < len(self.queue):
                self.queue.pop(index)

    def clear_queue(self):
        with self._wake:
            self.queue.clear()

    def _elapsed(self) -> float:
        if not self.current or self._started == 0.0:
            return 0.0
        paused = self._paused_accum
        if self.paused:
            paused += time.monotonic() - self._paused_at
        return max(0.0, time.monotonic() - self._started - paused)

    def status(self) -> dict:
        with self._lock:
            return {
                "current": self.current,
                "paused": self.paused,
                "queue": list(self.queue),
                "playing": self.current is not None,
                "shuffle": self.shuffle,
                "repeat": self.repeat,
                "elapsed": round(self._elapsed()),
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

    def _next_track(self) -> Optional[dict]:
        """Pick the next track to play, honoring repeat/shuffle. Caller holds lock."""
        # repeat-one: replay the same track, but only if it ended on its own
        # (an explicit skip/prev/stop should still move on).
        if self.repeat == "one" and self._natural_end and self.current:
            return self.current
        if self.queue:
            return self.queue.pop(0)
        if self.repeat == "all" and self.history:
            self.queue = self.history[:]            # start the list over
            self.history = []
            if self.shuffle:
                random.shuffle(self.queue)
            return self.queue.pop(0)
        return None

    def _run(self):
        while True:
            with self._wake:
                nxt = self._next_track()
                while nxt is None:
                    self.current = None
                    self._wake.wait()
                    nxt = self._next_track()

                replaying = (self.repeat == "one" and self._natural_end
                             and nxt is self.current)
                self.current = nxt
                if not replaying:
                    self.history.append(nxt)
                self.paused = False
                self._stop = False
                self._skipped = False
                self._natural_end = False
                self._started = time.monotonic()
                self._paused_at = 0.0
                self._paused_accum = 0.0
                track = nxt

            self._play_blocking(track)

            with self._wake:
                # Did the track finish on its own (vs. skip/prev/stop)?
                self._natural_end = not (self._skipped or self._stop)

    def _play_blocking(self, track: dict):
        video_id = track["videoId"]
        # www.youtube.com extracts more reliably than music.youtube.com here.
        url = f"https://www.youtube.com/watch?v={video_id}"
        ytdlp_cmd = ["yt-dlp", "-f", YTDLP_FORMAT, "-o", "-",
                     "--quiet", "--no-warnings", "--no-progress"]
        if YTDLP_PLAYER_CLIENT:
            ytdlp_cmd += ["--extractor-args",
                          f"youtube:player_client={YTDLP_PLAYER_CLIENT}"]
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


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


# --- Account (cookie upload) ----------------------------------------------- #


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({"method": auth_method()})


@app.post("/api/cookies")
def api_cookies():
    """Receive an uploaded Netscape cookies.txt, store it, (re)build browser.json,
    and report whether it's a logged-in session."""
    raw = request.get_data(as_text=True) or ""
    if "\t" not in raw or "youtube" not in raw.lower():
        return jsonify({"ok": False,
                        "error": "That doesn't look like a cookies.txt "
                        "(expected Netscape/tab-separated YouTube cookies)."}), 400
    with open(COOKIES_FILE, "w") as f:
        f.write(raw)
    if not build_browser_json():
        return jsonify({"ok": False,
                        "error": "Saved, but the cookie is missing the values "
                        "needed for login (SAPISID). Re-export while logged in "
                        "to music.youtube.com."}), 400
    reset_ytmusic()
    probe = account_probe()
    print(f"[ytm] cookie uploaded — authenticated={probe.get('authenticated')} "
          f"playlists={probe.get('playlists')}", flush=True)
    return jsonify({"ok": True, **probe})


@app.post("/api/auth/logout")
def api_auth_logout():
    for path in (AUTH_FILE, COOKIES_FILE):
        if os.path.exists(path):
            os.remove(path)
    reset_ytmusic()
    return jsonify({"method": auth_method()})


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    results = get_ytmusic().search(query, filter="songs", limit=25)
    return jsonify([t for t in (normalize_track(r) for r in results) if t])


@app.get("/api/library")
def api_library():
    try:
        songs = get_ytmusic().get_library_songs(limit=100)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"library needs a cookie (Account): {exc}"}), 400
    return jsonify([t for t in (normalize_track(s) for s in songs) if t])


@app.get("/api/playlists")
def api_playlists():
    try:
        pls = get_ytmusic().get_library_playlists(limit=100)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"playlists need a cookie (Account): {exc}"}), 400
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
    pl = get_ytmusic().get_playlist(playlist_id, limit=200)
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


@app.post("/api/play_tracks")
def api_play_tracks():
    body = request.get_json(force=True) or {}
    engine.play_tracks(body.get("tracks", []), shuffle=bool(body.get("shuffle")))
    return jsonify(engine.status())


@app.post("/api/queue/remove")
def api_queue_remove():
    body = request.get_json(force=True) or {}
    engine.remove_at(int(body.get("index", -1)))
    return jsonify(engine.status())


@app.post("/api/queue/clear")
def api_queue_clear():
    engine.clear_queue()
    return jsonify(engine.status())


@app.post("/api/shuffle")
def api_shuffle():
    body = request.get_json(force=True) or {}
    engine.set_shuffle(bool(body.get("on")))
    return jsonify(engine.status())


@app.post("/api/repeat")
def api_repeat():
    body = request.get_json(force=True) or {}
    engine.set_repeat(body.get("mode", "off"))
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
        # Keep browser.json in sync with the (possibly refreshed) cookie.
        build_browser_json()
    else:
        print(f"[ytdlp] no cookies file at {COOKIES_FILE} — upload one via the "
              "Account button (powers both playback and library/playlists)")
    print(f"[musicbridge] serving on :{PORT}, FIFO={SNAPFIFO}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
