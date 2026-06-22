FROM debian:bookworm-slim

ARG SNAPCAST_VERSION=0.35.0
ARG TARGETARCH

RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) DEB_ARCH=amd64 ;; \
        arm64) DEB_ARCH=arm64 ;; \
        arm)   DEB_ARCH=armhf ;; \
        *) echo "Unsupported arch: ${TARGETARCH}"; exit 1 ;; \
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

EXPOSE 1704 1705 1780

ENTRYPOINT ["/usr/bin/snapserver"]
