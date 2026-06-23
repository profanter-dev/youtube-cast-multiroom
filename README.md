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

### 2. Connect your YouTube Music account (one-time)

Library and Playlists need your Google account. The easiest, most durable way is
**Sign in with Google** right from the web UI's **Account** button — it uses a
self-refreshing OAuth token, so you never have to re-capture cookies.

It needs a free, one-time OAuth client (Google retired the shared one):

1. Go to <https://console.cloud.google.com/> → create a project (any name).
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → choose **External**, fill the
   required fields, and add **your own Google account** under *Test users*.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **TVs and Limited Input devices**. Copy the **Client ID** and
   **Client secret**.
5. Put them in `.env`:
   ```bash
   GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxx
   ```
6. `docker compose up -d musicbridge`, open the web UI, tap **Account → Start
   login**, open the shown link, enter the code, approve. Done — the token is
   stored at `$DATA_DIR/musicbridge/oauth.json` and refreshes itself.

**What the login powers:** your **Playlists** and **Liked** music come from the
official **YouTube Data API v3** using this OAuth token (stable + self-refreshing).
**Search** is public and needs no login. Playback always uses yt-dlp (+ optional
`cookies.txt`). Note the YTM-only "songs added to library" bucket isn't exposed by
the official API — the Liked tab covers your liked tracks instead.

> Why not `ytmusicapi`'s own auth for the library? Its OAuth path currently 400s
> server-side (a YouTube change), and browser-cookie auth goes stale. Going
> straight to the official Data API for playlists/liked sidesteps both.

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
| `DATA_DIR` | Host path for persistent data (`snapfifo`, `musicbridge/oauth.json`, optional `cookies.txt`). |
| `MUSIC_HOST` | Hostname Traefik routes to the music web UI. |
| `SNAPCAST_HOST` | Hostname Traefik routes to the Snapweb UI. |
| `MUSIC_AUTH` | `htpasswd` `user:hash` for the web UI (double every `$`). |
| `GOOGLE_CLIENT_ID` | OAuth client ID for Google login (optional; "TVs and Limited Input devices"). |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret matching `GOOGLE_CLIENT_ID`. |
