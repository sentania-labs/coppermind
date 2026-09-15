# The operator interface is a separate image and lifecycle from the public API.
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG BUILD_VERSION=dev
ARG BUILD_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="coppermind-admin" \
      org.opencontainers.image.description="Coppermind Admin: operator interface" \
      org.opencontainers.image.source="https://github.com/sentania-labs/coppermind" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

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
COPY services/api/pyproject.toml ./services/api/pyproject.toml
COPY services/store/pyproject.toml ./services/store/pyproject.toml
COPY services/git/pyproject.toml ./services/git/pyproject.toml
COPY services/admin ./services/admin
RUN uv sync --frozen --no-dev --package coppermind-admin \
    && rm -f /usr/local/bin/uv \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/bin/pip*

ENV COPPERMIND_BUILD_VERSION="${BUILD_VERSION}"

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin coppermind \
    && mkdir -p /data /run/coppermind/postgres \
    && chown -R coppermind:coppermind /data /run/coppermind
USER 1000
EXPOSE 8082

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8082/healthz')"

CMD ["uvicorn", "coppermind_admin.main:app", "--host", "0.0.0.0", "--port", "8082"]
