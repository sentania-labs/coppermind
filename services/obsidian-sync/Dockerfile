# The Obsidian Sync helper is independently built and versioned. It contains
# Node, the official client and its dependency-free supervisor.
FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="coppermind-obsidian-sync" \
      org.opencontainers.image.description="Coppermind supervised Obsidian Sync helper" \
      org.opencontainers.image.source="https://github.com/sentania-labs/coppermind" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g obsidian-headless@0.0.14 \
    && ob login --help \
    && ob sync-create-remote --help \
    && ob sync-list-remote --help \
    && ob sync-setup --help \
    && ob sync --help \
    && ob sync-status --help \
    && ob sync-unlink --help \
    && ob logout --help \
    && npm cache clean --force
WORKDIR /app
COPY services/obsidian-sync/supervisor.mjs services/obsidian-sync/health.mjs services/obsidian-sync/control.mjs services/obsidian-sync/settings.mjs ./
COPY services/obsidian-sync/fake ./fake

# The account token and vault encryption key live in this dedicated home,
# mounted from their own credential volume rather than the data backup.
ENV HOME="/var/lib/obsidian-sync" \
    COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

RUN mkdir -p /data /run/coppermind/internal /var/lib/obsidian-sync \
    && chown -R 1000:1000 /data /app /run/coppermind /var/lib/obsidian-sync \
    && chmod 0700 /var/lib/obsidian-sync
USER 1000
EXPOSE 8092
VOLUME ["/data", "/var/lib/obsidian-sync"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["node", "/app/health.mjs"]

CMD ["node", "/app/supervisor.mjs"]
