FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/cbusillo/launchplane"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md /app/
COPY alembic.ini /app/alembic.ini
COPY control_plane /app/control_plane
COPY config /app/config
COPY scripts /app/scripts

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8080

CMD ["/app/scripts/start-launchplane-service.sh"]
