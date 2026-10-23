# osu! AI Coach

Analyzes osu! replays hit by hit, builds a skill profile (aim, speed, accuracy, consistency,
stamina, reading), and recommends practice maps.

## Run it

```bash
docker compose up --build
```

- API: http://localhost:8000 (docs at `/docs`, health at `/health`)
- Postgres 16 + pgvector on `5432`, Redis on `6379`
- The `migrate` service runs `alembic upgrade head` before the API and worker start.

## Develop

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Run the API outside Docker (with Postgres and Redis from Compose):

```bash
docker compose up -d postgres redis
uv run alembic upgrade head
uv run uvicorn osu_coach.main:app --reload
```

New migration after changing `src/osu_coach/models.py`:

```bash
uv run alembic revision --autogenerate -m "describe change"
```

## Layout

```
src/osu_coach/
  main.py      FastAPI app
  api/         routers (/health so far)
  config.py    settings from env / .env
  db.py        async SQLAlchemy engine + session dependency
  models.py    SQLAlchemy declarative base
  worker.py    Celery app
migrations/    Alembic
tests/
```
