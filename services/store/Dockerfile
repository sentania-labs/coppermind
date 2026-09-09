# The store image. Build context is the repository root, which is the org
# convention: the image installs the shared `coppermind` package and one
# service from the same uv workspace, resolved from the committed uv.lock so a
# rebuild produces the same dependency set.
#
# This one image has three jobs, selected by the command:
#   python -m coppermind_store.bootstrap   one-shot, prepares /data and secrets
#   alembic -c /app/alembic.ini upgrade head   one-shot, migrations
#   uvicorn coppermind_store.main:app      the service
# Keeping them in one image is what makes the migration job impossible to run
# against a different version of the schema than the store expects.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="coppermind-store" \
      org.opencontainers.image.description="Coppermind store: the only writer of the notes filesystem" \
      org.opencontainers.image.source="https://github.com/sentania-labs/coppermind" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY coppermind ./coppermind
# The other workspace members' manifests are needed for uv to resolve the
# workspace against the committed lock; their source is not copied, so this
# image carries only its own service.
COPY services/api/pyproject.toml ./services/api/pyproject.toml
COPY services/store ./services/store
# uv and pip are build tools. Both are removed from the runtime image:
# nothing in the running container installs anything, and their own bundled
# dependencies are the usual source of image scan findings that no bump here
# can fix.
RUN uv sync --frozen --no-dev --package coppermind-store \
    && rm -f /usr/local/bin/uv \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

# The build identity the running service reports, not just the image labels.
ENV COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

# uid 1000 is shared by every image that touches the notes filesystem, so the
# store, the Git helper and the Obsidian Sync client all own what they write.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin coppermind \
    && mkdir -p /data /run/coppermind/internal /run/coppermind/postgres \
    && chown -R coppermind:coppermind /data /run/coppermind
USER 1000
EXPOSE 8081
VOLUME ["/data"]

# Uses the standard library, so the image carries no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/healthz')"

CMD ["uvicorn", "coppermind_store.main:app", "--host", "0.0.0.0", "--port", "8081"]
