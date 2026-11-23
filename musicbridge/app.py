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

import http.cookiejar
import itertools
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

import requests
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
# Snapcast JSON-RPC control endpoint (host:port). Reachable over the Docker
# network by the snapserver container name.
SNAPSERVER_CONTROL = os.environ.get("SNAPSERVER_CONTROL", "snapserver:1705")

# yt-dlp tuning. Leave the player client to yt-dlp's own auto-fallback (it lands
# on android_vr, which serves plain audio without a JS runtime / PO token);
# forcing a client list can exclude the one that actually works. `bestaudio/best`
# falls back to a combined stream if no audio-only format is offered.
YTDLP_FORMAT = os.environ.get("YTDLP_FORMAT", "bestaudio/best")
YTDLP_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT", "")
# Crucial: a *logged-in* cookie makes YouTube force SABR streaming and skip the
# android_vr client, leaving NO downloadable audio (yt-dlp issue #12482).
# Anonymous extraction uses android_vr and works, so by default we do NOT hand
# the cookie to yt-dlp (it's still used for the ytmusicapi account side). Flip to
# 1 only if extraction starts failing with "confirm you're not a bot".
YTDLP_USE_COOKIES = os.environ.get("YTDLP_USE_COOKIES", "0") == "1"
# Tracks you uploaded to YouTube Music only play through YouTube Music's own
# client (web_music, logged in) — youtube.com answers "Video unavailable" — and
# that client needs a PO token, which the bgutil-provider container generates.
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "")

# Cookie keeper (see the "Cookie keeper" section below).
# When the nightly check + refresh runs, in the container's local time (set TZ).
COOKIE_REFRESH_AT = os.environ.get("COOKIE_REFRESH_AT", "04:00")
# What a signed-in YouTube page does every ~10 min to rotate the short-lived
# __Secure-*PSIDTS session tokens: load RotateCookiesPage (hidden iframe) for a
# one-time token, then POST it to RotateCookies. Undocumented; overridable for tests.
COOKIE_ROTATE_PAGE_URL = os.environ.get(
    "COOKIE_ROTATE_PAGE_URL",
    "https://accounts.youtube.com/RotateCookiesPage?origin=https://www.youtube.com&yt_pid=1")
COOKIE_ROTATE_URL = os.environ.get("COOKIE_ROTATE_URL",
                                   "https://accounts.youtube.com/RotateCookies")
# Google answers bare scripted requests differently; look like the browser it is.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
# Google answers a second rotation within ~10 min with 429, so never rotate more
# often than this — protects against restart loops.
COOKIE_ROTATE_MIN_INTERVAL = int(os.environ.get("COOKIE_ROTATE_MIN_INTERVAL", "900"))
# Optional: POSTed as JSON when the cookie stops working (e.g. a Home Assistant
# webhook trigger URL — see README).
NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "")
COOKIE_STATE_FILE = os.environ.get(
    "COOKIE_STATE_FILE",
    os.path.join(os.path.dirname(COOKIES_FILE) or ".", "cookie_state.json"))

# Snapcast stream format — MUST match snapserver.conf (sampleformat=48000:16:2).
SAMPLE_RATE = "48000"
CHANNELS = "2"

# --------------------------------------------------------------------------- #
# YouTube Music account — cookie-based browser auth
# --------------------------------------------------------------------------- #

_ytmusic: Optional[YTMusic] = None
_ytmusic_lock = threading.Lock()


