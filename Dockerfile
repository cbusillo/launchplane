FROM mirror.gcr.io/library/node:22-bookworm-slim AS frontend-build

WORKDIR /app

RUN corepack enable \
    && corepack prepare pnpm@10.10.0 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml /app/frontend/
WORKDIR /app/frontend
RUN pnpm install --frozen-lockfile

COPY frontend /app/frontend
RUN pnpm build

FROM mirror.gcr.io/library/golang:1.26.4-bookworm AS github-cli-build

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local

RUN go install github.com/cli/cli/v2/cmd/gh@v2.95.0

FROM mirror.gcr.io/library/python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/cbusillo/launchplane"

ENV PYTHONDONTWRITEBYTECODE=1 \
    GH_PROMPT_DISABLED=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends --only-upgrade \
        libgssapi-krb5-2 \
        libk5crypto3 \
        libkrb5-3 \
        libkrb5support0 \
        libssl3t64 \
        openssl \
        openssl-provider-legacy \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=github-cli-build /go/bin/gh /usr/local/bin/gh

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
COPY alembic.ini /app/alembic.ini
COPY control_plane /app/control_plane
COPY scripts /app/scripts
COPY --from=frontend-build /app/control_plane/ui_static /app/control_plane/ui_static

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

CMD ["/app/scripts/start-launchplane-service.sh"]
