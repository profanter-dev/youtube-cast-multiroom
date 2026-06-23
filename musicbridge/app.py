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
import random
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, Response, jsonify, request, send_from_directory
from waitress import serve
from ytmusicapi import OAuthCredentials, YTMusic
from ytmusicapi.auth.oauth import RefreshingToken

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

AUTH_FILE = os.environ.get("YTM_AUTH_FILE", "/data/browser.json")
# OAuth device-flow token (self-refreshing). Preferred over browser.json because
# it doesn't go stale. Needs a Google Cloud OAuth client of type "TVs and Limited
# Input devices" — its id/secret go in the env below.
OAUTH_FILE = os.environ.get("YTM_OAUTH_FILE", "/data/oauth.json")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
SNAPFIFO = os.environ.get("SNAPFIFO", "/snapfifo/snapfifo")
PORT = int(os.environ.get("PORT", "8080"))
# Optional Netscape cookies.txt for yt-dlp, used if present. YouTube increasingly
# demands cookies ("confirm you're not a bot") for stream extraction.
COOKIES_FILE = os.environ.get("YTDLP_COOKIES", "/data/cookies.txt")

# Snapcast stream format — MUST match snapserver.conf (sampleformat=48000:16:2).
SAMPLE_RATE = "48000"
CHANNELS = "2"

# --------------------------------------------------------------------------- #
# YouTube Music account (lazy — never block server startup on a network call)
# --------------------------------------------------------------------------- #

_ytmusic: Optional[YTMusic] = None
_ytmusic_lock = threading.Lock()


def oauth_credentials() -> Optional[OAuthCredentials]:
    """OAuth client creds from env, or None if not configured."""
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return OAuthCredentials(client_id=GOOGLE_CLIENT_ID,
                                client_secret=GOOGLE_CLIENT_SECRET)
    return None


def auth_method() -> str:
    """Which credential source is active: 'oauth' | 'browser' | 'none'."""
    if os.path.exists(OAUTH_FILE) and oauth_credentials():
        return "oauth"
    if os.path.exists(AUTH_FILE):
        return "browser"
    return "none"


def get_ytmusic() -> YTMusic:
    """Build the YTMusic client on first use, not at import time.

    The YTMusic() constructor makes a network request to YouTube. Doing that at
    import time means a slow/blocked request (e.g. an anti-bot rate limit on the
    host IP) would stop the web server from ever starting — the page would just
    "load forever". Building it lazily keeps the UI responsive no matter what.

    Auth precedence: self-refreshing OAuth token, then browser.json, then
    unauthenticated (public search only).
    """
    global _ytmusic
    if _ytmusic is None:
        with _ytmusic_lock:
            if _ytmusic is None:
                method = auth_method()
                if method == "oauth":
                    print(f"[ytm] using OAuth token from {OAUTH_FILE}", flush=True)
                    _ytmusic = YTMusic(OAUTH_FILE,
                                       oauth_credentials=oauth_credentials())
                elif method == "browser":
                    print(f"[ytm] using account auth from {AUTH_FILE}", flush=True)
                    _ytmusic = YTMusic(AUTH_FILE)
                else:
                    print("[ytm] no auth configured — running unauthenticated "
                          "(public search only, no library/playlists)", flush=True)
                    _ytmusic = YTMusic()
    return _ytmusic


def reset_ytmusic():
    """Drop the cached client so the next call rebuilds with current auth."""
    global _ytmusic
    with _ytmusic_lock:
        _ytmusic = None


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


@app.get("/favicon.ico")
def favicon():
    return Response(status=204)


# --- Google account connect (OAuth device flow) ---------------------------- #
# Single-user app, so one in-flight flow is enough.
_auth_flow: dict = {}
_auth_lock = threading.Lock()


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({"method": auth_method(),
                    "configured": oauth_credentials() is not None})


@app.post("/api/auth/start")
def api_auth_start():
    creds = oauth_credentials()
    if not creds:
        return jsonify({"error": "OAuth is not configured. Set GOOGLE_CLIENT_ID "
                        "and GOOGLE_CLIENT_SECRET (a 'TVs and Limited Input "
                        "devices' client) in the environment and restart."}), 400
    try:
        code = creds.get_code()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"could not start login: {exc}"}), 400
    with _auth_lock:
        _auth_flow.clear()
        _auth_flow["device_code"] = code["device_code"]
    url = code.get("verification_url", "https://www.google.com/device")
    return jsonify({
        "user_code": code.get("user_code"),
        "verification_url": url,
        "interval": code.get("interval", 5),
        "expires_in": code.get("expires_in", 1800),
    })


@app.post("/api/auth/poll")
def api_auth_poll():
    creds = oauth_credentials()
    # Hold the lock across the whole exchange so concurrent polls serialize: the
    # first one redeems the (single-use) device code; any others then see it
    # cleared and return "idle" instead of redeeming it again and getting the
    # spurious invalid_grant that would clobber the success.
    with _auth_lock:
        device_code = _auth_flow.get("device_code")
        if not creds or not device_code:
            return jsonify({"status": "idle"})
        try:
            raw = creds.token_from_code(device_code)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"status": "error", "error": str(exc)})
        if raw.get("access_token"):
            # Google adds fields (e.g. refresh_token_expires_in) that
            # RefreshingToken's constructor rejects, so keep only what it wants.
            allowed = {"access_token", "refresh_token", "scope",
                       "token_type", "expires_in"}
            token = RefreshingToken(
                credentials=creds,
                **{k: v for k, v in raw.items() if k in allowed},
            )
            token.store_token(OAUTH_FILE)
            _auth_flow.clear()
            reset_ytmusic()
            print(f"[ytm] OAuth token stored at {OAUTH_FILE}", flush=True)
            return jsonify({"status": "connected"})
        err = raw.get("error")
        if err in ("authorization_pending", "slow_down"):
            return jsonify({"status": "pending"})
        # Hard error (expired/invalid code) — drop it so we stop hammering and
        # the user can cleanly start over.
        _auth_flow.clear()
        return jsonify({"status": "error", "error": err or "unknown error"})


@app.post("/api/auth/logout")
def api_auth_logout():
    if os.path.exists(OAUTH_FILE):
        os.remove(OAUTH_FILE)
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
        return jsonify({"error": f"library needs account auth: {exc}"}), 400
    return jsonify([t for t in (normalize_track(s) for s in songs) if t])


@app.get("/api/playlists")
def api_playlists():
    try:
        pls = get_ytmusic().get_library_playlists(limit=100)
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
    else:
        print(f"[ytdlp] no cookies file at {COOKIES_FILE} — playback works "
              "until YouTube demands a 'not a bot' check")
    print(f"[musicbridge] serving on :{PORT}, FIFO={SNAPFIFO}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
