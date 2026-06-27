# youtube-cast-multiroom

Synchronized multiroom **YouTube Music** for homes whose speakers are Google TVs
(or anything that can run the Snapcast client), built on **musicbridge** + **Snapcast**.

## Why not just cast?

YouTube Music can't cast to a self-built receiver. First-party Google apps require
**Cast device authentication** backed by a certificate signed by Google's private
Cast root CA — fused into licensed hardware and not obtainable by individuals. And
Sony Bravia / Google TVs generally can't be added to Google Home **speaker groups**,
so the native multiroom path is closed too.

So instead of pretending to be a Cast target, **musicbridge** drives your YouTube
Music *account* directly and feeds the audio into Snapcast:

```
Phone browser ──► music.<domain>  (musicbridge web UI, served via Traefik)
                        │  ytmusicapi  → your library / playlists / search
                        │  yt-dlp      → audio stream for the chosen track
                        │  ffmpeg      → raw PCM (48000:16:2)
                        ▼
                  /snapfifo  ──►  snapserver  ──►  Google TV #1 + #2  (Snapcast app, in sync)
```

The trade-off: you control playback from the **musicbridge web UI**, not the native
YouTube Music app's Cast button. That's unavoidable — YouTube Music exposes no open
"connect" protocol, so the only way in is through your account via `ytmusicapi`.

## Components

| Service | Role |
|---|---|
| `musicbridge` | Web remote + player. Searches your YTM account, streams audio into the Snapcast FIFO. |
| `snapserver`  | Reads the FIFO and streams in sync to every Snapcast client. |

## Prerequisites

- **Docker** + **Docker Compose**.
- An existing **Traefik** reverse proxy that:
  - is attached to an external Docker network named **`traefik`** (both services join it —
    `docker network create traefik` if you don't have one), and
  - has a TLS cert resolver named **`letsencrypt`** and a **`websecure`** entrypoint.

  If your Traefik uses different names, edit the `traefik.*` labels in
  `docker-compose.yml` (cert resolver, entrypoint, network) to match. No Traefik at
  all? You can strip the labels and publish musicbridge's port `8080` directly instead.
- DNS records for `MUSIC_HOST` and `SNAPCAST_HOST` pointing at the host.

## Deploy

```bash
git clone https://github.com/YOUR_USERNAME/youtube-cast-multiroom
cd youtube-cast-multiroom
cp .env.example .env          # edit DATA_DIR, MUSIC_HOST, SNAPCAST_HOST
```

> **Security:** the default compose adds **no authentication** — fine on a trusted LAN.
> If you expose `MUSIC_HOST` to the internet, add a Traefik basic-auth middleware: set
> `MUSIC_AUTH` in `.env` (generate with `htpasswd -nbB user pass`, **doubling every `$`**
> to `$$`), then add these labels to the `musicbridge` service:
> ```yaml
> - "traefik.http.routers.music.middlewares=music-auth"
> - "traefik.http.middlewares.music-auth.basicauth.users=${MUSIC_AUTH}"
> ```

### 1. Start

```bash
docker compose up -d --build
```

Open `https://<MUSIC_HOST>` in a browser. **Search** and playback work immediately —
tap a track and audio starts on every connected Snapcast client (set up the Snapcast
app on each TV first — see *Install the Snapcast app* below). Adjust per-room volume
from the **speaker icon** in the now-playing bar ("All rooms" sets every TV to the
same level). **Library and Playlists** stay empty until you do step 2.

### 2. Connect your YouTube Music account (cookie upload)

Library and Playlists need your account, which musicbridge gets from a single
**`cookies.txt`** exported from a browser logged into YouTube Music. The same cookie
also lets yt-dlp get past YouTube's "are you a bot?" checks if needed — one file.

Upload it from the web UI's **Account** button (no SSH needed). Export from an
**incognito/private window** so Google doesn't rotate (and quickly invalidate) the
cookie:

1. Install the **"Get cookies.txt LOCALLY"** extension (Chrome/Brave web store).
2. Open an **incognito window** → <https://music.youtube.com> → log in (your avatar
   shows, your library is visible).