def write_atomic(path: str, text: str):
    """Write via a temp file + rename, so a reader (ytmusicapi, yt-dlp, a crash
    mid-write) never sees a half-written cookie or browser.json."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
    write_atomic(AUTH_FILE, json.dumps(headers))
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
    global _ytmusic, _own_channel
    with _ytmusic_lock:
        _ytmusic = None
        _own_channel = None             # a new cookie may be a different account


# The account's own YouTube channel id, found lazily (see own_playlists()).
_own_channel: Optional[str] = None


def own_playlists(ytm: YTMusic, library: list) -> list:
    """The account's own public playlists that YouTube Music's library list leaves
    out. YTM's library doesn't include every playlist you own (e.g. some created on
    youtube.com), but your channel page lists them all — so merge those in.

    The channel id comes from any owned playlist in the library (get_playlist
    marks owned ones and names their author)."""
    global _own_channel
    if _own_channel is None:
        for p in library:
            pid = p.get("playlistId") or ""
            if not pid.startswith("PL"):        # skip Liked Music, episodes, recaps
                continue
            try:
                d = ytm.get_playlist(pid, limit=1)
            except Exception:  # noqa: BLE001
                continue
            author = d.get("author") or {}
            if d.get("owned") and author.get("id"):
                _own_channel = author["id"]
                break
        else:
            return []
    user = ytm.get_user(_own_channel)
    section = user.get("playlists") or {}
    if section.get("params"):
        return ytm.get_user_playlists(_own_channel, section["params"])
    return section.get("results") or []


def _transient(exc: Exception) -> bool:
    """Network trouble / rate limit / YouTube hiccup — says nothing about the cookie."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    msg = str(exc)
    return "HTTP 429" in msg or "HTTP 5" in msg


def account_probe() -> dict:
    """One signed-in read of the library: tells whether the cookie is a logged-in
    session, and is also the daily 'activity' the cookie keeper generates.

    authenticated: True (signed in) | False (signed out / expired) |
                   None (couldn't tell — network error, rate limit).
    """
    try:
        ytm = get_ytmusic()
        try:
            playlists = ytm.get_library_playlists(limit=50)
        except Exception as exc:  # noqa: BLE001
            if _transient(exc):
                raise
            playlists = []
        # get_liked_songs throws when the session is signed out; if it returns,
        # we're definitely authenticated.
        try:
            ytm.get_liked_songs(limit=1)
            liked_ok = True
        except Exception as exc:  # noqa: BLE001
            if _transient(exc):
                raise
            liked_ok = False
        authenticated = len(playlists) > 0 or liked_ok
        return {"authenticated": authenticated, "playlists": len(playlists)}
    except Exception as exc:  # noqa: BLE001
        return {"authenticated": None if _transient(exc) else False,
                "error": str(exc)[:200]}


# --------------------------------------------------------------------------- #
# Cookie keeper
#
# A browser stays signed in to YouTube indefinitely; an exported cookie sitting
# untouched on a server eventually doesn't (ours died after 7+ weeks). What a
# browser does that we didn't: use the session regularly, and keep the
# short-lived __Secure-*PSIDTS tokens rotated (RotateCookies), storing what
# Google sends back. So once at startup and once a night we do the same:
# rotate, persist the new values, then one signed-in library read. Whether
# that extends the session's lifetime isn't publicly proven — hence the state
# file + log lines (to measure it) and the webhook alert (so an expiry is
# noticed the morning it happens, not when you want music).
#
# Playback never touches any of this: yt-dlp streams without the cookie by
# default, and the nightly run waits while something is playing anyway.
# --------------------------------------------------------------------------- #

