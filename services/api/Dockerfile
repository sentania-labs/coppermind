# The API image. Build context is the repository root. The API mounts no
# volume and holds no state: it needs the shared package and the service, and
# nothing else.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="coppermind-api" \
      org.opencontainers.image.description="Coppermind API: the public contract, stateless" \
      org.opencontainers.image.source="https://github.com/sentania-labs/coppermind" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# The base image is pinned by digest so a rebuild is reproducible, which also
# means it freezes the distribution packages as they were the day that image
# was published. Applying the distribution's security updates here is what
# keeps the image scan clean between base image rebuilds.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.12@sha256:72ab0aeb448090480ccabb99fb5f52b0dc3c71923bffb5e2e26517a1c27b7fec /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY pyproject.toml uv.lock README.md ./
COPY coppermind ./coppermind
# The other workspace members' manifests are needed for uv to resolve the
# workspace against the committed lock; their source is not copied, so this
# image carries only its own service.
COPY services/store/pyproject.toml ./services/store/pyproject.toml
COPY services/api ./services/api
# uv and pip are build tools. Both are removed from the runtime image:
# nothing in the running container installs anything, and their own bundled
# dependencies are the usual source of image scan findings that no bump here
# can fix.
RUN uv sync --frozen --no-dev --package coppermind-api \
    && rm -f /usr/local/bin/uv \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

# The build identity the running service reports, not just the image labels.
ENV COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin coppermind
USER 1000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"

CMD ["uvicorn", "coppermind_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
