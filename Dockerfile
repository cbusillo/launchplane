FROM mirror.gcr.io/library/node:22-bookworm-slim AS frontend-build

WORKDIR /app

RUN corepack enable \
    && corepack prepare pnpm@10.10.0 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml /app/frontend/
WORKDIR /app/frontend
RUN pnpm install --frozen-lockfile

COPY frontend /app/frontend
RUN pnpm build

FROM mirror.gcr.io/library/golang:1.26.6-bookworm AS github-cli-build

ARG GITHUB_CLI_VERSION=v2.98.0
ARG GITHUB_CLI_GRPC_VERSION=v1.83.0
ARG GITHUB_CLI_X_CRYPTO_VERSION=v0.55.0
ARG GITHUB_CLI_X_TEXT_VERSION=v0.41.0

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local

RUN github_cli_x_mod_version=v0.40.0 \
    && mkdir -p /tmp/github-cli-build \
    && cd /tmp/github-cli-build \
    && go mod init launchplane.local/github-cli-build \
    && go get "github.com/cli/cli/v2/cmd/gh@${GITHUB_CLI_VERSION}" \
    && go get "google.golang.org/grpc@${GITHUB_CLI_GRPC_VERSION}" \
    && go get "golang.org/x/crypto@${GITHUB_CLI_X_CRYPTO_VERSION}" \
    && go get "golang.org/x/mod@${github_cli_x_mod_version}" \
    && go get "golang.org/x/text@${GITHUB_CLI_X_TEXT_VERSION}" \
    && go build -trimpath -o /go/bin/gh github.com/cli/cli/v2/cmd/gh \
    && go version -m /go/bin/gh | grep -F "github.com/cli/cli/v2" | grep -F "${GITHUB_CLI_VERSION}" \
    && go version -m /go/bin/gh | grep -F "google.golang.org/grpc" | grep -F "${GITHUB_CLI_GRPC_VERSION}" \
    && go version -m /go/bin/gh | grep -F "golang.org/x/crypto" | grep -F "${GITHUB_CLI_X_CRYPTO_VERSION}" \
    && go version -m /go/bin/gh | grep -F "golang.org/x/mod" | grep -F "${github_cli_x_mod_version}" \
    && go version -m /go/bin/gh | grep -F "golang.org/x/text" | grep -F "${GITHUB_CLI_X_TEXT_VERSION}"

FROM mirror.gcr.io/library/python:3.13-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/cbusillo/launchplane"

ENV PYTHONDONTWRITEBYTECODE=1 \
    GH_PROMPT_DISABLED=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends --only-upgrade \
        bsdutils \
        libblkid1 \
        libgssapi-krb5-2 \
        libk5crypto3 \
        libkrb5-3 \
        libkrb5support0 \
        liblastlog2-2 \
        libmount1 \
        libsmartcols1 \
        libssl3t64 \
        libuuid1 \
        login \
        mount \
        openssl \
        openssl-provider-legacy \
        util-linux \
    && apt-get install -y --no-install-recommends ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=github-cli-build /go/bin/gh /usr/local/bin/gh

RUN pip install --no-cache-dir uv \
    && python -m pip uninstall --yes pip setuptools

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
