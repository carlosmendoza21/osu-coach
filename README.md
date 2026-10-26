# osu! AI Coach

Analyzes osu! replays hit by hit, builds a skill profile (aim, speed, accuracy, consistency,
stamina, reading), and recommends practice maps.

## Run it

```bash
docker compose up --build
```

- API: http://localhost:8000 (docs at `/docs`, health at `/health`)
- `GET /players/{username}`: a player's profile, top 50 plays by pp and latest 20 plays.
  Synced from osu! through the worker when the stored copy is missing or older than
  10 minutes; otherwise served straight from the database
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
uv run pytest                  # unit tests (default)
uv run pytest -m integration   # live tests: real Postgres, Redis and osu! API
```

CI runs the unit tests and the Postgres tests (tables, sync and the players endpoint) on
every push.

On Windows, async database code needs a selector event loop (psycopg can't use the
default one); `tests/conftest.py` sets it for the tests, and the worker task sets it too.

**Unit tests** ([`tests/unit/`](tests/unit), 277 tests): call the code directly with nothing
running. `respx` fakes the osu! token endpoint (and blocks any unmocked request),
`fakeredis` stands in for Redis, and the rate limiter gets a fake clock, so a 60 s window
runs instantly.

*osu! token* (22):
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

*Rate limiter* (20):
- Allows exactly `limit` requests per window (1, 3 and 60), then refuses; refused attempts
  are never stored, so retries can't keep the window full
- The wait is until the oldest entry leaves the window; boundary: refused at 59.999 s,
  allowed at 60 s
- Requests at the same moment are all counted (unique members); 20 callers on one key
  share the limit; different keys are independent
- The key expires after the window, rounded up and never 0
- `acquire` doesn't sleep under the limit, spreads requests over windows
  (`0, 0, 60, 60, 120` s for a limit of 2), and records its slot even if it slept longer
  than asked. A fake-clock guard fails the test instead of hanging if `acquire` would wait
  forever

*Retries* (85, no network and no real waiting):
- Backoff doubles from 1 s to a 30 s cap with full jitter, and stays within the jitter range
- `Retry-After`: numbers are used, and negative, `inf`, `nan`, dates and junk fall back to
  backoff; every wait is capped, so a server can't make us sleep for a day
- Bad policies (`max_attempts` < 1, negative delays) are refused
- `429` and `5xx` are retried; `400`, `401`, `403`, `404` and other permanent statuses
  return at once
- No wait after the last attempt; the last response goes back to the caller
- Network errors (connect, timeouts, dropped connections) back off and are raised on the
  last attempt; our own bugs and errors like redirect loops are never retried

*API models* (39), using real osu! responses saved in
[`tests/fixtures/osu/`](tests/fixtures/osu) (a profile, three best scores, a failed play):
- Real users and scores parse with the right values; osu!'s ~70 user fields are reduced
  to the ones we model
- Mods are acronym lists (`[]`, `["PF"]`, `["HD", "DT"]`); beatmap `od`/`hp` come from
  osu!'s `accuracy`/`drain`, and a field literally named `od` isn't accepted instead
- Nullable where osu! sends null: ranks of inactive players, `pp` on unranked maps and
  failed plays
- Missing or wrongly typed fields fail with the exact location (`statistics.pp`,
  `beatmap.accuracy`, `mods.0`, ...); the newer score format (`great`/`ok`, mod objects)
  is rejected rather than misread
- Parsed objects are read-only

*API client* (36), with osu! faked by `respx`, fakeredis, a fake clock for the rate limiter,
and recorded retry waits:
- Requests: users by id (`key=id`) or username (`key=username`, escaped: spaces,
  brackets, `/`, and all-digit names), best and recent scores with the right `mode`,
  `limit` and `include_fails`, and the cached token on every request
- `limit` outside 1-100 is rejected before any request; an empty score list is fine
- Every attempt, retries included, takes a rate-limit slot; the limiter makes calls wait;
  `429`, `5xx` and network errors are retried
- A `401` clears the cached token and retries exactly once with a new one; a second `401`
  is an error, not a loop
- `404` raises `OsuNotFoundError`; other errors raise `OsuApiError` (its parent); errors
  never contain the token or secret

*Database tables* (34), checked from the model definitions without a database:
- `players` and `scores` keyed by osu!'s own ids (64-bit, not generated by the database);
  scores reference players with `ON DELETE CASCADE`
- Each column is nullable exactly when the osu! API field it comes from can be null (21
  column/field pairs), so real data never fails on `NOT NULL`
- The play date has no default; `updated_at` is set by Postgres; times keep their timezone
- Only the indexes queries use: `username` (not unique: names can be reused), `beatmap_id`,
  and `(player_id, pp)`

*Sync* (22), with a fake osu! client serving the saved responses and a fake session whose
statements are compiled to Postgres SQL:
- `player_values` / `score_values` fill every column from the right osu! field
- Upserts are `INSERT ... ON CONFLICT DO UPDATE` on the osu! id, updating every column but
  the id, and `updated_at = now()` for players; all scores go in one statement, with
  duplicates (a play in both best and recent) removed first
- Order: player, then scores, then one commit; scores are fetched by the resolved id;
  limits are passed on
- If osu! fails at any of the three requests, nothing is written

*`GET /players/{username}`* (18), with a fake sync, replaced queries and a fake session:
- A fresh player is served without syncing; a stale or unknown one is synced, then
  reloaded by the id the worker returned (`populate_existing`), so the response shows the
  synced values; the username is passed on as typed
- `404` when osu! doesn't know the player; with no stored data, `502` for an osu! error
  and `504` for a timeout; with stale data, that data is served instead (graceful
  degradation)
- The response has exactly the public fields; the worker is waited on in a thread with a
  30 s timeout

**Integration tests** ([`tests/integration/`](tests/integration), 67 tests): use real
Redis, real time, real processes, and the real osu! API with the credentials in `.env`.
They skip with the reason if Redis isn't running (`docker compose up -d redis`) or the
credentials are missing, and clean up their Redis keys even if a check fails.

*osu! token* (4):
- A real token is cached with a TTL of about 86,340 s
- A second call makes no new request to osu!
- The token is accepted by the API (`GET /beatmaps/75` returns 200)
- A wrong secret raises `OsuAuthError` and caches nothing

*Rate limiter* (5):
- In real time, 3-per-second lets the first three through at once and the next two after
  about 1 s
- The key gets a real TTL
- 20 Redis connections at once, and 4 or 8 separate processes firing at the same moment
  (3 rounds each), never let more than the limit through. A first version that counted
  and then added in separate calls let 4 through a limit of 3 here

*API models* (5, credentials only, no Redis):
- Today's responses still fit the models: peppy and the current top 5 users, lookup by
  username, 50 best scores each for the top 3, and recent plays including fails
- An unknown username returns `404`

*API client* (7, real osu! and Redis):
- The same player by username and by id; 50 best scores, best first, and `limit=5`
  matching the first five; recent plays of the current top 10
- Unknown usernames, including the all-digit `"2"`, raise `OsuNotFoundError`
- A rejected cached token recovers: user (401), new token, user again
- Each API request takes exactly one rate-limit slot

*Database* (13, Postgres only; also run in CI): a separate `<db>_test` database is created,
migrated with Alembic exactly as deployment does, and dropped afterwards
- The migration reaches head, `alembic check` finds nothing to change, and it can be
  undone and redone; nullability, the cascading foreign key and indexes match in Postgres
- Real users, best scores (with `HD`/`DT`) and a failed play without pp round-trip,
  including the play time's timezone and a 64-bit score id
- A score for an unknown player is refused, deleting a player deletes their scores, and a
  missing play date is refused
- "A player's best scores" uses the `(player_id, pp)` index, already in pp order (no sort)

*Sync* (11, Postgres; also run in CI), on its own test database:
- A first sync saves the player and scores; a second gives the same rows (idempotent),
  moves `updated_at`, and updates changed values
- Failed plays are saved without pp; a play in both lists is saved once; mods and the
  play time survive
- All or nothing: an osu! failure saves nothing, and a database error while saving scores
  rolls back the player too
- A real sync of peppy from osu! (needs Redis and credentials)

*`GET /players/{username}`* (14, Postgres; also run in CI): the real queries, and the
endpoint end to end with a sync that runs the real `sync_player` against a fake osu!
- Lookup ignores letter case and prefers the most recently synced row for a reused name
- Best: the top 50 by pp, highest first, without failed plays (Postgres sorts NULLs
  first) or other players' scores; recent: the newest 20, failed plays included
- A new player is synced and served; a repeat request (any letter case) is served from
  the database; a stale player is resynced and the response shows the new values; an
  unknown player is `404` and stores nothing

*Whole app* (5, the full stack running): over real HTTP, `/health`; peppy's profile with 50
best plays sorted by pp; a repeat request served without a new sync; `404` for an unknown
player; the endpoint in the OpenAPI docs.

*Worker* (3, the full stack running): `osu_sync` through the real Celery worker returns
peppy's summary, repeated syncs don't duplicate rows, and an unknown player fails the task
with `OsuNotFoundError`. It writes to the dev database, like the app.

Both suites were checked against deliberately broken code:
- `auth.py`: removing the expiry margin, the `400`/`401` check, or the cache read, sending
  the masked secret, or putting the reply in the error message
- `ratelimiter.py`: allowing one too few (`<` for `<=`), keeping refused entries, dropping
  the key expiry, a wrong wait time, or non-unique members
- `retry.py`: sleeping after the last attempt, no backoff for network errors, no cap on
  waits, accepting `Retry-After: inf`, narrow additive jitter, retrying every exception,
  allowing `max_attempts=0`, or retrying `404`
- `schemas.py`: forbidding extra fields, not freezing, making ranks or `pp` non-nullable,
  dropping the `od` alias or also accepting `od` by name, dropping a hit count, or
  loosening `mods` to a string
- `client.py`: asking the limiter without waiting, skipping it, not clearing the token or
  not retrying after a `401`, not treating `404` as not found, only counting `5xx` as
  errors, dropping `key=username`, not escaping usernames, an off-by-one `limit` check,
  inverting `include_fails`, or putting the token in an error message
- `models.py`: making `global_rank` or `pp` non-nullable, defaulting the play date to now,
  dropping `CASCADE`, a 32-bit score id, swapping the index columns, an extra index, or
  dropping the timezone. The default case first slipped past `alembic check`, which
  ignores server defaults unless `compare_server_default` is on; it now is
- `sync.py`: not removing duplicates, not setting `updated_at`, committing part-way,
  writing before all data is fetched, scores before the player, updating only some
  columns, ignoring the limit, a wrong play time, or dropping recent scores
- `api/players.py`: returning the method instead of calling it (`scalar_one_or_none`
  without `()`), a case-sensitive lookup, failed plays in best, either list in the wrong
  order, always or never resyncing, no `populate_existing`, no stale fallback, wrong
  status codes, or a zero freshness window. Query bugs are caught by the Postgres tests
  and endpoint logic by the unit tests, which is why CI runs both

Each makes at least one test fail.

## Layout

```
src/osu_coach/
  main.py      FastAPI app
  api/         routers: /health, /players/{username}
  osu/auth.py  osu! API token (client credentials), cached in Redis
  osu/ratelimiter.py  shared rate limit for osu! requests (sliding window in Redis)
  osu/retry.py        retries for temporary failures (backoff, jitter, Retry-After)
  osu/schemas.py      typed models of the osu! API responses we use
  osu/client.py       osu! API client: token, rate limit, retries, typed results
  sync.py      save a player and their scores from osu! (upserts, one transaction)
  config.py    settings from env / .env
  db.py        async SQLAlchemy engine + session dependency
  models.py    SQLAlchemy declarative base
  worker.py    Celery app and the osu_sync task
migrations/    Alembic
tests/
  unit/        no services needed
  fixtures/osu/  saved real osu! API responses
  integration/ real Redis + osu! API
```