3. Click the extension → **Export** (current site).
4. **Close the incognito window without signing out.**
5. In musicbridge, tap **Account → Choose cookies.txt** and pick that file.

The page confirms how many playlists it found. If it says *"signed-out / 0
playlists"*, the export wasn't from a logged-in YouTube Music session — redo it.
The cookie is stored at `$DATA_DIR/musicbridge/cookies.txt`, and `browser.json`
(ytmusicapi browser auth) is generated from it automatically.

> Cookies still go stale eventually — when Library/Playlists empty out, re-export
> (incognito) and re-upload via **Account → Replace cookie**.
>
> Why cookies and not OAuth? YouTube Music has no usable official API for the
> library, and `ytmusicapi`'s OAuth path is currently broken server-side. The
> web client (cookie auth) is the only thing that returns your *full* library,
> including saved playlists.

## Install the Snapcast app on each Google TV

1. Install the **Snapcast** Android client (by badaix) — from the Play Store if
   available, otherwise sideload the APK from the [Snapcast releases](https://github.com/badaix/snapcast/releases).
2. Open it, add a server, and enter the **IP of the Docker host** (the snapserver
   container publishes ports `1704`/`1705`/`1780`, so the host must be reachable on
   your LAN).
3. Connect — the TV now plays whatever musicbridge is streaming, in sync.

Repeat on every TV. Set volume from musicbridge's **speaker icon**, the Snapcast app,
or the Snapweb UI at `https://<SNAPCAST_HOST>` (or `http://<host>:1780`).

## Updating snapserver

Version is pinned in `snapserver/Dockerfile` (`SNAPCAST_VERSION`):

```bash
docker compose build snapserver && docker compose up -d snapserver
```

To pick up a newer `yt-dlp` (YouTube changes break it periodically):

```bash
docker compose build musicbridge && docker compose up -d musicbridge
```

## Environment variables

| Variable | Description |
|---|---|
| `DATA_DIR` | Host path for persistent data (`snapfifo`, `musicbridge/cookies.txt` + generated `browser.json`). |
| `MUSIC_HOST` | Hostname Traefik routes to the music web UI. |
| `SNAPCAST_HOST` | Hostname Traefik routes to the Snapweb UI. |
| `MUSIC_AUTH` | *Optional* — `htpasswd` `user:hash` (double every `$`). Only used if you add the basic-auth middleware (see Security). |
| `SNAPSERVER_CONTROL` | *Optional* — Snapcast control `host:port` for the Volume panel (default `snapserver:1705`). |

musicbridge also reads a few optional yt-dlp tuning vars (rarely needed):
`YTDLP_FORMAT` (default `bestaudio/best`), `YTDLP_PLAYER_CLIENT` (default empty),
and `YTDLP_USE_COOKIES` (default `0`).

## Troubleshooting

- **Library/Playlists empty** — the cookie isn't a logged-in session (or went
  stale). Re-export from an **incognito** music.youtube.com tab and re-upload via
  **Account**. The upload reports the playlist count so you know it worked.
- **A track fails with "Requested format is not available"** — handled by default
  (the image bundles the `deno` JS runtime and yt-dlp runs *without* the cookie, so
  YouTube doesn't force SABR-only streams). If you instead see **"confirm you're not
  a bot"**, set `YTDLP_USE_COOKIES=1` and `docker compose up -d musicbridge`.
- **Playback breaks after a YouTube change** — rebuild musicbridge to pull the
  latest yt-dlp: `docker compose build musicbridge && docker compose up -d musicbridge`.
- **Volume panel says "snapserver unreachable"** — set `SNAPSERVER_CONTROL` to your
  snapserver's `host:port` if it isn't the default `snapserver:1705`.