_cookie_lock = threading.RLock()     # serialises every cookies.txt read-modify-write


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _parse_ts(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _load_state() -> dict:
    try:
        with open(COOKIE_STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if os.path.exists(COOKIES_FILE) and not state.get("uploaded_at"):
        # Cookie predates the keeper: its mtime is the upload time (we never
        # rewrote it before), so the age stays measurable.
        mtime = datetime.fromtimestamp(os.path.getmtime(COOKIES_FILE)).astimezone()
        state["uploaded_at"] = mtime.replace(microsecond=0).isoformat()
        state.setdefault("status", "unknown")
    return state


_cookie_state: dict = _load_state()


def _save_state():
    try:
        write_atomic(COOKIE_STATE_FILE, json.dumps(_cookie_state, indent=2))
    except OSError as exc:
        print(f"[cookie] cannot save {COOKIE_STATE_FILE}: {exc}", flush=True)


def _cookie_age(at: Optional[datetime] = None) -> str:
    up = _parse_ts(_cookie_state.get("uploaded_at"))
    if not up:
        return "unknown age"
    secs = ((at or _now()) - up).total_seconds()
    return f"{secs / 86400:.1f} days old"


def merge_cookies(cookies: Iterable[http.cookiejar.Cookie]) -> int:
    """Write updated cookie values into cookies.txt; returns how many changed.

    Matches lines on (domain without leading dot, name) and keeps everything
    else — order, #HttpOnly_ prefixes, cookies we got no update for — as is.
    Only cookies sent to music.youtube.com are taken (a host-only cookie for
    accounts.youtube.com must not shadow a same-named .youtube.com one).
    """
    updates = {}
    for c in cookies:
        dom = (c.domain or "").lstrip(".")
        if dom in ("youtube.com", "music.youtube.com") and c.value is not None:
            updates[(dom, c.name)] = c
    if not updates:
        return 0
    with _cookie_lock:
        with open(COOKIES_FILE) as f:
            lines = f.read().splitlines()
        seen, changed = set(), 0
        for i, line in enumerate(lines):
            prefix = ""
            if line.startswith("#HttpOnly_"):
                prefix, line = "#HttpOnly_", line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            key = (parts[0].lstrip("."), parts[5])
            c = updates.get(key)
            if c is None:
                continue
            seen.add(key)
            new_exp = str(int(c.expires)) if c.expires else parts[4]
            if parts[6] == c.value and parts[4] == new_exp:
                continue
            parts[6], parts[4] = c.value, new_exp
            lines[i] = prefix + "\t".join(parts)
            changed += 1
        for key, c in updates.items():
            if key in seen:
                continue
            httponly = c.has_nonstandard_attr("HttpOnly") or c.has_nonstandard_attr("httponly")
            lines.append(("#HttpOnly_" if httponly else "") + "\t".join([
                c.domain, "TRUE" if c.domain.startswith(".") else "FALSE",
                c.path or "/", "TRUE" if c.secure else "FALSE",
                str(int(c.expires or 0)), c.name, c.value]))
            changed += 1
        if changed:
            write_atomic(COOKIES_FILE, "\n".join(lines) + "\n")
        return changed


def _set_cookies(resp: requests.Response) -> list:
    """The response's Set-Cookie headers as Cookie objects. Parsed by hand: the
    stdlib jar silently drops cookies its domain policy dislikes, and
    SimpleCookie truncates values containing '/' (SAPISID has them)."""
    host = requests.utils.urlparse(resp.url).hostname or ""
    out = []
    for header in resp.raw.headers.getlist("Set-Cookie"):
        first, *attrs = header.split(";")
        name, eq, value = first.strip().partition("=")
        if not eq or not name:
            continue
        a = {}
        for attr in attrs:
            k, _, v = attr.strip().partition("=")
            a[k.lower()] = v.strip()
        expires = None
        if a.get("max-age", "").lstrip("-").isdigit():
            expires = int(time.time()) + int(a["max-age"])
        elif a.get("expires"):
            expires = http.cookiejar.http2time(a["expires"])
        if expires is not None and expires <= time.time():
            continue                  # a deletion — keep what we have
        domain = a.get("domain", "")
        domain = ("." + domain.lstrip(".")) if domain else host
        out.append(http.cookiejar.Cookie(
            0, name, value.strip(), None, False, domain, bool(a.get("domain")),
            domain.startswith("."), a.get("path") or "/", "path" in a,
            "secure" in a, expires, False, None, None,
            {"HttpOnly": ""} if "httponly" in a else {}))
    return out


def rotate_cookies() -> str:
    """Ask Google for fresh __Secure-*PSIDTS tokens the way an open YouTube tab
    does, and store them. Returns 'ok …' | 'rejected …' | 'error: …'.

    1. GET RotateCookiesPage with the session -> HTML holding
       init('<token>', m, o, l, interval)
    2. POST [m, token, o, l] (zeros as null) to RotateCookies -> Set-Cookie.
    A bare POST without the token gets 403 even for a valid session."""
    cookies = parse_cookies_txt(COOKIES_FILE)
    header = lambda: "; ".join(f"{k}={v}" for k, v in cookies.items())  # noqa: E731
    base = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    changed = 0

    def absorb(resp) -> int:
        got = _set_cookies(resp)
        # Send them on (incl. host-only accounts.youtube.com ones, like a
        # browser would); merge_cookies persists only the .youtube.com ones.
        cookies.update({c.name: c.value for c in got
                        if c.domain.lstrip(".").endswith("youtube.com")})
        return merge_cookies(got)

    try:
        page = requests.get(COOKIE_ROTATE_PAGE_URL, timeout=20, headers={
            **base, "Accept": "text/html,*/*", "Referer": "https://www.youtube.com/",
            "Cookie": header()})
        if page.status_code in (401, 403):
            return f"rejected (page HTTP {page.status_code})"
        if not page.ok:
            return f"error: page HTTP {page.status_code}"
        changed += absorb(page)
        m = re.search(r"""init\(\s*['"]([^'"]+)['"]\s*,\s*([-\d.eE]+)\s*,"""
                      r"""\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)""", page.text)
        if not m:
            return "error: no rotation token in RotateCookiesPage"

        def num(x):
            v = float(x)
            return None if v == 0 else (int(v) if v.is_integer() else v)

        body = [num(m.group(2)), m.group(1), num(m.group(3)), num(m.group(4))]
        resp = requests.post(COOKIE_ROTATE_URL, data=json.dumps(body), timeout=20, headers={
            **base, "Accept": "*/*", "Content-Type": "application/json",
            "Origin": "https://accounts.youtube.com",
            "Referer": "https://accounts.youtube.com/RotateCookiesPage",
            "Cookie": header()})
    except requests.RequestException as exc:
        return f"error: {exc.__class__.__name__}: {exc}"[:200]
    finally:
        if changed:
            build_browser_json()
            reset_ytmusic()
    if resp.status_code in (401, 403):
        return f"rejected (HTTP {resp.status_code})"
    if not resp.ok:
        return f"error: HTTP {resp.status_code}"
    n = absorb(resp)
    changed += n
    if n:
        build_browser_json()
        reset_ytmusic()
    _cookie_state["rotated_at"] = _now().isoformat()
    _cookie_state["rotations"] = _cookie_state.get("rotations", 0) + 1
    return f"ok ({changed} cookie(s) updated)"


def notify(event: str, message: str):
    if not NOTIFY_WEBHOOK_URL:
        return
    try:
        requests.post(NOTIFY_WEBHOOK_URL, timeout=15, json={
            "event": event, "title": "Multiroom", "message": message})
        print(f"[cookie] notified webhook: {event}", flush=True)
    except requests.RequestException as exc:
        print(f"[cookie] webhook failed: {exc}", flush=True)


def maintain_cookie(reason: str, rotate: bool = True) -> dict:
    """Rotate (if due) + one signed-in read; record and log the outcome, and
    alert once when the cookie turns out to be dead."""
    with _cookie_lock:
        if not os.path.exists(COOKIES_FILE):
            _cookie_state.clear()
            _cookie_state["status"] = "none"
            _save_state()
            return dict(_cookie_state)

        if not os.path.exists(AUTH_FILE):
            build_browser_json()
        rot = "skipped"
        if rotate:
            last = _parse_ts(_cookie_state.get("rotated_at"))
            if last and (_now() - last).total_seconds() < COOKIE_ROTATE_MIN_INTERVAL:
                rot = "skipped (rotated recently)"
            else:
                rot = rotate_cookies()

        probe = account_probe()
        now = _now()
        prev = _cookie_state.get("status")
        _cookie_state["checked_at"] = now.isoformat()
        if probe.get("authenticated"):
            status = "ok"
            _cookie_state["ok_at"] = now.isoformat()
            _cookie_state["playlists"] = probe.get("playlists", 0)
            _cookie_state.pop("expired_at", None)
            _cookie_state.pop("error", None)
        elif probe.get("authenticated") is None and not rot.startswith("rejected"):
            status = prev if prev in ("ok", "expired") else "unknown"
            _cookie_state["error"] = probe.get("error")
        else:
            status = "expired"
            _cookie_state.setdefault("expired_at", now.isoformat())
            _cookie_state["error"] = probe.get("error") or "signed out"
        _cookie_state["status"] = status
        _save_state()

        print(f"[cookie] {reason}: rotate={rot}; session={status.upper()}"
              f"{' (' + str(probe.get('playlists')) + ' playlists)' if status == 'ok' else ''}"
              f"; cookie {_cookie_age(now)}"
              f"{'; ' + str(_cookie_state.get('error')) if status != 'ok' else ''}",
              flush=True)
        # Alert on the transition only — and not for a dead cookie just uploaded
        # (the Account sheet already says so).
        if status == "expired" and prev != "expired" and reason != "upload":
            ok_at = _parse_ts(_cookie_state.get("ok_at"))
            notify("cookie_expired",
                   "YouTube Music cookie stopped working "
                   f"({_cookie_age(now)}; last OK {ok_at:%d %b %H:%M})." if ok_at else
                   f"YouTube Music cookie stopped working ({_cookie_age(now)}).")
            print(f"[cookie] ⚠ EXPIRED — {_cookie_age(now)}, last OK "
                  f"{_cookie_state.get('ok_at') or 'never'}; upload a fresh one "
                  "via Account", flush=True)
        return dict(_cookie_state)


def next_refresh_at(after: Optional[datetime] = None) -> datetime:
    after = after or _now()
    try:
        hh, mm = (int(x) for x in COOKIE_REFRESH_AT.split(":", 1))
    except ValueError:
        hh, mm = 4, 0
    target = after.replace(hour=hh, minute=mm, second=0)
    if target <= after:
        target += timedelta(days=1)
    return target


def cookie_status() -> dict:
    """Cookie health for the UI."""
    s = dict(_cookie_state)
    s.setdefault("status", "none" if not os.path.exists(COOKIES_FILE) else "unknown")
    if os.path.exists(COOKIES_FILE):
        s["next_check_at"] = next_refresh_at().isoformat()
    return s


def _cookie_keeper():
    time.sleep(3)                    # let the web server come up first
    try:
        maintain_cookie("startup")
    except Exception as exc:  # noqa: BLE001 — never let the keeper die
        print(f"[cookie] startup check failed: {exc}", flush=True)
    while True:
        target = next_refresh_at()
        # Sleep in short slices: robust against suspend/clock jumps.
        while _now() < target:
            time.sleep(min(60, max(1, (target - _now()).total_seconds())))
        # Nobody listens at night — but if something *is* playing, wait for it
        # to stop (up to 3 h; the check is harmless to playback regardless).
        deadline = target + timedelta(hours=3)
        if engine.current is not None:
            print("[cookie] nightly check: music is playing — waiting", flush=True)
        while engine.current is not None and _now() < deadline:
            time.sleep(60)
        try:
            maintain_cookie("nightly")
        except Exception as exc:  # noqa: BLE001 — never let the keeper die
            print(f"[cookie] nightly check failed: {exc}", flush=True)


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
    loading: bool = False                            # track picked but no audio yet
    # Bumped on every explicit "start playing" (play a track/list, resume) — not
    # when the queue advances by itself. The TV app opens its Now Playing screen
    # when this changes, so dismissing it sticks until you start something new.
    play_seq: int = 0
    _ytdlp: Optional[subprocess.Popen] = None
    _ffmpeg: Optional[subprocess.Popen] = None
    _stop: bool = False
    _skipped: bool = False
    _natural_end: bool = False                       # last track finished on its own
    _started: float = 0.0                            # monotonic start of current track
    _paused_at: float = 0.0                          # monotonic time pause began
    _paused_accum: float = 0.0                       # total paused seconds this track
    _got_audio: bool = False                         # this attempt produced audio

    def __post_init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        ensure_fifo()
        threading.Thread(target=self._run, daemon=True).start()

    # -- public controls ---------------------------------------------------- #

    def play_now(self, track: dict):
        with self._wake:
            self.play_seq += 1
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
            self.play_seq += 1
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
                self.play_seq += 1
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
                "loading": self.loading,
                "elapsed": round(self._elapsed(), 1),
                "play_seq": self.play_seq,
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
                    self.loading = False
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
                self.loading = True          # cleared once ffmpeg emits audio
                track = nxt

            self._play_blocking(track)

            with self._wake:
                # Did the track finish on its own (vs. skip/prev/stop)?
                self._natural_end = not (self._skipped or self._stop)

    def _play_blocking(self, track: dict):
        got_audio = self._attempt(track, with_cookies=YTDLP_USE_COOKIES)
        # Your own uploads (and other private/age-gated videos) only open with
        # your login; the cookie-free path can't fetch them. Retry those once with
        # the cookie — but only then, since a logged-in session pushes normal
        # songs onto streams yt-dlp can't download.
        if (not got_audio and not self._skipped and not self._stop
                and os.path.exists(COOKIES_FILE)):
            print("[player] ↻ no audio — retrying as YouTube Music, logged in "
                  "(needed for your uploads)", flush=True)
            self.loading = True
            self._attempt(track, with_cookies=True, music_client=True)

    def _attempt(self, track: dict, with_cookies: bool,
                 music_client: bool = False) -> bool:
        """One yt-dlp -> ffmpeg -> FIFO run. Returns whether audio came out.

        music_client: ask as YouTube Music (music.youtube.com + web_music client,
        PO token from POT_PROVIDER_URL) — the only way to get your uploads."""
        self._got_audio = False
        video_id = track["videoId"]
        ytdlp_cmd = ["yt-dlp", "-f", YTDLP_FORMAT, "-o", "-",
                     "--quiet", "--no-warnings", "--no-progress"]
        if music_client:
            url = f"https://music.youtube.com/watch?v={video_id}"
            ytdlp_cmd += ["--extractor-args", "youtube:player_client=web_music"]
            if POT_PROVIDER_URL:
                ytdlp_cmd += ["--extractor-args",
                              f"youtubepot-bgutilhttp:base_url={POT_PROVIDER_URL}"]
        else:
            # www.youtube.com extracts more reliably than music.youtube.com here.
            url = f"https://www.youtube.com/watch?v={video_id}"
            if YTDLP_PLAYER_CLIENT:
                ytdlp_cmd += ["--extractor-args",
                              f"youtube:player_client={YTDLP_PLAYER_CLIENT}"]
        # yt-dlp rewrites its --cookies file on exit. Give it a private copy (so it
        # can't race the cookie keeper or an upload) and merge back what it got.
        jar_copy = None
        if with_cookies and os.path.exists(COOKIES_FILE):
            fd, jar_copy = tempfile.mkstemp(prefix="ytdlp-cookies-", suffix=".txt")
            os.close(fd)
            with _cookie_lock:
                shutil.copyfile(COOKIES_FILE, jar_copy)
            ytdlp_cmd += ["--cookies", jar_copy]
        ytdlp_cmd.append(url)

        started = time.monotonic()
        print(f"[player] ▶ {track.get('title', video_id)} ({video_id})"
              f"{' [with cookie]' if jar_copy else ''}"
              f"{' [YouTube Music client]' if music_client else ''}", flush=True)
        try:
            # stderr is inherited (not silenced) so yt-dlp/ffmpeg errors land in
            # `docker compose logs musicbridge`.
            self._ytdlp = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE)
            self._ffmpeg = subprocess.Popen(
                # -y: don't refuse to "overwrite" the FIFO, which always exists.
                # -progress pipe:1: machine-readable progress on stdout, which we
                # watch to know when audio actually starts flowing.
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", "pipe:0", "-f", "s16le",
                 "-ar", SAMPLE_RATE, "-ac", CHANNELS,
                 "-progress", "pipe:1", SNAPFIFO],
                stdin=self._ytdlp.stdout, stdout=subprocess.PIPE,
            )
            # Let ffmpeg own the read end so yt-dlp gets SIGPIPE if ffmpeg dies.
            self._ytdlp.stdout.close()
            threading.Thread(target=self._watch_progress,
                             args=(self._ffmpeg,), daemon=True).start()
            ff_rc = self._ffmpeg.wait()
            yt_rc = self._ytdlp.wait()
            elapsed = time.monotonic() - started
            print(f"[player] ■ ended after {elapsed:.1f}s "
                  f"(ffmpeg={ff_rc}, yt-dlp={yt_rc})", flush=True)
            if not self._got_audio and not self._skipped:
                if jar_copy:
                    print("[player] ⚠ no audio even with the cookie — yt-dlp could "
                          "not get the stream (see its error above).", flush=True)
                elif not os.path.exists(COOKIES_FILE):
                    print("[player] ⚠ no audio — yt-dlp could not get the stream "
                          "(see its error above). Private videos and your own "
                          "uploads need a cookie: upload one via Account.", flush=True)
        except Exception as exc:  # noqa: BLE001 — never let the worker die
            print(f"[player] error: {exc}", flush=True)
        finally:
            self.loading = False
            self._signal_procs(signal.SIGCONT)  # in case we were paused
            for p in (self._ytdlp, self._ffmpeg):
                if p and p.poll() is None:
                    try:
                        p.kill()
                    except ProcessLookupError:
                        pass
            self._ytdlp = self._ffmpeg = None
            if jar_copy:
                _merge_back_ytdlp_jar(jar_copy)
        return self._got_audio

    def _watch_progress(self, proc):
        """Clear `loading` once ffmpeg reports it's actually producing audio,
        then keep draining its progress output so it never blocks on a full pipe."""
        cleared = False
        try:
            # Must keep reading to EOF so ffmpeg never blocks on a full pipe;
            # per-line errors are swallowed so the drain loop never stops early.
            for raw in iter(proc.stdout.readline, b""):
                if cleared:
                    continue
                try:
                    if raw.startswith((b"out_time_us=", b"out_time_ms=")):
                        val = raw.split(b"=", 1)[1].strip()
                        if val.isdigit() and int(val.decode()) > 0:
                            self.loading = False
                            self._got_audio = True
                            cleared = True
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


