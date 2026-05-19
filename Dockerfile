FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY deploy/log_config.yaml ./deploy/log_config.yaml
COPY src/ ./src/
COPY templates/ ./templates/
COPY data/user_config_allowlist.toml ./data/user_config_allowlist.toml
RUN uv sync --frozen --no-dev && \
    chown -R 65534:65534 /app

USER 65534:65534

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "claw_proxy.app:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "deploy/log_config.yaml"]
