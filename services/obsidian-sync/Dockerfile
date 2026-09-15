# The Obsidian Sync helper is independently built and versioned. It contains
# only Node, the pinned headless client and its dependency-free supervisor.
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

WORKDIR /app
COPY services/obsidian-sync/package.json services/obsidian-sync/package-lock.json ./
RUN npm ci --omit=dev \
    && npm cache clean --force
COPY services/obsidian-sync/supervisor.mjs services/obsidian-sync/health.mjs services/obsidian-sync/control.mjs ./
COPY services/obsidian-sync/fake ./fake

# No HOME on the data volume: where the Obsidian account credential lives is
# an open captain decision, and the data volume is what a backup carries.
ENV PATH="/app/node_modules/.bin:$PATH" \
    COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

RUN mkdir -p /data /run/coppermind/internal \
    && chown -R 1000:1000 /data /app /run/coppermind
USER 1000
EXPOSE 8092
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["node", "/app/health.mjs"]

CMD ["node", "/app/supervisor.mjs"]
