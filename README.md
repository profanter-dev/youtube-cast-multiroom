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
| `bgutil-provider` | Generates YouTube "PO tokens" for YouTube Music's own client — needed only for tracks you **uploaded** to YouTube Music. Private network, no ports. |

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

Both images are published to GHCR, so the host runs **pull-only** — no clone, no
build. It needs exactly two files:

```bash
mkdir -p /opt/youtube-cast-multiroom && cd /opt/youtube-cast-multiroom
curl -O  https://raw.githubusercontent.com/profanter-dev/youtube-cast-multiroom/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/profanter-dev/youtube-cast-multiroom/main/.env.example
# edit .env — DATA_DIR, MUSIC_HOST, SNAPCAST_HOST
```

Both packages are **public**, so the host needs no registry login — `docker compose
pull` works anonymously. (They contain no secrets; your cookie lives in `$DATA_DIR`,
never in an image.)

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
docker compose pull && docker compose up -d
```

Open `https://<MUSIC_HOST>` in a browser. **Search** and playback work immediately —
tap a track and audio starts on every connected Snapcast client (set up the Snapcast
app on each TV first — see *Install the Snapcast app* below). Tap the mini player to
open the full player; adjust per-room volume from its **speaker icon** ("All rooms"
sets every TV to the same level). **Liked and Playlists** stay empty until you do
step 2.

The UI is built for phones: tabs at the bottom, and the URL always holds the open
tab, search and playlist, so a reload (or a bookmark) lands in the same spot — scroll
position included. On Android/iOS use **Add to Home screen** to open it full-screen
like an app.

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
5. In musicbridge, tap the **account icon** (top right) → **Upload cookies.txt** and
   pick that file.

The page confirms how many playlists it found. If it says *"signed-out / 0
playlists"*, the export wasn't from a logged-in YouTube Music session — redo it.
The cookie is stored at `$DATA_DIR/musicbridge/cookies.txt`, and `browser.json`
(ytmusicapi browser auth) is generated from it automatically.

#### Keeping the cookie alive

A browser stays signed in for good; an exported cookie left untouched eventually
gets signed out (one lasted 7+ weeks here). musicbridge therefore behaves like a
browser that's opened once a day: **at startup and every night at
`COOKIE_REFRESH_AT`** (default `04:00`, container-local time — set `TZ`) it

1. asks Google to rotate the short-lived session tokens (`RotateCookies`, what an open
   YouTube tab does every ~10 min) and writes the new values back into `cookies.txt`,
2. reads your library once with the cookie — and records whether that worked.

Playback is never affected: yt-dlp streams without the cookie, and the nightly run
waits while music is playing. Whether this extends the cookie's life isn't publicly
proven, so musicbridge also **tells you when it dies**: the account icon gets a
red dot, the Account sheet shows the cookie's age and last good check, and — if you
set `NOTIFY_WEBHOOK_URL` — it POSTs `{"event": "cookie_expired", "title", "message"}`
once, e.g. to a Home Assistant automation:

```yaml
# Home Assistant automation (Settings → Automations → ⋮ → Edit in YAML)
alias: Multiroom cookie expired
triggers:
  - trigger: webhook
    webhook_id: multiroom-cookie       # → NOTIFY_WEBHOOK_URL=http://<ha-host>:8123/api/webhook/multiroom-cookie
    allowed_methods: [POST]
    local_only: true
actions:
  - action: notify.notify               # or notify.mobile_app_<your_phone>
    data:
      title: "{{ trigger.json.title }}"
      message: "{{ trigger.json.message }}"
```

Every check is logged (`docker logs musicbridge | grep cookie`) with the cookie's age,
and kept in `$DATA_DIR/musicbridge/cookie_state.json`. When it does expire, re-export
(incognito) and use **Account → Replace cookie**.

> Why cookies and not OAuth? YouTube Music has no usable official API for the
> library, and `ytmusicapi`'s OAuth path is currently broken server-side. The
> web client (cookie auth) is the only thing that returns your *full* library,
> including saved playlists.

## Install the Snapcast app on each Google TV

**Recommended:** the **Multiroom** TV app in [`android/`](android/) — a Snapdroid
fork that plays the stream like the official app and also shows a full-screen
*Now Playing* (artwork, title, progress) that opens by itself when music starts.
See [`android/README.md`](android/README.md) for building and installing it via adb.

Or use the official app:

