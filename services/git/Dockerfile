# The Git helper image. Build context is the repository root, like every
# Coppermind image, but this one installs only its own service and not the
# shared package: that package carries database drivers, and this helper never
# talks to PostgreSQL, the store or the network.
#
#   python3 -m coppermind_git.main     the helper (the default command)
#   python3 -m coppermind_git.health   the container health check
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="coppermind-git" \
      org.opencontainers.image.description="Coppermind Git helper: records the history of the notes filesystem" \
      org.opencontainers.image.source="https://github.com/sentania-labs/coppermind" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# Git from the base image's distribution, so its security fixes arrive with the
# distribution's. The base image is pinned by digest so a rebuild is
# reproducible, which also freezes its packages as they were the day that image
# was published; the upgrade is what keeps the image scan clean in between.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY pyproject.toml uv.lock README.md ./
# The other workspace members' manifests are needed for uv to resolve the
# workspace against the committed lock; their source is not copied.
COPY services/api/pyproject.toml ./services/api/pyproject.toml
COPY services/store/pyproject.toml ./services/store/pyproject.toml
COPY services/git ./services/git
RUN uv sync --frozen --no-dev --package coppermind-git \
    && rm -f /usr/local/bin/uv \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

ENV COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

# uid 1000, shared with the store, so the helper can write `.git` inside the
# notes filesystem the store owns.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin coppermind \
    && mkdir -p /data \
    && chown coppermind:coppermind /data
USER 1000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-m", "coppermind_git.health"]

CMD ["python3", "-m", "coppermind_git.main"]
