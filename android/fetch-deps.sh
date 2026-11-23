#!/bin/sh
# Downloads the prebuilt native dependencies (.aar) the Snapcast client links
# against into Snapcast/libs/ (gitignored). Same set and versions upstream
# Snapdroid's CI uses — public GitHub release assets, no token needed.
set -eu
cd "$(dirname "$0")/Snapcast/libs"
BASE=https://github.com/badaix/snapcast-deps/releases/download/v0.29.0
for aar in boost-1.85.0 flac-1.4.2 oboe-1.9.0 ogg-1.3.5 opus-1.1.2 soxr-0.1.3 tremor-1.0.1 vorbis-1.3.7; do
    [ -f "$aar.aar" ] || curl -fsSLO "$BASE/$aar.aar"
done
ls -1 *.aar