1. Install the **Snapcast** Android client (by badaix) — from the Play Store if
   available, otherwise sideload the APK from the [Snapcast releases](https://github.com/badaix/snapcast/releases).
2. Open it, add a server, and enter the **IP of the Docker host** (the snapserver
   container publishes ports `1704`/`1705`/`1780`, so the host must be reachable on
   your LAN).
3. Connect — the TV now plays whatever musicbridge is streaming, in sync.

Repeat on every TV. Set volume from musicbridge's **speaker icon**, the Snapcast app,
or the Snapweb UI at `https://<SNAPCAST_HOST>` (or `http://<host>:1780`).

## Publishing images to GHCR

There is no CI — images are built and pushed by hand from a dev machine.

1. Create a GitHub Personal Access Token (classic, or fine-grained with Packages
   access) with the `write:packages` and `read:packages` scopes.
2. Log in and push, from the repo root:

```bash
echo "$GITHUB_PAT" | docker login ghcr.io -u profanter-dev --password-stdin

docker build -t ghcr.io/profanter-dev/youtube-cast-multiroom-musicbridge:latest ./musicbridge
docker push       ghcr.io/profanter-dev/youtube-cast-multiroom-musicbridge:latest

docker build -t ghcr.io/profanter-dev/youtube-cast-multiroom-snapserver:latest ./snapserver
docker push       ghcr.io/profanter-dev/youtube-cast-multiroom-snapserver:latest
```

To keep a rollback target, also tag the same build with the date and push it. To
roll back, point the `image:` line in the host's `docker-compose.yml` at that tag
instead of `:latest`:

```bash
docker tag  ghcr.io/profanter-dev/youtube-cast-multiroom-musicbridge:latest \
            ghcr.io/profanter-dev/youtube-cast-multiroom-musicbridge:$(date +%Y%m%d)
docker push ghcr.io/profanter-dev/youtube-cast-multiroom-musicbridge:$(date +%Y%m%d)
```

> **Architecture:** musicbridge is **linux/amd64 only** — the `deno` download in its
> Dockerfile is a hardcoded x86_64 build. Build it on an amd64 machine, or pass
> `--platform linux/amd64`. (snapserver resolves its arch from `TARGETARCH`, falling
> back to the build host's own architecture when BuildKit isn't in play.)

> **Visibility:** both packages are public, so hosts pull without a login. GHCR
> creates *new* packages as private regardless of the repository's visibility and
> never inherits it, so if you ever push under a new image name, flip it at
> `github.com/users/profanter-dev/packages` → the package → *Package settings* →
> *Danger Zone* → *Change visibility*.

Pushing links each package to this repository automatically, via the
`org.opencontainers.image.source` label in both Dockerfiles — that's what makes them
show up in the repo's **Packages** sidebar.

Then, on the host:

```bash
docker compose pull && docker compose up -d
```

## Updating

Everything is a rebuild-and-push from the dev machine followed by a `pull` on the
host — the host never builds anything.

- **Newer `yt-dlp`** (YouTube changes break it periodically) — rebuild and push
  `musicbridge`; the image installs the latest at build time.
- **Newer snapserver** — bump `SNAPCAST_VERSION` in `snapserver/Dockerfile`, then
  rebuild and push `snapserver`.
- **Changed `snapserver.conf`** — it is baked into the image, so this also needs a
  rebuild and push (or uncomment the bind-mount in `docker-compose.yml` if you have a
  checkout on the host).

## Environment variables

| Variable | Description |
|---|---|
| `DATA_DIR` | Host path for persistent data (`snapfifo`, `musicbridge/cookies.txt` + generated `browser.json` and `cookie_state.json`). |
| `MUSIC_HOST` | Hostname Traefik routes to the music web UI. |
| `SNAPCAST_HOST` | Hostname Traefik routes to the Snapweb UI. |
| `MUSIC_AUTH` | *Optional* — `htpasswd` `user:hash` (double every `$`). Only used if you add the basic-auth middleware (see Security). |
| `SNAPSERVER_CONTROL` | *Optional* — Snapcast control `host:port` for the Volume panel (default `snapserver:1705`). |
| `TZ` | *Optional* — container timezone, e.g. `Europe/Berlin` (default `UTC`). The nightly cookie check uses local time. |
| `COOKIE_REFRESH_AT` | *Optional* — `HH:MM` of the nightly cookie check + refresh (default `04:00`). |
| `NOTIFY_WEBHOOK_URL` | *Optional* — URL that gets a JSON POST when the cookie stops working (see *Keeping the cookie alive*). |

musicbridge also reads a few optional yt-dlp tuning vars (rarely needed):
`YTDLP_FORMAT` (default `bestaudio/best`), `YTDLP_PLAYER_CLIENT` (default empty),
and `YTDLP_USE_COOKIES` (default `0`).

## Troubleshooting

- **Library/Playlists empty / red dot on the account icon** — the cookie isn't a
  logged-in session (or expired). Re-export from an **incognito** music.youtube.com
  tab and re-upload via **Account**. The upload reports the playlist count so you
  know it worked. Don't keep that incognito window open: a browser still using the
  same session rotates it and signs the server's copy out within minutes.
- **A track fails with "Requested format is not available"** — handled by default
  (the image bundles the `deno` JS runtime and yt-dlp runs *without* the cookie, so
  YouTube doesn't force SABR-only streams). If you instead see **"confirm you're not
  a bot"**, set `YTDLP_USE_COOKIES=1` and `docker compose up -d musicbridge`.
- **Playback breaks after a YouTube change** — rebuild musicbridge on your dev
  machine to pull the latest yt-dlp, push it (see *Publishing images to GHCR*), then
  `docker compose pull musicbridge && docker compose up -d musicbridge` on the host.
- **A song you uploaded to YouTube Music doesn't play** — uploads are private
  videos that only YouTube Music's own client (logged in) may stream; youtube.com
  says "Video unavailable". musicbridge retries such tracks as YouTube Music with
  your cookie and a PO token from the `bgutil-provider` container (log line
  `[YouTube Music client]`). Make sure that container runs and shares the
  `multiroom` network with musicbridge.
- **Volume panel says "snapserver unreachable"** — set `SNAPSERVER_CONTROL` to your
  snapserver's `host:port` if it isn't the default `snapserver:1705`.
