# youtube-cast-multiroom

Synchronized multiroom YouTube Music receiver built on a fake Chromecast device (castbridge) and Snapcast.

## How it works

1. **castbridge** advertises itself on your LAN as a Chromecast device named "Multiroom" via mDNS.
2. When you cast from YouTube Music, castbridge receives the track, downloads audio via `yt-dlp`, transcodes it with `ffmpeg`, and writes raw PCM into a named pipe.
3. **snapserver** reads from that pipe and streams the audio in perfect time-sync to every connected Snapcast client.

## Deploy

```bash
git clone https://github.com/YOUR_USERNAME/youtube-cast-multiroom
cd youtube-cast-multiroom
cp .env.example .env          # edit DEVICE_NAME if you want a different cast target name
docker compose up -d --build
```

> **Note:** castbridge runs with `network_mode: host` so mDNS multicast packets reach the LAN. The host running Docker must be on the same network segment as your phone for mDNS discovery to work.

## Updating snapserver

The snapserver version is pinned in `snapserver/Dockerfile` (`SNAPCAST_VERSION`). To update:

1. Change `SNAPCAST_VERSION` to the new version number.
2. Rebuild and restart: `docker compose build snapserver && docker compose up -d snapserver`

## Install Snapdroid on each Google TV

1. Open the **Google Play Store** on your Google TV.
2. Search for **Snapcast** and install the app by badaix.
3. Open the app, tap the **+** button, and enter the IP address of the host running Docker.
4. Press **Connect** — the TV will now receive synchronized audio whenever something is casting.

Repeat on every TV you want in the multiroom group.

## Cast music

1. Open **YouTube Music** on your phone.
2. Tap the **Cast** icon (top-right area of the player).
3. Select **Multiroom** (or whatever you set `DEVICE_NAME` to).
4. Music starts playing in sync on all connected TVs.

To change volume per-room or mute individual TVs, use the Snapcast app on that TV or the Snapweb UI.

## Snapweb volume control UI

The Snapweb interface is available at `http://<your-host>:1780`. If you have Traefik with a wildcard cert, the included labels will also expose it over HTTPS on whatever hostname you configure in `docker-compose.yml`.

From there you can:
- Adjust volume for each individual Snapcast client
- Mute or delay individual rooms
- Group/ungroup clients

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DEVICE_NAME` | `Multiroom` | Name shown in the YouTube Music cast picker |

## Volumes

| Volume | Purpose |
|---|---|
| `snapfifo` | Named pipe shared between castbridge and snapserver |
| `castbridge-certs` | Persisted self-signed TLS cert for the Cast V2 server |
