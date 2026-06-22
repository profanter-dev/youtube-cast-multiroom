FROM node:20-slim

# Install ffmpeg and curl (for yt-dlp download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    avahi-utils \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Download latest yt-dlp binary
RUN curl -L "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp" \
    -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

# Install dependencies first (layer cache)
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile --ignore-scripts

# Copy source and compile
COPY tsconfig.json ./
COPY src/ ./src/
RUN yarn tsc

# Remove devDependencies after build
RUN yarn install --frozen-lockfile --ignore-scripts --production

ENTRYPOINT ["node", "dist/index.js"]
