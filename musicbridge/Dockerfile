FROM python:3.12-slim

# ffmpeg decodes the YouTube audio stream to raw PCM for the Snapcast FIFO.
# deno is a JavaScript runtime yt-dlp uses to descramble YouTube signatures;
# without it, extraction falls back to a limited client and many tracks fail
# with "Requested format is not available". (x86_64 glibc build for Debian.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl unzip \
    && curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static/ ./static/

ENV YTM_AUTH_FILE=/data/browser.json \
    SNAPFIFO=/snapfifo/snapfifo \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "app.py"]