def _merge_back_ytdlp_jar(path: str):
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        if os.path.exists(COOKIES_FILE) and merge_cookies(jar):
            build_browser_json()
            reset_ytmusic()
    except Exception as exc:  # noqa: BLE001 — best effort
        print(f"[ytdlp] could not merge back yt-dlp cookies: {exc}", flush=True)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


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
    return send_from_directory("static", "icon.svg", mimetype="image/svg+xml")


@app.get("/manifest.webmanifest")
def manifest():
    # "Add to Home screen" opens it full-screen, like an app.
    return send_from_directory("static", "manifest.webmanifest",
                               mimetype="application/manifest+json")


# --- Account (cookie upload) ----------------------------------------------- #


@app.get("/api/auth/status")
def api_auth_status():
    return jsonify({"method": auth_method(), "cookie": cookie_status()})


@app.post("/api/auth/check")
def api_auth_check():
    """'Check now' in the Account sheet: a signed-in read, no rotation."""
    maintain_cookie("manual check", rotate=False)
    return jsonify({"method": auth_method(), "cookie": cookie_status()})


@app.post("/api/cookies")
def api_cookies():
    """Receive an uploaded Netscape cookies.txt, store it, (re)build browser.json,
    and report whether it's a logged-in session."""
    raw = request.get_data(as_text=True) or ""
    if "\t" not in raw or "youtube" not in raw.lower():
        return jsonify({"ok": False,
                        "error": "That doesn't look like a cookies.txt "
                        "(expected Netscape/tab-separated YouTube cookies)."}), 400
    with _cookie_lock:
        write_atomic(COOKIES_FILE, raw)
        if not build_browser_json():
            return jsonify({"ok": False,
                            "error": "Saved, but the cookie is missing the values "
                            "needed for login (SAPISID). Re-export while logged in "
                            "to music.youtube.com."}), 400
        reset_ytmusic()
        _cookie_state.clear()
        _cookie_state["uploaded_at"] = _now().isoformat()
        print("[cookie] new cookie uploaded", flush=True)
        state = maintain_cookie("upload", rotate=False)
    return jsonify({"ok": True, "authenticated": state.get("status") == "ok",
                    "playlists": state.get("playlists", 0),
                    "cookie": cookie_status()})


