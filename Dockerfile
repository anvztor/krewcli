FROM node:22-bookworm AS node

FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=node /usr/local /usr/local
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml ./
COPY uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY docker/start-krewcli-compose.sh ./docker/start-krewcli-compose.sh

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
    && npm install -g @openai/codex@0.117.0 @anthropic-ai/claude-code \
    && mkdir -p /home/krewcli \
    && chmod 0777 /home/krewcli \
    && chmod +x /app/docker/start-krewcli-compose.sh

ENV PATH="/app/.venv/bin:${PATH}"
ENV HOME=/home/krewcli

WORKDIR /workspace

ENTRYPOINT ["/app/docker/start-krewcli-compose.sh"]
