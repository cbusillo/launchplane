FROM mirror.gcr.io/library/python:3.14-slim AS python-base

FROM mirror.gcr.io/library/node:22-bookworm-slim AS frontend-node

FROM python-base AS frontend-build

COPY --from=frontend-node /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && pip install --no-cache-dir uv

WORKDIR /app

COPY frontend/package.json /app/frontend/
WORKDIR /app/frontend
RUN corepack enable \
    && corepack install

COPY frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml /app/frontend/
RUN pnpm install --frozen-lockfile

COPY pyproject.toml uv.lock README.md /app/
RUN uv sync --frozen --no-dev --no-install-project
ENV UV_FROZEN=1
COPY control_plane /app/control_plane
COPY frontend /app/frontend
RUN pnpm build

FROM mirror.gcr.io/library/golang:1.27.3-bookworm AS artifact-scanner

ARG TRIVY_VERSION=v0.76.0
ARG TRIVY_GO_JOSE_VERSION=v4.1.6
ARG TRIVY_X_NET_VERSION=v0.62.0

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local

RUN mkdir -p /tmp/artifact-scanner-build \
    && cd /tmp/artifact-scanner-build \
    && go mod init launchplane.local/artifact-scanner-build \
    && go get "github.com/aquasecurity/trivy/cmd/trivy@${TRIVY_VERSION}" \
    && go get "github.com/go-jose/go-jose/v4@${TRIVY_GO_JOSE_VERSION}" \
    && go get "golang.org/x/net@${TRIVY_X_NET_VERSION}" \
    && go build -trimpath -o /go/bin/trivy \
        -ldflags="-s -w -extldflags=-static -X github.com/aquasecurity/trivy/pkg/version/app.ver=${TRIVY_VERSION#v}" \
        github.com/aquasecurity/trivy/cmd/trivy \
    && go version -m /go/bin/trivy | grep -F "github.com/aquasecurity/trivy" | grep -F "${TRIVY_VERSION}" \
    && go version -m /go/bin/trivy | grep -F "github.com/go-jose/go-jose/v4" | grep -F "${TRIVY_GO_JOSE_VERSION}" \
    && go version -m /go/bin/trivy | grep -F "golang.org/x/net" | grep -F "${TRIVY_X_NET_VERSION}" \
    && /go/bin/trivy --version

FROM python-base AS runtime

LABEL org.opencontainers.image.source="https://github.com/cbusillo/launchplane"

ENV PYTHONDONTWRITEBYTECODE=1 \
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

COPY --from=artifact-scanner /go/bin/trivy /usr/bin/trivy

RUN pip install --no-cache-dir uv \
    && python -m pip uninstall --yes pip setuptools

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
COPY alembic.ini /app/alembic.ini
COPY control_plane /app/control_plane
COPY scripts /app/scripts
COPY --from=frontend-build /app/control_plane/ui_static /app/control_plane/ui_static

RUN uv sync --frozen --no-dev

# Service-owned testing verification uses the library's matching headless browser.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/launchplane-browsers
RUN uv run playwright install --with-deps --only-shell chromium \
    && useradd --system --user-group --create-home --home-dir /opt/launchplane-browser \
        --shell /usr/sbin/nologin launchplane-browser \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:${PATH}" \
    UV_NO_SYNC=1

EXPOSE 8080

CMD ["/app/scripts/start-launchplane-service.sh"]