@app.post("/api/auth/logout")
def api_auth_logout():
    with _cookie_lock:
        for path in (AUTH_FILE, COOKIES_FILE, COOKIE_STATE_FILE):
            if os.path.exists(path):
                os.remove(path)
        _cookie_state.clear()
        reset_ytmusic()
    return jsonify({"method": auth_method(), "cookie": cookie_status()})


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
    ytm = get_ytmusic()
    try:
        pls = ytm.get_library_playlists(limit=100)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"playlists need a cookie (Account): {exc}"}), 400
    try:
        seen = {p.get("playlistId") for p in pls}
        for extra in own_playlists(ytm, pls):
            if extra.get("playlistId") in seen:
                continue
            # The library is alphabetical after its specials (Liked Music,
            # episodes, recaps — ids not starting with PL): slot extras in there.
            key = (extra.get("title") or "").casefold()
            at = next((i for i, p in enumerate(pls)
                       if (p.get("playlistId") or "").startswith("PL")
                       and (p.get("title") or "").casefold() > key), len(pls))
            pls.insert(at, extra)
    except Exception as exc:  # noqa: BLE001 — the library list alone is still useful
        print(f"[ytm] could not add channel playlists: {exc}", flush=True)
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


# --------------------------------------------------------------------------- #
# Snapcast per-room volume (JSON-RPC over the control port)
# --------------------------------------------------------------------------- #

