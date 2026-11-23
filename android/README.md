# Multiroom for Android TV (Snapdroid fork)

A fork of [Snapdroid](https://github.com/snapcast/snapdroid) — the Snapcast client
for Android, by Johannes Pohl — with one addition: a full-screen **Now Playing**
screen for TVs, fed by musicbridge.

- Plays the Snapcast stream exactly like Snapdroid (same bundled native
  `snapclient`, v0.29.0), so it replaces the official app.
- While the player runs it polls musicbridge's `/api/status` and **opens Now Playing
  by itself when music starts** — or when the TV is switched on while music plays —
  even over another app. It closes itself ~10 s after the music stops, returning
  to whatever was on before. It never wakes a TV that is off.
- Now Playing shows artwork, title, artist, a live progress bar and the next song;
  the remote controls playback: **OK** pause/play, **◀ ▶** previous/next, **BACK**
  closes it.
- Own package id (`dev.profanter.multiroom`), so it installs next to (or instead
  of) the official app and Play Store updates never touch it.

Everything added is marked `Multiroom fork` in the source. Like Snapdroid, this
folder is **GPL-3.0** (see [`LICENSE`](LICENSE)); upstream Snapcast is the git
submodule at `Snapcast/src/main/cpp/snapcast`, pinned to the commit Snapdroid uses.

## Build

Needs the Android SDK with NDK `27.0.12077973` and CMake `3.22.1`
(`sdkmanager "ndk;27.0.12077973" "cmake;3.22.1"`) and a JDK 17+.

```bash
git submodule update --init --recursive
cd android
./fetch-deps.sh                                  # prebuilt native deps → Snapcast/libs/
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
./gradlew assembleRelease                        # → Snapcast/build/outputs/apk/release/
```

Release builds are signed when `android/keystore.properties` exists (gitignored):

```properties
storeFile=/home/you/.android/multiroom-release.jks
storePassword=…
keyAlias=multiroom
keyPassword=…
```

Keep that keystore: Android only installs an update signed with the same key
(otherwise: uninstall first, which also resets the app's settings).

## Install on a TV

On the TV: *Settings → Device Preferences → About* → click *Build* 7× to unlock
*Developer options*, then enable **USB debugging** / network debugging. Then:

```bash
TV=<tv-ip>                           # the TV's IP
PKG=dev.profanter.multiroom
adb connect $TV:5555                 # accept the prompt on the TV ("Always allow")
adb -s $TV:5555 install -r Snapcast/build/outputs/apk/release/Snapcast-release.apk

# Let Now Playing open by itself from the background ("Display over other apps").
adb -s $TV:5555 shell appops set $PKG SYSTEM_ALERT_WINDOW allow

# Configure without typing on the remote: snapserver host, musicbridge URL, start on boot.
adb -s $TV:5555 shell am start -n $PKG/de.badaix.snapcast.MainActivity \
  --es host <snapserver-ip> --es musicbridge_url https://<MUSIC_HOST> --ez autostart true

# Start playing (also happens on every boot with autostart).
adb -s $TV:5555 shell am broadcast -n $PKG/de.badaix.snapcast.BroadcastReceiver \
  -a de.badaix.snapcast.START_SERVICE
```

Replacing the official app? Install and start this one **first**, then
`adb uninstall de.badaix.snapcast`. The player holds a keep-screen-on lock; removing
the old app first leaves a gap in which the TV's idle standby can switch it off.

The new app registers as a new Snapcast client (new id, volume 100 %): set its
name and volume in musicbridge's Rooms panel or Snapweb, and delete the old entry.

Settings (host, Now Playing URL, *Open Now Playing when music starts*) are also in
the app's own settings dialog; *Now Playing* is in its menu.
