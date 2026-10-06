FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="SoulSync Playlist Publisher" \
      org.opencontainers.image.description="Personal Navidrome playlists and music copies alongside an unchanged SoulSync instance" \
      org.opencontainers.image.source="https://github.com/mschabhuettl/soulsync-playlist-publisher"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODE=dry-run \
    INTERVAL_SECONDS=300 \
    CONFIG_PATH=/config/playlist-publisher.json \
    HEARTBEAT_PATH=/tmp/publisher-heartbeat.json \
    SOULSYNC_CONFIG_PATH=/app/config/config.json

WORKDIR /opt/publisher
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && mkdir -p /config /state \
    && chown 3007:3007 /state
COPY publish_playlists.py soul_source.py navidrome_api.py publisher_files.py run_service.py ./

USER 3007:3007
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python3", "-B", "/opt/publisher/run_service.py", "healthcheck"]
ENTRYPOINT ["python3", "-B", "/opt/publisher/run_service.py"]