_rpc_id = itertools.count(1)


def snap_rpc(method: str, params: Optional[dict] = None, timeout: float = 4.0):
    """One JSON-RPC call to snapserver's control port; returns the result."""
    host, _, port = SNAPSERVER_CONTROL.partition(":")
    req_id = next(_rpc_id)
    req = {"id": req_id, "jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    with socket.create_connection((host, int(port or 1705)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(req) + "\r\n").encode())
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("snapserver closed the connection")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                # skip async notifications, wait for our matching response
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg.get("result")


def snap_clients() -> list:
    """Flatten Server.GetStatus into a list of clients with volume."""
    status = snap_rpc("Server.GetStatus")
    out = []
    for group in status.get("server", {}).get("groups", []):
        for c in group.get("clients", []):
            cfg = c.get("config", {})
            vol = cfg.get("volume", {})
            out.append({
                "id": c.get("id"),
                "name": cfg.get("name") or c.get("host", {}).get("name") or "Speaker",
                "percent": vol.get("percent", 0),
                "muted": vol.get("muted", False),
                "connected": c.get("connected", False),
            })
    return out


@app.get("/api/snapcast/clients")
def api_snap_clients():
    try:
        return jsonify(snap_clients())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"snapserver unreachable: {exc}"}), 502


@app.post("/api/snapcast/volume")
def api_snap_volume():
    body = request.get_json(force=True) or {}
    percent = max(0, min(100, int(body.get("percent", 0))))
    try:
        snap_rpc("Client.SetVolume", {"id": body.get("id"),
                 "volume": {"muted": bool(body.get("muted", False)), "percent": percent}})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


