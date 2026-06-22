# Ripster — Python web UI for multi-service music downloading.
# NOTE: Apple Music (zhaarey/AMD) needs the Go downloader (main.go) + Windows
# wrapper .exe and will NOT work in this Linux image. Deezer / Qobuz / Tidal /
# SoundCloud / Spotify-convert run fine here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RIPSTER_HOST=0.0.0.0 \
    RIPSTER_PORT=7799

# Runtime deps: ffmpeg (transcode/postprocess), git+curl+ca-certificates
# (some engines pip-install from VCS / fetch over https at runtime).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App source.
COPY . .

# Seed a config.yaml from the example on first build if the operator didn't
# bake one in (the running app rewrites it via config_service).
RUN [ -f config.yaml ] || cp config.example.yaml config.yaml

EXPOSE 7799

# Persist downloads and per-service tokens across container restarts.
VOLUME ["/app/downloads", "/app/tokens"]

CMD ["python", "app.py"]
