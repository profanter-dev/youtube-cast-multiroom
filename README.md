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
Phone browser ──► music.<domain>  (musicbridge web UI, password-protected via Traefik)
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

## Deploy

```bash
git clone https://github.com/YOUR_USERNAME/youtube-cast-multiroom
cd youtube-cast-multiroom
cp .env.example .env          # edit DATA_DIR, MUSIC_HOST, SNAPCAST_HOST, MUSIC_AUTH
```

### 1. Set the web-UI password (`MUSIC_AUTH`)

The UI is exposed publicly via Traefik, so it's protected with HTTP basic auth.

```bash
htpasswd -nbB youruser 'yourpassword'
```

Put the result in `.env` as `MUSIC_AUTH`, **doubling every `$`** (compose treats a
single `$` as a variable). Example: `$2y$05$abc…` becomes `$$2y$$05$$abc…`.

### 2. Connect your YouTube Music account (cookie upload)

Search and playback work out of the box. **Library and Playlists** need your
account, which musicbridge gets from a single **`cookies.txt`** exported from a
browser logged into YouTube Music. The same cookie also lets yt-dlp pull streams
past YouTube's "are you a bot?" checks — one file powers everything.

Upload it from the web UI's **Account** button (no SSH needed):

1. Open <https://music.youtube.com> in a browser and confirm you're logged in
   (your avatar shows, your library is visible).
2. Install the **"Get cookies.txt LOCALLY"** extension (Chrome/Brave web store).
3. On the music.youtube.com tab, click the extension → **Export** (current site).
4. In musicbridge, tap **Account → Choose cookies.txt** and pick that file.

The page confirms how many playlists it found. If it says *"signed-out / 0
playlists"*, the export wasn't from a logged-in YouTube Music session — redo
step 1. The cookie is stored at `$DATA_DIR/musicbridge/cookies.txt`, and
`browser.json` (ytmusicapi browser auth) is generated from it automatically.

> Cookies go stale every few weeks — when Library/Playlists empty out, just
> re-export and re-upload via **Account → Replace cookie**.
>
> Why cookies and not OAuth? YouTube Music has no usable official API for the
> library, and `ytmusicapi`'s OAuth path is currently broken server-side. The
> web client (cookie auth) is the only thing that returns your *full* library,
> including saved playlists.

### 3. Start

```bash
docker compose up -d --build
```

Open `https://<MUSIC_HOST>` on your phone, log in, search, and tap a track. Audio
starts on every connected Snapcast client.

## Install the Snapcast app on each Google TV

1. Open the **Google Play Store** on the Google TV.
2. Search for **Snapcast** and install the app by badaix.
3. Open it, tap **+**, and enter the IP of the host running Docker.
4. Connect — the TV now plays whatever musicbridge is streaming, in sync.

Repeat on every TV. Per-room volume/mute is handled in the Snapcast app or the
Snapweb UI at `http://<host>:1780`.

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
| `MUSIC_AUTH` | `htpasswd` `user:hash` for the web UI (double every `$`). |
