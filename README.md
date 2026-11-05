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
- `POST /replays/upload` (a multipart `file`, at most 4 MB): stores an osu! standard
  `.osr` replay (from stable or lazer) with its frames, and fetches its map in the
  background. `201` for a new file, `200` for one uploaded before, `422` for a file that
  isn't a valid replay, `413` for one that's too large
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

CI runs the unit tests and the Postgres tests (tables, migrations, sync and the players
endpoint) on
every push.

On Windows, async database code needs a selector event loop (psycopg can't use the
default one); `tests/conftest.py` sets it for the tests, and the worker task sets it too.

**Unit tests** ([`tests/unit/`](tests/unit), 752 tests): call the code directly with nothing
running. `respx` fakes the osu! token endpoint (and blocks any unmocked request),
`fakeredis` stands in for Redis, and the rate limiter gets a fake clock, so a 60 s window
runs instantly.

*osu! token* (22):
- `get_token` cache: a cache hit makes no osu! request, a miss fetches and stores, a second
  call reuses the token, the TTL is `expires_in` minus a 60 s margin, and an expired entry
  triggers a new fetch; a token living 60 s or less is used but not cached (Redis rejects
  an expiry of 0 or less)
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

*API models* (52), using real osu! responses saved in
[`tests/fixtures/osu/`](tests/fixtures/osu) (a profile, three best scores, a failed play,
and a full beatmap):
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
- Full beatmaps: `OsuBeatmap` extends a score's beatmap with max combo, checksum, status,
  mode and the mapset (artist, title, creator), inheriting the `od`/`hp` aliases; the
  mapset is not typed as a difficulty; a score's partial beatmap isn't accepted as a full
  one

*API client* (50), with osu! faked by `respx`, fakeredis, a fake clock for the rate limiter,
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
- Beatmaps by id, and by the MD5 checksum a replay records: sent as `?checksum=` (not in
  the path), lowercased first; 8 kinds of bad checksum (wrong length, not hex, spaces, a
  trailing newline, dashes) are refused before any request, token fetch or rate-limit
  slot, with the rejected value in the message

*`.osu` files* (29), with osu.ppy.sh faked by `respx`, a temporary folder, and two real
map files saved in [`tests/fixtures/osu/`](tests/fixtures/osu) (one with `\r\n` line
endings, one with `\n`):
- Downloads go to `https://osu.ppy.sh/osu/{id}` without a token, through the shared rate
  limit (every attempt, retries included), and are saved byte for byte
- A cached copy whose MD5 matches the checksum (in either letter case) is used with no
  request; a corrupted or outdated copy is downloaded again
- osu!'s answer to an unknown map (`200` with an empty body) is `OsuNotFoundError`; a
  download that doesn't match the checksum is `BeatmapFileError` and writes nothing, and
  never replaces a good cached copy; HTTP errors are `OsuApiError`
- Files are written to a temporary file and renamed: a write that fails halfway leaves
  the old file untouched, and no `.tmp` files are left behind

*`.osu` parser* (68), on the two real map files and small hand-written ones:
- Both real maps give exactly the circle, slider and spinner counts and the AR/OD/CS/HP
  osu!'s API reports; objects come in time order; `\r\n` and `\n` files parse the same;
  the result is read-only
- The format's quirks: a BOM or blank lines before the version line, a space after the
  colon (`SliderMultiplier: 1.4`), no `ApproachRate` line (AR = OD, as osu! does for old
  maps), 2-field timing points, type bit flags (`5` is a circle starting a new combo, `12`
  a spinner), trailing commas, decimal times, all four slider curve types, extra fields
- Anything broken raises `OsuFileError` naming the line: a file that isn't `.osu`, a
  missing or non-numeric difficulty value, bad timing points, bad or unknown hit objects

*`.osr` parser* (111), on replays built byte by byte (`tests/osrdata.py`), so every field
can be set:
- The header from both osu!stable and osu!lazer: counts and combo as unsigned numbers,
  UTF-8 names (and ones over 127 bytes), the play time exact to the microsecond in UTC, the
  4-byte score id of replays before 2014, Target Practice's extra 8 bytes, and lazer's
  score data block, which gives lazer's mods (Classic too) and the score id lazer leaves
  out of the header. A score id of 0 or -1, or a non-number in lazer's block, is no id
- The map checksum is lowercased and must be an MD5 (32 hex characters, the whole
  string), or osu! could never find the map
- Mods as osu! shows them: `NC` not `NC+DT`, `PF` not `PF+SD`, and a hand-made file with
  only NC's bit doesn't crash
