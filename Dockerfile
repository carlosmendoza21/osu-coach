# One image for both the API and the Celery worker.
FROM python:3.12-slim AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first so code changes don't bust this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# /app/data must belong to app: a new named volume copies this folder's owner, and
# without it the worker gets "Permission denied" on its first download.
RUN useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data
USER app

EXPOSE 8000
CMD ["uvicorn", "osu_coach.main:app", "--host", "0.0.0.0", "--port", "8000"]