@app.post("/api/snapcast/volume_all")
def api_snap_volume_all():
    body = request.get_json(force=True) or {}
    percent = max(0, min(100, int(body.get("percent", 0))))
    try:
        for c in snap_clients():
            if c["connected"]:
                snap_rpc("Client.SetVolume", {"id": c["id"],
                         "volume": {"muted": False, "percent": percent}})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True})


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
        # Keep browser.json (account auth) in sync with the uploaded cookie.
        build_browser_json()
        print(f"[ytdlp] yt-dlp cookies: {'on' if YTDLP_USE_COOKIES else 'off'} "
              "(off avoids YouTube's SABR gating; cookie still powers the account)")
    else:
        print(f"[ytdlp] no cookie at {COOKIES_FILE} — upload one via the Account "
              "button for library/playlists")
    print(f"[cookie] keeper: check + refresh at startup and daily at "
          f"{COOKIE_REFRESH_AT} ({time.strftime('%Z')}); alerts "
          f"{'→ webhook' if NOTIFY_WEBHOOK_URL else 'off (NOTIFY_WEBHOOK_URL unset)'}",
          flush=True)
    print(f"[ytdlp] uploads/private tracks: retried via YouTube Music's client, "
          f"PO tokens from {POT_PROVIDER_URL or 'nowhere (POT_PROVIDER_URL unset)'}",
          flush=True)
    threading.Thread(target=_cookie_keeper, daemon=True).start()
    print(f"[musicbridge] serving on :{PORT}, FIFO={SNAPFIFO}")
    serve(app, host="0.0.0.0", port=PORT, threads=8)