- Frames follow osu!lazer's decoder: times are running totals; stable's two placeholder
  frames are dropped but their time counts (the second sits at the skip point, so losing
  it would shift every frame); frames that step back are skipped but move the clock; the
  seed frame is skipped wherever it is; empty or short entries are ignored
- Anything broken is a `ReplayFileError` with a fixed message: a file cut off at any byte,
  every byte damaged two ways, other game modes, no map checksum, bad strings, dates
  outside year 1-9999, non-numeric or `nan`/`inf` frame values, cut LZMA data, a missing
  or unreadable lazer block or mod list, and no frames. Compressed data that unpacks past
  16 MB (frames) or 1 MB (lazer's block) is refused while unpacking: a 20 MB bomb never
  uses more than LZMA's own 8 MB buffer

*Play conditions* (74), against osu!'s published values (the osu! wiki's tables; osu!'s API
no longer reports AR or OD with mods, so there's nothing live to check):
- Hit windows by OD (80 - 6 OD, 140 - 8 OD, 200 - 10 OD), rounded down to whole ms minus
  half a ms as lazer does; a press's result at each window's edge, early presses inside
  400 ms counting as misses, and presses too early or too late not counting at all
- HR (CS x1.3, the rest x1.4, capped at 10, map flipped) and EZ (halved), with the radius
  following the new CS; DT/NC and HT/DC change the speed but not the windows in song time
- Effective AR and OD with speed mods: AR 10 + DT = 11, OD 10 + DT = 11.11, AR 9 + DT =
  10.33, HT's AR 10 = 9; approach times and circle radii from the wiki's tables
- Mods that change nothing are allowed; Relax, Autopilot, Auto, Mirror, lazer's custom
  difficulty and Wind Up are refused, naming every one, as are HR with EZ and two speed mods

*Storing a replay* (28), with real files and a fake session:
- A new upload keeps the original `.osr` and writes its frames to Parquet, both named by
  the file's SHA-256, then inserts one row and commits; the row holds every header field,
  the frame count, and the frames' path relative to the data folder
- A file uploaded before is only looked up: not parsed, not written again. A bad file
  writes nothing. If another upload of the same file wins the race, its id is returned
- The frames table keeps every frame exactly, in order, with 64-bit columns (a hand-made
  file can't overflow them), and reads back the same from Parquet
- A write goes to a temporary file and is renamed: a failed write leaves the old file and
  no temporary file. When Windows refuses the rename because another upload of the same
  file is renaming at that moment, the other writer's identical file is kept
- The replay's map: a map stored with the exact file and its hit objects isn't fetched
  again (osu! isn't asked); a missing one is looked up by checksum and fetched, then
  checked again; if osu!'s file is a newer version, `MapChangedError`; osu! errors pass
  through with nothing fetched. The check is lowercased and needs the hit objects

*Uploading* (15, `POST /replays/upload`), with a fake store, session and queue:
- A new file is `201` with the replay (not its hash, file path or game version), and its
  map is queued; the same file again is `200`; a stored map is never queued; a repeat
  upload queues a still-missing map again (an earlier fetch may have failed)
- A bad file is `422` with the parser's reason and nothing else happens; a file of exactly
  4 MB is accepted and one byte more is `413` without parsing; a missing file (or the
  wrong field name) is `422`; the documented responses are 200, 201, 413 and 422

*Worker tasks* (2): the tasks' names, and the errors declared as expected (`throws=`), so
Celery logs a missing player, a taiko map or a changed map in one line instead of a
traceback.

*Fetching a map* (16), with a fake client, file store and session:
- One map is fetched, its file is asked for with osu!'s checksum, and the writes go map
  row (with its max combo) -> old objects deleted -> new objects inserted -> one commit;
  every object is stored with its features
- Other game modes, a file whose object counts don't match osu!'s, osu! errors, a bad
  download and a broken file are all refused before anything is written

*Pattern features* (58), on the two real maps and small hand-built ones:
- Slider end times from the timing in effect: BPM sections, slider speed changes (capped
  at 0.1x-10x, reset by a new BPM section), repeats, and the first timing point for
  objects before it. On the real maps no slider runs into the next note, and at least 95%
  end exactly on the beat grid (map 75's one freely drawn 2007 slider is checked on its own)
- Circle radius from CS; spacing and angle between objects, including stacked notes and
  floating-point rounding; no spacing or angle next to spinners
- Labels: streams (1/4 beat or faster, close together) and jumps (1/2 beat or faster, far
  apart), each at its limits, plus slider, spinner and other; known counts for both maps

*Database tables* (69), checked from the model definitions without a database:
- `players`, `scores` and `beatmaps` keyed by osu!'s own ids (64-bit, not generated by
  the database); scores reference players with `ON DELETE CASCADE` and beatmaps without
  it (a map with scores can't be deleted); the map checksum is unique
- Each column is nullable exactly when the osu! API field it comes from can be null (42
  column/field pairs), so real data never fails on `NOT NULL`
- `hit_objects` is keyed by (map, position), belongs to its map (`ON DELETE CASCADE`), and
  only spacing, angle and time since the previous object can be null
- Constraint names follow a naming convention that matches Postgres's own, so migrations
  can refer to them
- The play date has no default; `updated_at` is set by Postgres; times keep their timezone
- Only the indexes queries use: `username` (not unique: names can be reused), `beatmap_id`,
  and `(player_id, pp)`; the checksum's unique constraint is its index

*Sync* (31), with a fake osu! client serving the saved responses and a fake session whose
statements are compiled to Postgres SQL:
- `player_values` / `score_values` / `beatmap_values` fill every column from the right
  osu! field; a score's map and a full map give the same row except the max combo
- Upserts are `INSERT ... ON CONFLICT DO UPDATE` on the osu! id, updating every column but
  the id, and `updated_at = now()` for players; all scores go in one statement, with
  duplicates (a play in both best and recent) removed first
- Maps go in one statement too, deduplicated (retries of one map), with
  `max_combo = COALESCE(new, stored)` so a score's map data never erases a known value
- Order: maps, then the player, then scores, then one commit; scores are fetched by the
  resolved id; limits are passed on
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

**Integration tests** ([`tests/integration/`](tests/integration), 146 tests): use real
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

*API client* (13, real osu! and Redis):
- The same player by username and by id; 50 best scores, best first, and `limit=5`
  matching the first five; recent plays of the current top 10
- Unknown usernames, including the all-digit `"2"`, raise `OsuNotFoundError`
- A rejected cached token recovers: user (401), new token, user again
- Each API request takes exactly one rate-limit slot
- Beatmap 22423 matches the saved fixture; looking it up by its checksum (either case)
  finds the same map, as does looking up a real score's map; unknown ids and checksums
  are not found; a bad checksum never reaches osu!

*Database* (24, Postgres only; also run in CI): a separate `<db>_test` database is created,
migrated with Alembic exactly as deployment does, and dropped afterwards
- The migration reaches head, `alembic check` finds nothing to change, and it can be
  undone and redone; nullability, both foreign keys, the unique checksum and indexes match
  in Postgres, and every constraint has the name the convention gives
- Real users, best scores (with `HD`/`DT`) and a failed play without pp round-trip,
  including the play time's timezone and a 64-bit score id
- A score for an unknown player or map is refused, deleting a player deletes their scores,
  deleting a map with scores is refused, two maps can't share a checksum (but any number
  can have none), and a missing play date is refused
- "A player's best scores" uses the `(player_id, pp)` index, already in pp order (no sort)

*Migrations on existing data* (5, Postgres; also run in CI): each test starts at the
revision before the beatmaps table, adds a player and scores on maps that aren't stored
(the state a deployed database was in), then upgrades
- The upgrade succeeds by removing those orphaned scores, keeps players, marks them stale
  so they re-sync, and adds the foreign key
- Downgrading keeps players and drops the new table; upgrading again works; an empty
  database upgrades too

*Sync* (17, Postgres; also run in CI), on its own test database:
- A first sync saves the player and scores; a second gives the same rows (idempotent),
  moves `updated_at`, and updates changed values
- Failed plays are saved without pp; a play in both lists is saved once; mods and the
  play time survive
- All or nothing: an osu! failure saves nothing, and a database error while saving scores
  rolls back the player too
- Maps: every score's map is stored once with the right values (including the failed
  play's), retries of one map store it once, re-rated maps are updated, a known max combo
  survives a sync and a new one replaces it, and failures roll the maps back too
- A real sync of peppy from osu! stores every score's map, once (needs Redis and
  credentials)

*`GET /players/{username}`* (14, Postgres; also run in CI): the real queries, and the
endpoint end to end with a sync that runs the real `sync_player` against a fake osu!
- Lookup ignores letter case and prefers the most recently synced row for a reused name
- Best: the top 50 by pp, highest first, without failed plays (Postgres sorts NULLs
  first) or other players' scores; recent: the newest 20, failed plays included
- A new player is synced and served; a repeat request (any letter case) is served from
  the database; a stale player is resynced and the response shows the new values; an
  unknown player is `404` and stores nothing

*`.osu` files* (8): real downloads (needs Redis) match the saved files byte for byte,
the API's checksum matches the downloaded file (including for a real score's map),
a second request uses the cache, and an unknown map is not found. With the stack running:
`/app/data` belongs to the app user, and a file the worker downloads is readable by the
API through the shared volume.

*`.osu` parser* (1): about 20 real maps (peppy's best scores for old file formats, the
current #1 player's for new ones) are downloaded and parsed; every one matches osu!'s API
on object counts and AR/OD/CS/HP. Their pattern features are computed too: no slider runs
into the next note on any of them, and both streams and jumps are found.

*`.osr` replays* (9), on the real replays in `data/replays/` (gitignored: they're other
players' plays; skipped if there are none). Each reads with frame times that never go
back, the score id matches the website's file name, and (needs Redis and credentials)
osu! finds the map by the replay's checksum, matching the file name's map id; the judged
hits fit the map's object count, the frames run from before the first object to past the
last, and a full combo has at least one key press per circle and slider.

In Postgres, map 22423's 254 objects round-trip with their features, need their map,
are deleted with it, and can't share a position.

*Fetching a map* (10, Postgres; also run in CI): the map and its 254 objects are stored;
fetching again gives the same rows; objects from an older, bigger version of the map are
removed; other maps' objects are left alone; a mismatch stores nothing; a failure while
storing the objects rolls the map back too; and a real fetch from osu! (needs Redis and
credentials). `maps_to_fetch` lists a player's osu! standard maps without hit objects:
not maps already fetched, other modes or other players' maps, and each map once.

*Storing a replay* (14, Postgres; also run in CI): the row holds the replay exactly
(play time with its time zone, mods as a list); the same file twice is one row; five
uploads of one file at the same moment give one row and one id; a bad file stores no row;
the database itself refuses a second row for one file. A replay finds its map by checksum
once the map is stored, and not a newer version of the map. The real replays in
`data/replays/` are stored and their frames read back. The replay's map (with the fake
osu! and file store, map 22423) is fetched with its 254 objects, not fetched again once
stored, found in either letter case only with its hit objects, and a newer version is
stored but refused as the replay's map.

*Whole app* (7, the full stack running): over real HTTP, `/health`; peppy's profile with 50
best plays sorted by pp; a repeat request served without a new sync; `404` for an unknown
player; the endpoint in the OpenAPI docs. Day 3's goal: uploading a replay on map 22423
(whose objects are removed first) stores the replay and queues the map, the worker stores
all 254 objects, and uploading again is `200` with the same replay and nothing queued; a
bad file is `422`.

*Worker* (8, the full stack running): `fetch_beatmap` stores map 22423's 254 objects and
refuses a taiko map. `fetch_replay_map` finds map 22423 by its checksum (and doesn't fetch
it again, in either letter case), and fails with `OsuNotFoundError` for an unknown file. A sync queues `fetch_beatmap` for the player's maps without objects
(a removed map is fetched back with all 254 objects), and a second sync queues none.
`osu_sync` through the real Celery worker returns
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
- beatmaps (`client.py`, `schemas.py`): the checksum in the path, not lowercasing,
  loosening or dropping the checksum check, `match` instead of `fullmatch`, an error
  message without the value, a wrong path, the mapset typed as a difficulty, a
  non-nullable max combo, or no checksum field
- `models.py`: making `global_rank` or `pp` non-nullable, defaulting the play date to now,
  dropping `CASCADE`, a 32-bit score id, swapping the index columns, an extra index, or
  dropping the timezone. The default case first slipped past `alembic check`, which
  ignores server defaults unless `compare_server_default` is on; it now is
- beatmaps table and sync (`models.py`, the migration, `sync.py`): no foreign key, CASCADE
  on maps, a non-unique checksum, a non-nullable max combo, no naming convention, the
  migration without its orphan cleanup or without marking players stale, no `COALESCE`,
  maps not deduplicated, not saved or saved after the scores, OD and HP swapped, or the
  star rating from the wrong field
- `files.py`: not checking the download or the cached copy against the checksum, not
  lowercasing it, accepting an empty body, ignoring HTTP errors, writing directly instead
  of through a temporary file, skipping the rate limit, normalizing line endings, not
  creating the folder, or hashing decoded text instead of the bytes

- `osufile.py`: testing the type with `==` instead of bit flags, ignoring new combos, no
  AR fallback, 2-field timing points read as inherited, keeping a BOM, not skipping
  comments, a bare `except` that swallows errors, accepting unknown types, swapping slider
  point coordinates, or 0-based line numbers. Two more breaks (not stripping values, and
  splitting only on `\n`) change nothing, because `float()` ignores surrounding spaces and
  every line is stripped anyway

- `patterns.py`: a wrong radius formula, no fallback before the first timing point, an
  uncapped or inverted slider speed, ignoring repeats, angles in radians or without the
  rounding clamp, spinner positions used, jumps without a distance check, sliders labelled
  like circles, or streams without a spacing limit. Not resetting the slider speed at a
  new BPM section changes nothing: earlier sections' speeds are already skipped

- `beatmaps.py`: accepting other game modes, not checking the file's object counts,
  fetching the file without its checksum, dropping the max combo, not deleting old
  objects, deleting every map's objects, never committing, or storing objects before
  their map
- `maps_to_fetch` listing other modes, maps that already have objects, every player's maps,
  or a map twice; caching tokens that live 60 s or less; tasks without `throws=`
- `osrfile.py` (19 breaks): dropping the placeholders' time (osrparse's behaviour, which is
  why the parser reads the format itself), placeholders anywhere, keeping frames that step
  back or not moving the clock for them, reading the seed frame, an 8-byte score id for
  old replays, not skipping Target Practice's accuracy, signed counts, keeping id 0,
  ignoring lazer's id or taking JSON `true` as one, `NC+DT`, lazer mods from the bits, no
  unpack limit, accepting cut LZMA data or `nan` positions, an uncaught date overflow,
  accepting other modes or an empty checksum
- `replays.py` and its migration (15 breaks): parsing before looking the file up, no
  `ON CONFLICT`, no fallback after a lost race, no commit, an absolute frames path, not
  keeping the original file, writing in place or leaving the temporary file, not creating
  folders, a 32-bit time column, dropping the score id or player name, a wrong frame
  count, and a migration without the unique hash or with a time zone-less play time.
  Breaking the folders first hung the test run: one failed upload left the others'
  sessions holding locks, so the test now uses a `TaskGroup` and the cleanup a lock timeout
- Uploading and the replay's map (16 breaks): an off-by-one size limit or reading only up
  to it, a repeat upload still `201`, an uncaught bad file (`500`), always queueing the
  map, never re-queueing on a repeat, queueing the wrong value, a stored-map check without
  hit objects or lowercasing, no check before or after fetching, ignoring every refused
  rename, a checksum not lowercased or not checked as an MD5 (or with `match` instead of
  `fullmatch`), and the task without `MapChangedError` in `throws=`
- `difficulty.py` (20 breaks): one slope for the whole 0-10 scale, windows not rounded down
  or without the half ms, strict window edges, late presses as misses or no early misses,
  HR uncapped or with CS x1.4, EZ ignored, no HR flip, DT at 1.33x, windows scaled by speed,
  no radius factor, the radius from the unmodded CS (caught after adding a check), unknown
  mods, HR with EZ or two speed mods allowed, Relax as neutral, and effective AR/OD ignoring
  the speed or using one slope

`.gitattributes` marks `.osu` files as binary: git's `\r\n` to `\n` conversion would change
a map file's MD5 and break the fixtures on other machines.
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
  api/         routers: /health, /players/{username}, /replays/upload
  osu/auth.py  osu! API token (client credentials), cached in Redis
  osu/ratelimiter.py  shared rate limit for osu! requests (sliding window in Redis)
  osu/retry.py        retries for temporary failures (backoff, jitter, Retry-After)
  osu/schemas.py      typed models of the osu! API responses we use
  osu/client.py       osu! API client: token, rate limit, retries, typed results
  osu/files.py        download and cache .osu files, verified by MD5
  osufile.py   parse .osu files: difficulty, timing points, hit objects
  patterns.py  per-object features: slider end times, spacing, angle, pattern labels
  beatmaps.py  fetch one map: details, verified .osu file, hit objects (fetch_beatmap task)
  osrfile.py   parse .osr replays (stable and lazer): header, mods, frames
  difficulty.py  play conditions: mods applied, hit windows, circle radius, approach time
  replays.py   store an upload (original, Parquet frames, row); fetch the replay's map
  sync.py      save a player and their scores from osu! (upserts, one transaction)
  config.py    settings from env / .env
  db.py        async SQLAlchemy engine + session dependency
  models.py    the database tables
  worker.py    Celery app and its tasks: osu_sync, fetch_beatmap, fetch_replay_map
migrations/    Alembic
tests/
  unit/        no services needed
  fixtures/osu/  saved real osu! API responses
  integration/ real Redis + osu! API
```
