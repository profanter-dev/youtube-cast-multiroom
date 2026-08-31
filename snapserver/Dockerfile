FROM debian:bookworm-slim

ARG SNAPCAST_VERSION=0.35.0
ARG TARGETARCH

RUN set -eux; \
    # TARGETARCH is supplied by BuildKit only. The legacy builder leaves it unset,
    # which under `set -u` aborts the build — so fall back to the build host's own
    # architecture, which is the right answer for any non-cross build.
    ARCH="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "${ARCH}" in \
        amd64) DEB_ARCH=amd64 ;; \
        arm64) DEB_ARCH=arm64 ;; \
        arm|armhf) DEB_ARCH=armhf ;; \
        *) echo "Unsupported arch: ${ARCH}"; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends wget ca-certificates; \
    wget -q "https://github.com/snapcast/snapcast/releases/download/v${SNAPCAST_VERSION}/snapserver_${SNAPCAST_VERSION}-1_${DEB_ARCH}_bookworm.deb" \
        -O /tmp/snapserver.deb; \
    apt-get install -y --no-install-recommends /tmp/snapserver.deb; \
    rm /tmp/snapserver.deb; \
    apt-get purge -y wget; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.source=https://github.com/profanter-dev/youtube-cast-multiroom

# Baked in (not bind-mounted) so a deployment host needs no checkout of this repo —
# just docker-compose.yml + .env. snapserver reads this path by default.
COPY snapserver.conf /etc/snapserver.conf

EXPOSE 1704 1705 1780

ENTRYPOINT ["/usr/bin/snapserver"]
