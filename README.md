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

## Testing

```bash
uv run pytest                  # unit tests (default, what CI runs)
uv run pytest -m integration   # live tests: real Redis + real osu! API
```

**Unit tests** ([`tests/unit/`](tests/unit), 23 tests): call the code directly with nothing
running. `respx` fakes the osu! token endpoint (and blocks any unmocked request), and
`fakeredis` stands in for Redis.
- `get_token` cache: a cache hit makes no osu! request, a miss fetches and stores, a second
  call reuses the token, the TTL is `expires_in` minus a 60 s margin, and an expired entry
  triggers a new fetch
- Token request: sent as a client-credentials form with the real secret, not the
  `SecretStr` mask
- Errors: `400`/`401` raise `OsuAuthError`; `403`, `429` and `5xx` stay HTTP errors, so an
  outage isn't reported as bad credentials; network errors pass through; broken replies
  (missing fields, HTML, empty body) raise `OsuAuthError`
- No leaks: neither the client secret nor the token appears in any error, and failed
  fetches never write to the cache

**Integration tests** ([`tests/integration/`](tests/integration), 4 tests): use real
Redis and the real osu! API with the credentials in `.env`. They skip with the reason if
Redis isn't running (`docker compose up -d redis`) or the credentials are missing, and
delete the cached token key before and after each test.
- A real token is cached with a TTL of about 86,340 s
- A second call makes no new request to osu!
- The token is accepted by the API (`GET /beatmaps/75` returns 200)
- A wrong secret raises `OsuAuthError` and caches nothing

The unit tests were checked against deliberately broken versions of `auth.py`: removing the
expiry margin, the `400`/`401` check, or the cache read, sending the masked secret, or
putting the reply in the error message each make at least one test fail.

## Layout

```
src/osu_coach/
  main.py      FastAPI app
  api/         routers (/health so far)
  osu/auth.py  osu! API token (client credentials), cached in Redis
  config.py    settings from env / .env
  db.py        async SQLAlchemy engine + session dependency
  models.py    SQLAlchemy declarative base
  worker.py    Celery app
migrations/    Alembic
tests/
  unit/        no services needed
  integration/ real Redis + osu! API
```
