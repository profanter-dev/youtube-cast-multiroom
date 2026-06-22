FROM python:3.12-slim

# ffmpeg decodes the YouTube audio stream to raw PCM for the Snapcast FIFO.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
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
