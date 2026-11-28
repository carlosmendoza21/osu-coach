# osu! AI Coach

Analyzes osu! replays hit by hit, builds a skill profile (aim, speed, accuracy, consistency,
stamina, reading), and recommends practice maps.

## Run it

```bash
docker compose up --build
```

- API: http://localhost:8000 (docs at `/docs`, health at `/health`)
- `GET /players/{username}`: a player's profile, top 50 plays by pp and latest 20 plays,
  each with its map (artist, title, difficulty, star rating).
  Synced from osu! through the worker when the stored copy is missing or older than
  10 minutes; otherwise served straight from the database
- `POST /replays/upload` (a multipart `file`, at most 4 MB): stores an osu! standard
  `.osr` replay (from stable or lazer) with its frames, then in the background fetches its
  map (if needed) and analyzes it hit by hit: UR, timing bias, aim error, results by
  pattern. `201` for a new file, `200` for one uploaded before (with its analysis once
  done), `422` for a file that isn't a valid replay, `413` for one that's too large
- `GET /replays/{id}`: a replay with its map and analysis: our 300/100/50/miss counts and
  whether they match osu!'s, UR, timing bias, aim error, and results by pattern (jump,
  stream, slider, other). `"pending"` with empty analysis until it's done; `404` for an
  unknown id
- `GET /replays/{id}/charts`: the data behind an analyzed replay's charts: each judged
  note's timing error (in real time), aim error, position as the player saw it (flipped
  with HR) and pattern, a 64 x 48 grid of where the cursor spent its time, and the play's
  300/100/50 windows (from the map's OD and the mods, in real time). `409` until the
  replay is analyzed
- `GET /players/{osu_id}/skills`: a player's six skills (aim, speed, accuracy,
  consistency, stamina, reading), each 0-100, with the strongest and weakest and how many
  plays they were scored from. Scored again each time their maps' attributes are fetched
  (after every sync); `409` until the first scoring, `404` for an unknown player
- `GET /players/{osu_id}/recommendations`: 10 practice maps for the player's weakest
  skill, each with a reason from real numbers ("AR 9; 4.3★, 0.6★ above your comfort
  level (3.7★)"). The maps in a star range around their comfort level (the weighted
  star rating of their plays, with mods) that are highest in what the skill needs
  (jumps and wide gaps, streams, long streams, AR, OD or length), then the nearest their
  style (pgvector), none they've played and one per mapset. Made with the skill
  profile; `409` until then, `404` for an unknown player
- `POST /players/{osu_id}/report`: a coaching report: a summary, the 2 strongest skills
  and the 3 weakest (weakest first), each with evidence from the player's plays and
  uploaded replays (matched by name), a practice plan, and the practice maps with their
  reasons. Code decides every fact (which skills, their order, every number, the
  maps). With `COACH_LLM` set, a model writes the words (summary, explanations, plan)
  after looking things up through four tools, and every number it writes must be one
  of the facts; otherwise, or if it fails, the template report is sent with
  `fallback_reason` saying why. `"source"` says which (`"llm"` or `"template"`), `model`
  who wrote it and `tools_used` what it looked up. `409` before the player is scored,
  `404` for an unknown player. Asked for with `Accept: text/event-stream`, it streams
  instead (server-sent events): a `step` event for each thing the coach does ("Looking
  up your skills"), then the `report` (or an `error`), so a page can show a report that
  takes minutes being written
- Two Celery workers: `sync-worker` runs only player syncs (the `sync` queue), so a player
  search never waits behind background work; `worker` runs the rest (map downloads,
  attributes, replay analysis)
- Candidate maps for the recommender: the `collect_candidates` task finds about 25 well
  known ranked maps in each half-star band from 2 to 9 stars and queues their downloads
  (about 15 minutes the first time, within osu!'s rate limit)
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

The coach's LLM (optional; without it, reports come from templates). To run one locally
for free with [Ollama](https://ollama.com), pull a model and name it in `.env`:

```bash
ollama pull qwen3:8b
```

Then set `COACH_LLM=ollama:qwen3:8b` in `.env` and restart the API (`docker compose up -d
api`); in Docker it reaches Ollama on your machine at `host.docker.internal`. On a laptop
CPU a report takes 2-4 minutes.

New migration after changing `src/osu_coach/models.py`:

```bash
uv run alembic revision --autogenerate -m "describe change"
```

### Evals

The coach's reports are checked against saved eval cases in
[`evals/cases/`](evals/cases): each is one player's coaching facts and practice maps,
saved as JSON so a check needs no database and gives the same answer every run. The
automatic checks: the report's structure (the three weakest skills, weakest first, the
profile's scores, a plan step each), every number in a model's words being a fact, and
the practice maps (for the weakest skill, one per mapset, none played, inside the star
range the recommender allows, each with some of what the skill needs). `check` exits
with 1 if any check fails.

```bash
uv run python -m osu_coach.evals check                          # template reports
uv run python -m osu_coach.evals check --model ollama:qwen3:8b  # a model's (slow)
uv run python -m osu_coach.evals export --all                   # save cases from the DB
```

Exporting picks each player's practice maps again first, so a case's maps match the map
pool it records. On the 26 cases (mostly top players, so the weakest skills are
consistency, stamina and, once, reading), template reports pass every check; on average
a third of the practice maps fall inside the recommender's first star range, before it
widens.

### Frontend

React + TypeScript, built with Vite, in [`frontend/`](frontend). With the API running
(`docker compose up -d`), start the dev server at http://localhost:5173:

```bash
cd frontend
npm install
npm run dev
```

Pages: `/players/{name}` (a profile: the skills radar, a "Coach me" button that shows the
coach working step by step and then its report, the practice maps, and the plays),
`/replays/new` (upload an `.osr`), and
`/replays/{id}` (a replay's analysis: counts against osu!'s, UR, timing, aim, results by
pattern, then three charts: hit timing over the 300/100/50 windows, UR over the map with
each miss marked, and where the cursor was, with misses, 50s and 100s marked; it checks
again every 2 s until the analysis is done). The charts (Recharts) load as a separate file,
only on an analyzed replay's page.

The dev server forwards `/api/...` to the API on port 8000 (`API_URL` to change it), so
the page and the API share one address. Checks, as in CI's frontend job:

```bash
npm run lint
npm run typecheck
npm test
npm run build
```

`npm run test:live` also calls the running API (see Testing).

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

**Unit tests** ([`tests/unit/`](tests/unit), 1296 tests): call the code directly with nothing
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

*API models* (57), using real osu! responses saved in
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
- A real map search page (its first 4 mapsets): the sets in order with their play
  counts and every difficulty of every mode (catch and taiko too), each with what a map
  row needs (`od`/`hp` from `accuracy`/`drain`, max combo, checksum); the last page's
  null cursor and an empty result parse; a page without its sets is refused

*API client* (62), with osu! faked by `respx`, fakeredis, a fake clock for the rate limiter,
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
- A map's difficulty attributes: a POST with the mods and `"ruleset": "osu"` as JSON,
  through the same rate limit, retries and token refresh as the GETs; `404` is not found;
  an answer with only a star rating (osu!'s answer for an unranked map) is an
  `IncompleteAttributesError`, while any other broken answer is raised as it is
- Beatmaps by id, and by the MD5 checksum a replay records: sent as `?checksum=` (not in
  the path), lowercased first; 8 kinds of bad checksum (wrong length, not hex, spaces, a
  trailing newline, dashes) are refused before any request, token fetch or rate-limit
  slot, with the rejected value in the message
- Map search: ranked (`s=ranked`) osu! standard (`m=0`) mapsets, most played first, with
  the star range in the text query (`stars>=5.0 stars<5.5`); a first page sends no
  cursor and the next sends the last one's; rate limited and retried like the rest

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

*Judging a replay* (43), on small maps and presses built by hand (OD 10, CS 4, AR 10):
- Timing: each window's edge, early presses inside 400 ms as misses, a press 401 ms early
  ignored (the next one still hits), a late press finding the circle already missed, and
  circles never pressed or after the replay ends as misses
- Aim: the cursor must be within the radius (36.4 px hits, 36.6 doesn't), the aim error
  is the distance from the center, HR flips the map and shrinks the circles
- Keys: M1/K1 are "left" and M2/K2 "right"; holding is one press, K1 while M1 is held
  isn't a new press, the other button is, both in one frame are two presses, smoke isn't
- A press goes to the earliest unjudged circle under the cursor that has appeared;
  stacked circles are hit in order
- Note lock: a later circle shakes until the latest earlier one is judged or its time
  has come, then hitting it misses every earlier circle still waiting (marked forced)
- Slider heads are judged like circles; spinners are left out for now; maps in file
  formats before v5 play 24 ms later

*Analysis* (31), with hand-made judgements and a fake session:
- UR is 10 x the population spread of the timing errors (two hits 5 ms either side give
  50, not 70.7); the mean error shows early/late bias; the aim error is the mean distance
  from circle centers. Only hits count: a miss, even an early press, has no timing
- With DT or HT, timing is in real time (DT's 15 ms of song time is 10 ms); aim isn't
- Fewer than two hits give no stats rather than a meaningless spread
- Storing: delete this replay's results, insert one row per judgement (an early miss keeps
  its press; the note-lock flag is kept), in batches of 1000 (none for zero rows), then the
  summary on the replay with the database's time, then one commit
- Results by pattern: every result listed (0 where none), joined through the map the
  replay was analyzed against, object by object, for this replay only
- Analyzing a stored replay, on map 22423 with replays built to play it: the map's file is
  asked for with the replay's checksum, all 251 circles and slider heads are judged and
  stored, and `matches_official` compares every count with the file's (false here when
  osu! also counts the 3 spinners, true when the header agrees; skipping a stacked note
  lets the next tap hit it, a 50, and misses the next). DT's speed reaches the stats, HR
  flips the map; an unknown replay, a map not stored yet, or RX store nothing
- Our counts from the stored results: 0 where a result never happened, and none at all
  (not four zeros) for a replay not analyzed yet

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

*Uploading* (19, `POST /replays/upload`), with a fake store, session and queue:
- A new file is `201` with the replay (not its hash, file path or game version), and its
  analysis is queued, after fetching the map if it isn't stored; the same file again is
  `200`; a stored map is never fetched again; a repeat upload of a replay not analyzed yet
  queues it again (an earlier job may have failed); an analyzed one queues nothing and
  comes back with its UR, timing bias, aim error and analysis time
- Queueing: the analysis alone when the map is stored; otherwise a chain, fetching the map
  then analyzing, so the analysis never runs if the fetch fails; the map job's id is
  chosen up front (a new one each time) so both ids can be returned
- A bad file is `422` with the parser's reason and nothing else happens; a file of exactly
  4 MB is accepted and one byte more is `413` without parsing; a missing file (or the
  wrong field name) is `422`; the documented responses are 200, 201, 413 and 422

*Showing a replay* (10, `GET /replays/{id}`), with a fake session and replaced queries:
- Analyzed: the play, its stats, our counts, `matches_official` (false if any count
  differs, such as one 50 judged a 100), results by pattern, and the map it was analyzed
  against, loaded by id
- Pending: no counts or patterns, and nothing looked up for them; the map with the
  replay's exact file, by checksum, or none if it isn't stored yet
- An unknown id is `404` and nothing else is looked up; an id that isn't a number is `422`;
  the documented responses are 200, 404 and 422

*Map attributes* (24), on osu!'s real answers for map 22423 (nomod and DT):
- The model: every field parsed (star rating, aim, speed, speed notes, slider factor,
  hard-to-aim and hard-to-tap counts), DT harder than nomod, fields osu! adds ignored, a
  missing field an error, read-only
- Mod keys: sorted, "" for none, NC as DT and DC as HT (osu!'s answers are identical), NF,
  SD and PF dropped (they can't change difficulty), HD, FL, EZ and Classic kept
- Storing: one upsert keyed by map and mods, replacing the values but not the fetch time,
  and every attribute has its column

*Skill scoring* (68), on plays built by hand:
- Each play's value per skill: aim (the map's aim difficulty, 3% off per miss), speed (the
  same, times accuracy squared), accuracy (+0.5% per OD over 8, less under), consistency
  (the share of the map's combo held, 5% off per miss, never over 1), stamina (hard-to-tap
  notes, none from a play with more than 3 misses), reading (the AR seen, +0.5 for HD, +1
  for FL)
- The pp-style weighting: best first whatever the order given, weights 1, 0.95, 0.95^2...
  (two values 10 and 0 give 10 / 1.95), the best plays counting most and one fluke unable
  to set the score
- The scales: straight lines between points, flat outside; each runs 0 to 100 upwards,
  and the reference points (a 5-star map's aim is 60, AR 10.5 is 85) are as documented
- The profile: every skill scored to one decimal from its weighted values, all zeros
  without plays, the strongest and weakest, and a better play always scoring higher in
  its skill

*Map pattern vectors* (34), on hand-made objects and map 22423's real file:
- Eleven numbers, each 0-1, and the table's column holds exactly that many
- Shares: none of nothing is 0, not a division error. Long streams: 16 notes in a row
  count and 15 don't, a run to the last note counts, and a jump splits two bursts so
  neither is long. Spacing bands in circle diameters, each starting at its edge (1.0 is
  1-2), so the same 100 px gap is 2-3 diameters at CS 7 but 1-2 at CS 2
- A hand-made map's whole vector; BPM / 300 and stars / 10, held between 0 and 1 even
  for nonsense input; an empty map; values to 4 places; any field by name
- Map 22423 (a 140 BPM Hard): no jumps, 18 stream notes of 254 in short bursts, 84
  sliders, most gaps 1-2 diameters, and every gap in a band

*Chart data* (46, `GET /replays/{id}/charts`), on hand-made frames and a fake session:
- The cursor grid: 8 x 8 px cells by default, each frame in its cell (the corners, the
  middle, a cell's edges), the playfield's right and bottom edges outside it, frames off
  the playfield counted apart, every frame counted once, other grid sizes
- Each note: its result, timing and aim rounded to two places, real time for DT/NC/HT
  (the time axis stays on the map's clock), HR flipping it up and down only, a note never
  pressed with no timing; the query joins this replay's map and keeps map order
- The endpoint: the data with the configured data folder; `404` for no replay and `409`
  for one not analyzed (or with no map), reading no files; documented responses
- The play's windows, worked out by hand: OD 10, 9.5 and 7, HR (OD 9.8), EZ, and DT/NC/HT
  in real time (OD 7 with DT: 25, 55.67, 86.33 ms); a missing map says so

*Worker tasks* (10): the tasks' names, and the errors declared as expected (`throws=`), so
Celery logs a missing player, a taiko map, a changed map, an unknown replay, a map not
stored yet or unsupported mods in one line instead of a traceback. Syncs are routed to
the `sync` queue, and every other task to the default queue (a map download on `sync`
would bring back the wait this avoids).

*Candidate maps* (23), on hand-made mapsets, a real search page and a fake osu!:
- 14 half-star bands from 2 to 9, each starting where the last ends
- Picking a band's maps: the low end in and the high end out (5.4999 in, 5.5 out); one
  per mapset, the one nearest the band's middle; only ranked osu! standard difficulties
  (a taiko or loved one in a ranked set is skipped); the sets' order kept; no more than
  wanted, sets with nothing in the band not counting, and none when none are wanted. On
  the real page: Reol's No title [Insane], Make a Move's 5.42 over its 5.06 and 5.08,
  Harumachi Clover's only one, and quaver's 5.21
- Reading pages: one page when it's enough, more with each last cursor until the band
  is full, the last page or 3 pages ending it, a set on two pages picked once, and pages
  with nothing in the band read past

*LLM client* (28), with Ollama faked by `respx`:
- Requests: no streaming and no thinking (slow on a CPU), tools sent as functions (a
  tool without arguments still with a schema), a schema sent as the format, the URL with
  or without a trailing slash, and the provider and model as its name
- Replies: text, tool calls (with or without arguments) kept for the conversation, and
  no content read as empty text
- Errors: a `404` (model not pulled), a `500`, an answer that isn't JSON, without a
  message, with a broken tool call or a message that isn't an object, no server, and a
  timeout are all `LLMError`, which the coach falls back on (two of these first crashed
  instead: the tests found them)
- The `COACH_LLM` setting: `ollama:qwen3:8b` (the model's own `:` kept, spaces
  trimmed), nothing for an empty one, and a clear refusal for an unknown provider or a
  missing model

*Evals* (47), on cases built by hand:
- Cases: a player's facts saved and loaded back unchanged (with and without replays),
  a case through JSON and back, cases read sorted by file name the same on every system
  (Windows compares paths ignoring case: A, b, C there but A, C, b on Linux; the tests
  caught it), none in an empty folder, and safe file names from usernames (spaces,
  brackets, slashes, `..`, letters outside A-Z)
- Structure: a template report passes; weaknesses out of order, a wrong score, missing
  strengths or plan steps, or other maps each fail, saying what was found
- Numbers: a template's words aren't checked; a model's facts, a tool's raw value (a UR
  of 95.25 written 95.3), made-up numbers (9.7, 99.5, 1,000) and a missing explanation
- Practice maps: good maps pass; none, no target, another skill, two from one mapset, a
  played map, the edges of the allowed range (bottom in, top out), a map with none of
  the skill's need; the allowed range itself (a step up, widened, centred on the hardest
  map for a level above the pool, at the comfort level for accuracy) and none without
  its inputs; the share in the first range as a measurement
- The summary and the command: pass rates and means, fallbacks and failures listed;
  `check` passing, failing (exit 1), with no cases, and limited; `export` needing players

*LLM coaching report* (44), with a fake model that replays scripted answers:
- The four tools, by the brief's names: the skill profile with every skill's evidence,
  the replays (raw UR, notes per pattern), misses by pattern worst first, the practice
  maps; without replays or maps they say so; an unknown tool is an answer, not a crash
- The number check: facts include the evidence's numbers and every tool's raw values
  (a UR of 85.25 may be written 85.3); counts up to 10 written whole are fine; a decimal
  ("AR 8.0") must be a fact; 0.05 of rounding allowed and no more; a thousands comma
  read as one number; every part of the words checked
- Merging: the facts stay code's (the weaknesses, their order, scores, evidence,
  strengths and maps) and only the words are the model's, whatever order it wrote them
  in; a weakness left out, or a number not in the facts, is refused
- The loop: what the model is asked (the rules, every tool, the tools' answers sent
  back, then the schema with the weaknesses as code ranked them), stopping after 4
  rounds of lookups, a model that writes at once, and the tools used recorded. An
  unreachable model, a failure while writing, an answer that isn't JSON or misses
  parts, a made-up number, or no plan each give the template report with the reason
- Steps, for the page: "Reading your profile", one for each lookup (every tool has one;
  an unknown tool still gets "Looking up ..."), "Writing your report", in order; a
  failure stops them; and they're optional

*Coaching report* (29), on hand-made plays and replay numbers:
- Evidence from plays, one sentence set per skill with its numbers: the hardest-aim and
  fastest passes, average and best accuracy and the highest OD, combo held (capped at
  100%, maps without a combo left out) and plays without misses, the most hard-to-tap
  notes cleared with 3 misses or fewer (none when every play has more), the highest and
  average AR and plays with Hidden; nothing without plays; an unknown skill refused
- Evidence from replays, each to its skill: UR to accuracy, jump misses to aim, stream
  misses to speed, all misses to consistency, nothing to stamina or reading; nothing
  without replays, a pattern they don't have, or a UR
- The report: skills best first (ties in the usual order); the 2 strongest and the 3
  weakest, weakest first, with play and replay evidence; the plan's first step points
  at the practice maps and later ones don't (none does without maps); the summary with
  and without replays; the player, the maps and `"source": "template"`

*Recommending* (37), on hand-made maps:
- The star range: a step up (+0.2 to +0.7) for most skills, at the comfort level (-0.4
  to +0.1) for accuracy and consistency, 0.5 more each side per widening, rounded
- What each skill needs, one per skill: jumps plus wide gaps, streams, long streams as
  a share of all notes (none without streams), AR, OD, length
- Choosing: the 20 highest in the need (maps with none left out), then the 10 of those
  nearest the style, nearest first; the nearest maps cut when they're lowest in the
  need; one difficulty per mapset, the first kept; nothing to choose from gives nothing
- Reasons: each skill's numbers ("62% jumps, 31% of gaps over 4 circle widths", "4:05
  of play to hold a combo through", "AR 10" without ".0"), then the level: above, below,
  or right at it within 0.05 stars; an unknown skill is refused

*Fetching a map* (17), with a fake client, file store and session:
- One map is fetched, its file is asked for with osu!'s checksum, and the writes go map
  row (with its max combo) -> old objects deleted -> new objects inserted -> its pattern
  vector stored (the same numbers the vector tests get for this map) -> one commit;
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

*Sync* (63), with a fake osu! client serving the saved responses and a fake session whose
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
- Plays osu! sends with id 0 (every failed play, and passed plays it hasn't processed
  yet; a real saved failed play has it): a play with an id is kept as it is; a failed
  play gets a stable id of its own from the player, the map and the time, negative (never
  a real id), within a BIGINT and never 0, the same on every sync and different for
  another player, map or time (a second later); a passed one is skipped until it has its
  id, and so is its map, and the sync counts it; the order is kept

*`GET /players/{username}`* (23), with a fake sync, replaced queries and a fake session:
- A fresh player is served without syncing; a stale or unknown one is synced, then
  reloaded by the id the worker returned (`populate_existing`), so the response shows the
  synced values; the username is passed on as typed
- `404` when osu! doesn't know the player; with no stored data, `502` for an osu! error
  and `504` for a timeout; with stale data, that data is served instead (graceful
  degradation)
- The response has exactly the public fields; the worker is waited on in a thread with a
  30 s timeout
- Each score comes with its map (id, mapset, artist, title, difficulty, stars), looked up
  in one query for both lists, and none at all for a player with no scores; a map that
  isn't stored is `null` rather than dropping the score

*`GET /players/{osu_id}/skills`* (13), with a fake session:
- A scored player gets their profile, looked up once; a player we have but haven't
  scored is `409` "isn't ready yet"; an unknown player, or one whose id only another
  player's profile has, is `404`
- The response has exactly the profile's fields, with the time in UTC; a username, a
  decimal or no id at all isn't an osu! id (`422`, `404`); the endpoint is documented

**Integration tests** ([`tests/integration/`](tests/integration), 294 tests): use real
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

*API client* (17, real osu! and Redis):
- The same player by username and by id; 50 best scores, best first, and `limit=5`
  matching the first five; recent plays of the current top 10
- Unknown usernames, including the all-digit `"2"`, raise `OsuNotFoundError`
- A rejected cached token recovers: user (401), new token, user again
- Each API request takes exactly one rate-limit slot
- Beatmap 22423 matches the saved fixture; looking it up by its checksum (either case)
  finds the same map, as does looking up a real score's map; unknown ids and checksums
  are not found; a bad checksum never reaches osu!
- Map search: 50 mapsets at 5-5.5 stars, each with a ranked osu! standard difficulty in
  the band, most played first, and the next page carrying on without repeats
- Map attributes: nomod, the star rating and max combo are the map's own; DT is harder;
  and osu! itself confirms the mod keys' assumptions: NC's answer equals DT's and SD's
  equals nomod's; an unknown map is not found

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

*Migrations on existing data* (7, Postgres; also run in CI): each test starts at the
revision before the beatmaps table, adds a player and scores on maps that aren't stored
(the state a deployed database was in), then upgrades
- The upgrade succeeds by removing those orphaned scores, keeps players, marks them stale
  so they re-sync, and adds the foreign key
- Downgrading keeps players and drops the new table; upgrading again works; an empty
  database upgrades too
- The shared id-0 score (`656c2b0ee3e6`): from the revision before it, with real scores, a
  failed play with an id of its own and the one id-0 row, the upgrade removes only that
  row; downgrading does nothing and upgrading again finds nothing more

*Sync* (20, Postgres; also run in CI), on its own test database:
- A first sync saves the player and scores; a second gives the same rows (idempotent),
  moves `updated_at`, and updates changed values
- Failed plays are saved without pp; a play in both lists is saved once; mods and the
  play time survive
- All or nothing: an osu! failure saves nothing, and a database error while saving scores
  rolls back the player too
- Maps: every score's map is stored once with the right values (including the failed
  play's), retries of one map store it once, re-rated maps are updated, a known max combo
  survives a sync and a new one replaces it, and failures roll the maps back too
- Failed plays (id 0 from osu!) on one map a minute apart are saved as two rows with ids
  of their own, and syncing one again updates its row; a passed play without its id yet
  is left out, with its map
- A real sync of peppy from osu! stores every score's map, once (needs Redis and
  credentials; it found the id-0 plays: four of peppy's plays shared one row)

*`GET /players/{username}`* (15, Postgres; also run in CI): the real queries, and the
endpoint end to end with a sync that runs the real `sync_player` against a fake osu!
- Lookup ignores letter case and prefers the most recently synced row for a reused name
- Best: the top 50 by pp, highest first, without failed plays (Postgres sorts NULLs
  first) or other players' scores; recent: the newest 20, failed plays included
- A new player is synced and served; a repeat request (any letter case) is served from
  the database; a stale player is resynced and the response shows the new values; an
  unknown player is `404` and stores nothing
- Every score, best and recent, comes with the map the sync stored for it

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

*Judging real replays* (3; needs the replays in `data/replays/`, Redis and credentials):
Day 4's goal. Each replay's map is downloaded and the replay judged: our 300/100/50/miss
counts equal the official ones osu! wrote into the file, every circle and slider head is
judged once, and the timing looks like a real player's (mean near 0, UR 30-200). All
three current replays match exactly (gwb 1186/24/2/9, mrekk 2224/14/0/0 and 2838/18/0/0).

*Analysis* (21, Postgres; also run in CI), on map 22423 and replays built on it: results
and summary stored and read back exactly; analyzing again replaces everything (fewer
objects, no leftover misses); another replay's results are left alone; an unanalyzed
replay has empty columns. Deleting a replay deletes its results; an analyzed map can't be
deleted; results need a replay and are one per object. Results by pattern equal a tally
of the stored objects' labels, cover every object, and are empty before analysis.
Analyzing a stored replay that plays the map (6 ms either side) with a real session,
which expires loaded rows on commit as the worker's does: 251 results, UR 60, the map's
id and analysis time on the replay, the same rows when analyzed twice; a replay whose map
isn't stored, or no replay at all, is refused. Our counts are none before analysis and
exact after; `GET /replays/{id}` against Postgres shows a replay pending (with its map
already) and then analyzed (251 300s, matching osu!, UR 60), and `404` for no replay.
Its chart data: `409` before analysis, then every circle and slider head in map order at
the map file's positions, with the taps' 6 ms either way and every frame counted; a DT
play's timing in real time (4 ms); an HR play's notes flipped as the player saw them;
`404` for no replay.

*Storing a replay* (14, Postgres; also run in CI): the row holds the replay exactly
(play time with its time zone, mods as a list); the same file twice is one row; five
uploads of one file at the same moment give one row and one id; a bad file stores no row;
the database itself refuses a second row for one file. A replay finds its map by checksum
once the map is stored, and not a newer version of the map. The real replays in
`data/replays/` are stored and their frames read back. The replay's map (with the fake
osu! and file store, map 22423) is fetched with its 254 objects, not fetched again once
stored, found in either letter case only with its hit objects, and a newer version is
stored but refused as the replay's map.

*Whole app* (14, the full stack running): over real HTTP, `/health`; peppy's profile with 50
best plays sorted by pp; a repeat request served without a new sync; `404` for an unknown
player; peppy's skills, scored after their sync, each 0-100 with the right strongest and
weakest, and `404` for an unknown id's; peppy's recommendations for their weakest skill,
none they've played, one per mapset, each with its reason, and `404` for an unknown id's;
peppy's coaching report, agreeing with the skills and practice maps endpoints (the same
strongest and weakest, scores and maps), every piece of evidence with a number; the
endpoints in the OpenAPI docs. Day 3's goal: uploading a replay on map 22423
(whose objects are removed first) stores the replay and queues the map, the worker stores
all 254 objects, and uploading again is `200` with the same replay and nothing queued; a
bad file is `422`. Day 4's goal end to end: a replay playing map 22423 is uploaded with the
map's objects removed; the map is fetched and then the replay analyzed by the worker (UR
60, all 251 results stored), and uploading again returns the analysis and queues nothing.
`GET /replays/{id}` then shows it analyzed with its map and every object in its results by
pattern, and its chart data has every note's timing and the cursor's full grid; an unknown
replay is `404`.

*Map attributes* (12, Postgres; also run in CI), two players and four maps: a player
needs each of their osu! standard maps under each mod key (map 1 nomod, HD, and DT for
both DT and NC; map 2 nomod for a PF play), not the taiko map or another player's;
stored pairs drop off, also when stored for another player (they belong to the map).
Fetching stores every pair with its normalized mods and then asks for nothing; a map
osu! no longer has is skipped and counted; a failure part-way keeps what was fetched,
and the next run asks only for the rest. A map osu! has only a star rating for (an
unranked map) is skipped and counted, not stored, and asked for again on the next run.
Storing again replaces the values; deleting a map deletes its attributes; attributes need
their map; the database sets the time.

*A player's plays for scoring* (5, Postgres; also run in CI): a player's passed osu!
standard plays with their map's attributes, matched by mod key (an NC play gets the DT
attributes and the AR and OD seen at 1.5x: 10.33 and 9.78); a play without attributes yet
and a Relax play are skipped and counted; failed plays, taiko plays and other players'
aren't counted at all; a player without plays has none; a profile from them is in range.

*Storing a player's profile* (9, Postgres; also run in CI): a profile is stored with its
six scores, strongest, weakest and play counts, and the database sets the time; storing
again replaces it in place, with a later time; each player has their own; a profile needs
its player, and deleting the player deletes it. Scoring a player stores (and commits) the
profile from their loaded plays; once a skipped play's attributes arrive it counts on the
next scoring; a player without plays is stored with zeros; an unknown player isn't scored
and nothing is stored.

*Map pattern vectors* (13, Postgres with pgvector; also run in CI): a vector is stored with
its version; storing again replaces it in place, with a later time; it needs its map,
and deleting the map deletes it; pgvector refuses a vector one number short. Maps with
objects and no current vector need one (not a map without objects, nor one already
built); an outdated version is built again. A vector built from stored objects matches
one built from the objects directly, with the map's CS, BPM and stars, even when the
rows are stored shuffled (read out of order, two long streams would merge into one);
building every missing vector commits each and then finds none. Ordering maps by
pgvector distance to an all-streams target gives the stream map, then the jump map, then
the slider map.

*Candidate maps stored* (7, Postgres; also run in CI), with a fake osu! serving one page
per band: every band searched once; the candidates' rows stored (the set's artist and
title, the difficulty's own stars, max combo and checksum) and all of them to fetch, in
order; a map fetched since isn't fetched again but is still a candidate; a played map
without objects is left to the sync; running again adds no rows; and osu! failing on the
4th band keeps the 3 bands done.

*Recommendations* (25, Postgres with pgvector; also run in CI), with players, plays and
maps written in by hand:
- The comfort level: a DT play at its DT rating, weighted 1, 0.95... (5.0 and 6.0 give
  5.51); an NC+HD play matched to its "DT,HD" attributes; failed, taiko and
  attribute-less plays left out; none without plays. The maps
  played include failed and taiko plays. The style: the average of the played maps'
  vectors (maps without one left out); none without vectors
- The maps in range: nearest the style first, by pgvector distance; not loved, taiko,
  vector-less or played maps; the bottom end in and the top end out. The hardest map:
  only ranked maps with vectors
- Recommending: the 10 highest-AR maps of 12 for a reading player, from 5.71-6.21 stars;
  none without a profile or plays; a stamina player's range widened twice to reach the
  only long-stream maps; a player above every map gets a range centred on the hardest
  map, so it's in without widening
- Storing: ranks 1-10, best score first, the skill, the comfort level and each reason
  stored and committed; made again, replaced; cleared when no longer possible; another
  player's untouched; deleting the player or a map deletes them
- The endpoint, over this database: the maps in rank order (ranks reversed in the
  database, so neither the picks' order nor the ids') with their details, the
  skill and comfort level; `404` for an unknown player, `409` before scoring, `422` for
  a non-number, and an empty list for a player scored before recommendations existed

*Coaching facts and report* (22, Postgres; also run in CI): a player's replays matched by
name in any letter case, only analyzed ones, not someone else's: their count, mean UR,
notes per pattern and miss rate per pattern from the stored results; none without
replays; no UR when none was measured. A player's facts: their username, scores, plays
(with their maps' attributes), replays and practice maps in rank order with names and
reasons; none for an unscored or unknown player; no maps when none are stored. The
endpoint sends the template report as built; `404` for an unknown player, `409` before
scoring, `422` for a non-number, `405` for a GET. With a (fake) model, the endpoint sends
its words over code's facts; a model that fails gives the template with the reason; and
no model is asked for an unscored player. The tests always set the model themselves, so
a `COACH_LLM` in a developer's `.env` can't reach them. As a stream: a template report
is one `report` event, uncached and unbuffered (`no-cache`, `X-Accel-Buffering: no`);
a model's steps come first, in order, and the report last; a model that fails still
streams the template with the reason; an unexpected failure is an `error` event rather
than a cut-off; a `404` or `409` is known before streaming, so it's a plain JSON error;
and without asking for a stream, it's JSON as before.

*A real model* (1, needs Ollama with the model pulled; skips with the reason otherwise,
as in CI): `qwen3:8b` (or `OLLAMA_TEST_MODEL`) writes a report on hand-written facts in
about 3 minutes on a laptop CPU: the facts are code's, and either every number in its
words is a fact or it fell back to the template saying why.

*Eval cases* (6, Postgres with pgvector; also run in CI), on the recommender tests'
hand-made world: a player's case (their facts, and 10 practice maps for their weakest
skill with ranks, vectors, reasons, the comfort level and the hardest map in the pool);
export picks the maps (and stores them) first; a played map is left out, and one played
after picking is marked in the snapshot; no case for an unscored or unknown player; and
a case through JSON loads back as the coach gathers it, its template report passing.

*Worker* (14, the full stack running): `fetch_beatmap` stores map 22423's 254 objects and
its pattern vector (the one worked out from the file in the unit tests), and refuses a
taiko map; `build_map_vectors` builds a removed vector back the same and then finds none.
One worker takes only syncs and another the background work, and nothing else listens
on `sync`. `collect_candidates` on real osu! finds at least 20 maps in each of the 14
bands. `fetch_replay_map` finds map 22423 by its checksum (and doesn't fetch
it again, in either letter case), and fails with `OsuNotFoundError` for an unknown file.
`analyze_replay` fails with `ReplayNotFoundError` for a replay that doesn't exist. A sync queues `fetch_beatmap` for the player's maps without objects
(a removed map is fetched back with all 254 objects), and a second sync queues none.
`osu_sync` through the real Celery worker returns
peppy's summary, repeated syncs don't duplicate rows, and an unknown player fails the task
with `OsuNotFoundError`. `fetch_attributes` leaves none of peppy's plays without
attributes, and a second run fetches none, and peppy's profile is scored again and
stored as the task reports it, with recommendations for the weakest skill (as many
stored as reported); for an unknown player nothing is fetched and no profile or
recommendations are made. It writes to the dev database, like the app.

**Frontend tests** ([`frontend/`](frontend), Vitest; also run in CI):

*Unit* (346, `npm test`), with `fetch` replaced:
- `api.ts`: each call's URL, under the proxied `/api`, with player names escaped (a space,
  a slash or a `?` can't break the path); an upload posts the file as the `file` form
  field, without a hand-set Content-Type (the browser adds multipart's boundary); the JSON
  comes back as it is; 2xx is success
- Errors: FastAPI's `detail` string, or the first field error's message, becomes an
  `ApiError` with the status; a proxy's HTML error page or another body gives the status
  text, and with none, "request failed (500)"; a network failure passes through
- The home page: "Checking the API..." until the answer, one request to `/api/health`,
  then "API connected", or "API unreachable" for an answer other than ok, a proxy error or
  no answer at all
- Routes: `/`, `/players/:username`, and "This page doesn't exist." for anything else
  (`/players` alone included); the header link home and the search box on every page
- Formatting: pp rounded with thousands separators, a score's 0-1 accuracy and a player's
  percent (not multiplied twice), "unranked", "NM" or "+HDDT", stars, dates, and "time
  ago" in whole units, singular or plural, never in the future
- The search box: trims the name, escapes it into the URL (a space, a slash, a `?`), and
  does nothing for an empty or blank name; names are capped at 32 characters
- The plays table: each column as osu! shows it, the map linked to osu! in a new tab that
  can't reach back, the map's id when its details aren't stored, failed plays dimmed with
  an F and no pp, misses in red, and a line instead of an empty table
- The profile page: one request for the name in the URL (escaped), a loading message, the
  profile and both tables; 404, 502, 504, other errors and no answer each explained;
  searching another player drops the last one at once, and a late answer (or error) for
  the previous player never replaces the new one
- Skills on the profile page (40, with fake timers for the waiting): asked for by the
  player's osu! id only once the profile is in, shown between the profile and the plays,
  and a skills error leaves the rest of the page as it is. The radar's order (aim on top,
  speed beside stamina, accuracy beside consistency), every skill labelled once, and
  "from 1 play" or "from 58 plays (3 not scored yet)". The panel: loading, the strongest
  and weakest, the six scores to one decimal, the radar drawn; a profile not scored yet
  (`409`) is asked for again every 5 s, not sooner, shown when it's in, and given up on
  after 5 minutes with a message; leaving stops the asking; a `404`, a `500` or no answer
  each explained and not asked again; another player's id is asked for, and neither the
  last player's profile nor a late answer for them ever shows for the new one. The radar
  itself, with Recharts given a fixed size: its six labels round from the top, and each
  point as far out as its score on a fixed 0-100 scale (a best of 50 reaches halfway)
- Waiting on the API (13, the hook both panels share, with fake timers): loading, then
  the data asked for once; on a `409`, asked again every poll and not sooner, the data
  once ready, and given up on after the limit (one request per poll until then); any
  other error passed on and not asked again; a new id loading until its own answer, a
  late answer for the last id never shown, and the last id's or a left page's asking
  stopped
- Practice maps on the profile page (15): asked for by osu! id, shown under the skills
  and above the plays, and an error there leaves the rest of the page as it is. The
  heading names the skill and the comfort level; the maps in a numbered list in the
  API's order, each linked to osu! (a new tab that can't reach back) with its mapper,
  stars, rounded BPM, AR, OD, length and reason. Not picked yet (a `409`, or none made
  for a player scored before they existed: opening the page syncs them) shows "Picking
  practice maps..." until they're in, and is given up on after 5 minutes; made but
  nothing fitting has its own message; a `404`, a `500` or no answer each explained
- `formatLength`: minutes and padded seconds (`2:05`, `0:59`, `1:00`), over an hour as
  minutes (`75:00`), rounded, never negative
- The report stream (21): reading events out of text: one, two, half of one kept for
  the next chunk, a data line still waiting for its blank line, Windows line endings,
  `data:` with or without a space, several data lines joined, comments and ids ignored,
  an event without data skipped, and a whole report as sent. `streamReport`: a POST
  asking for a stream, each event in order, events split anywhere across chunks
  (a "★" split between two chunks still decoded whole), a last event without its blank
  line, an empty stream, a `409` or `404` as an `ApiError` with its reason, and the
  signal passed on so it can be cancelled
- The Coach panel (22, on fake timers, with a stream the test feeds): before, a button
  and nothing asked; while it runs, the latest step with the time so far (1:05 after 65
  s), the earlier steps below, the button off; the report: who wrote it (the model, or
  templates and why the model's words weren't used), the summary, each weakness in
  order with its explanation and evidence, the strengths, the plan, and a link to the
  practice maps (none without maps); asking again replaces the report; a `409`, a `404`,
  a `500`, no answer, an `error` event and a stream that ends early each explained;
  leaving cancels the request, asking again cancels the last, and no clock is left
  ticking. On the page it sits between the skills and the practice maps (which have the
  id its link goes to), and asks for nothing until the button is pressed
- Analysis formatting: osu!'s accuracy formula (a 100 a third, a 50 a sixth; 0 objects
  give 0, not NaN; FREEDOM DiVE's counts give osu!'s 99.58%), "1.8 ms early / late / on
  time" (within 0.05 ms), UR and pixels; upload errors (413, 422 with the parser's
  reason) and replay errors (404) in plain words
- The analysis: the map linked to osu!, the player, mods, stars and date (or "Map not
  fetched yet"); accuracy, UR, timing, aim error and combo, with UR and aim explained on
  hover; osu!'s counts and ours side by side, each row its own when they differ, "✓
  matches osu!" or the known reason they differ; patterns in a fixed order (unknown ones
  last), each with its accuracy and misses in red
- The upload page: a picked or dropped file is sent as the `file` field, then the
  replay's page opens; while uploading, the file is named and the picker is off; a
  refusal is explained and another file (even the same one) can be tried
- The replay page, on fake timers: one request for the id in the URL (none for an id that
  isn't a whole number); an analyzed replay shown at once and not asked for again;
  pending says whether the map is being fetched or the replay analyzed, and asks again
  every 2 s (not before) until analyzed; after 90 s it stops and explains; leaving the
  page or an error stops the checking; moving to another replay drops the last one at
  once, and its late answer never replaces the new one
- Chart data: only hits (an early miss too is left out); 2 ms bins whose edges are whole
  so 0 is always one, each error in its bin, errors past the 50 window in the outermost
  bins; UR over each 50 hits (the population spread, hits in time order, a looser section
  higher, one point when there are fewer, none under two); "1:23" times; square-root
  shading (a quarter of the time is half the shade)
- The heatmap's drawing, on a fake canvas: the playfield cleared, each visited cell shaded
  by its time (the busiest fully), circles at each 100 and 50 and an x at each miss, none
  for 300s
- The charts, with Recharts given a fixed size: the histogram's three windows, its 0
  line, a bar per bin with hits and an axis spanning the 50 window; UR's line, a red line
  per miss and a time axis across the whole map in minutes; "too few hits" instead of an
  empty chart; the heatmap's label and legend (and frames off the playfield), drawn with
  the data, and nothing breaking without canvas support; the panel loading this replay's
  charts, an error saying only the charts are missing, and a late answer for another
  replay never replacing them

*Live* (10, `npm run test:live`; needs the API running, skips otherwise): a real Vite dev
server is started with `vite.config.ts`, and `api.ts` calls the API through its proxy:
`/health` answers; an unknown replay is the API's `404` with its reason; an upload reaches
the parser (so the form field name is right); peppy's profile and plays have exactly the
fields `types.ts` declares, no more and no fewer, and every play comes with its map;
peppy's skills (checked until scored) have exactly the declared fields, and an unknown
player's are the API's `404` with its reason; peppy's practice maps (checked until picked)
and each map's fields are exactly as declared, ranked 1 onwards; peppy's coaching report
streams in (steps, then the report last) with exactly the declared fields, and an
unknown player's is the API's `404` before any stream; and a
real replay from `data/replays/` uploads and is checked until analyzed, as the replay page
does, with its detail's fields (every pattern's too) as declared and our counts matching
osu!'s; its chart data has the declared fields and exactly the notes and misses its
analysis counted.

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
- `judge.py` (18 breaks): the press going to the latest circle, a radius 1 px too small, no
  aim check, no HR flip, holding as pressing, M1 not a left press, smoke as a press, one
  press per frame, no note lock, no forced misses, late misses not checked before a press,
  too-early presses as misses, no offset for old formats or one for v5 too, spinners judged
  as circles, a flipped error sign, and unpressed circles left out. Removing the "not on
  screen yet" check changes nothing: a circle appears at least 450 ms early and a press
  more than 400 ms early never counts, so the check only stops the search early
- `analysis.py` and its migration (22 breaks): a sample spread instead of the population's,
  misses' timing counted, song time instead of real time or the rate the wrong way, UR or
  aim from one hit, no delete before inserting or deleting every replay's results, no
  commit, every batch getting all rows, the last batch dropped, an insert even when empty,
  the note-lock flag or aim not stored, no analyzed time or map id, updating every
  replay, the pattern join ignoring the map, the pattern query for every replay, results
  not filled with 0, and a migration without CASCADE or without the map's foreign key
- `analyze_replay`, its task and the upload's queueing (16 breaks): no unknown-replay or
  stored-map check, the map's file without its checksum, mods ignored, the speed not
  passed to the stats, the official counts read after storing (the commit expires the
  row: caught against Postgres), always matching official, the task without
  `ReplayNotFoundError`, analyzed replays queued again, the map always or never fetched,
  the chain in the wrong order or not a chain, the map job's id not given to it or used as
  the analysis id, and the stats left out of the response
- `GET /replays/{id}` and `judged_counts` (13 breaks): no `404`, the map always by checksum
  or always by id, always "analyzed", counts or patterns looked up while pending, matching
  whenever counted, the official 100s read from the 50s, the map left out, the `404` not
  documented, counts not filled with 0, zeros instead of none, and every replay's counts
- The frontend (13 breaks): names not escaped, the wrong upload field name, a hand-set
  Content-Type, error statuses not checked, FastAPI's `detail` or field errors ignored, no
  message without a status text, `ApiError` without its name, no `/api` prefix, the
  health answer not checked, failures not caught, and the proxy keeping the `/api` prefix
  (caught only by the live tests, which use the real proxy). Not ignoring an answer that
  arrives after the page is gone changes nothing visible: React 18 dropped its warning
  for it, so no test can see it; the flag stays as React's recommended pattern
- The profile page and each score's map (20 breaks): pp not rounded, a score's accuracy
  not x100 or a player's x100 again, no rank shown as "#0", no mods as "+", always plural,
  the 404 not explained, the search not trimmed or not escaped, failed plays not dimmed, no
  map id fallback, a map link that can reach back, late answers not ignored, the old
  player shown while loading, the player route renamed, no header search; in the API, maps
  looked up for a player with no scores, not attached, or not for recent plays. Not
  clamping a future "time ago" to zero changes nothing: a negative time is under every
  unit, so it reads "just now" either way
- The replay pages (24 breaks): 100s weighted like 300s, no guard for zero objects, early
  and late swapped, no "on time", the 413 or 404 not explained, the parser's reason
  dropped, no redirect after upload, the picker not cleared, no drag highlight, the
  picker on while uploading, errors not shown, no checking again, checking every 4 s,
  never giving up, checking on after leaving, late answers not ignored, bad ids asked
  for, the old replay shown while loading, patterns unsorted, the verdict inverted, the
  osu! row showing our counts, misses not standing out, and no upload link. Two first got
  through, both gaps in the tests (the next replay always answered at once; the sample's
  two rows of counts were equal); a test for each was added
- The skills panel (20 breaks): the wrong URL, stamina and accuracy swapped, a wrong
  label, the radar's points out of order, always "plays", skipped plays not mentioned, a
  `409` not asked again, every error asked again, never giving up, the asking not stopped
  on leaving, late answers shown, the old player's profile shown, the `404` not explained,
  whole-number scores, the strongest shown as the weakest, a scale fitted to the data, the
  radar starting at the side, and the panel missing, below the plays or given the wrong
  id. Four first got through, all gaps in the tests (the new player never answered first,
  the old profile was never on screen when the id changed, the highest sample score
  rounded up to 100 anyway, and the tables weren't in the order check); a test for each
  was added
- Practice maps and the shared waiting (22 breaks): a `409` not asked again, every error
  asked again, never giving up, the asking not stopped on leaving, late answers or
  another id's result shown, the internal id leaking to callers, none made yet shown as
  made, no message when nothing fits, maps linked to their mapset, a link that can reach
  back, BPM not rounded, no mapper, no reason, the `404` not explained, no comfort
  level, an unordered list, seconds not padded, negative lengths, the wrong URL, and the
  panel missing or above the skills
- The Coach page and its stream (20 breaks): a partial event dropped, Windows line
  endings not read, `data:` without a space misread, no `Accept` header, a GET, no
  signal, each chunk decoded on its own, the last event dropped, the clock never
  ticking, leaving not cancelling, an early end left running, error events ignored, no
  fallback reason, no model in the byline, a `409` not explained, the button on while
  running, earlier steps not listed, a link to nowhere, no coach on the page, and the
  practice maps without the id its link goes to
- `charts.py` and its endpoint (13 breaks): song time instead of real time or the rate the
  wrong way, speed mods ignored, HR not flipped or flipped sideways, aim not rounded, the
  join ignoring the map, notes out of map order, the playfield's right edge counted in,
  rows and columns swapped, off-playfield frames not counted, charts of a pending replay,
  and no `404`
- The replay charts (18 breaks): bin edges not whole, far errors lost, misses counted as
  hits, hits out of time order, a sample spread, UR over everything so far, unpadded
  seconds, linear shading, 300s marked, cells transposed, no check for a 2D context, the
  UR axis starting at its first point, no miss lines, the 300 window not shaded, late
  charts not ignored, the last replay's charts kept; and windows in song time or ignoring
  mods. Two gaps showed while writing the tests: the UR axis started at the first point,
  hiding any miss in a map's first 50 notes (fixed: it spans the whole map), and a tick
  test passed on an empty list (fixed: it needs ticks). Late charts first got through,
  since no test had the old replay answer last; one does now
- Map attributes (15 breaks): NC not merged with DT, PF kept, HD dropped, keys in reverse
  order, other modes or every player's plays included, stored pairs fetched again, gone
  maps not skipped, one commit at the end, storing again keeping the old values, the mods
  not sent, no `ruleset`, a GET instead of a POST, the JSON body not sent, and the
  migration without CASCADE
- Skill scoring and the unranked-map handling (20 breaks): no miss penalty, speed ignoring
  accuracy, accuracy ignoring OD, consistency over 1 or ignoring misses, stamina from any
  play, no HD bonus, values not sorted, a plain average, no floor below a scale, scores
  not rounded, the weakest skill being the strongest, failed or taiko plays loaded, raw
  mods as the key, Relax counted, the AR without mods, every bad answer called unranked,
  unranked answers not recognized, and unranked maps stopping the fetch
- Skill profiles and their endpoint (11 breaks): the strongest stored as the weakest, the
  used count stored as skipped, storing again changing nothing, the time not moved on a
  replace, an unknown player not caught, the profile not committed, the skipped count
  dropped, not-ready as `404`, an unknown player as `409`, the player looked up before the
  profile, and the migration without CASCADE
- Map pattern vectors (21 breaks): no guard for an empty map, 16 notes not a long
  stream, the last run not counted, spacing in pixels, band edges going down, BPM not
  capped, stars below 0, values not rounded, streams counted as jumps, the radius as the
  diameter, storing again changing nothing, the time not moved on a replace, the version
  not stored, outdated vectors kept, maps without objects built, objects read out of
  order, building not committed, `fetch_beatmap` storing no vector or swapping BPM and
  stars, and the migration without CASCADE or without the extension. The BPM-and-stars
  swap first got through (only the worker test, run against the live stack, saw the
  values); a unit test now checks the vector `fetch_beatmap` stores
- Map search, candidates and the sync queue (21 breaks): any status or no most-played
  order in the search, stars above rather than from the low end, the cursor never sent,
  the band's high end included or low end left out, any mode or status picked, the first
  fit rather than the nearest the middle, every fit of a set, too many when none are
  wanted, the same page every time, a set picked twice, one page only, the top band
  dropped, no max combo stored, one commit at the end, fetched maps fetched again, every
  map without objects fetched, and syncs on the default queue or everything on `sync`
- Recommendations (31 breaks): failed plays or raw mods in the comfort level, a plain
  average for it, played or unranked maps recommended, the range's top end included,
  the farthest from the style first, accuracy a step up, a range that never widens or
  widens one side, stamina as long streams alone, aim without wide gaps, several per
  mapset, maps without the need kept, the lowest need first, the pool not cut, no final
  order by style, no clamp or the first clamp above the pool, the strongest skill
  trained, old ones kept or every player's cleared, ranks from 0, nothing committed, the
  "right at" band too wide, above and below swapped, `409` for an unknown player, an
  empty list before scoring, maps out of rank order, and the migration without either
  CASCADE. Three first got through, all gaps in the tests (a single-mod play's key is
  its mods anyway, the range widened past the old clamp's gap, and the picks' order
  happened to be the ids'); a test for each was added
- The coaching report (22 breaks): names matched by case, unanalyzed replays counted, 100s
  counted as misses, the highest UR instead of the mean, maps out of rank order, stars
  not rounded, the lowest plays taken as the top, the best accuracy as the last, combo
  held over 100%, no guard for a map without a combo, stamina from any play, Flashlight
  counted as Hidden, aim from stream misses, total misses from rates alone, skills
  worst first, weaknesses not weakest first, the maps step with no maps, later steps
  pointing at the maps, replays left out of the summary, called an LLM report, and the
  `404` and `409` swapped
- The LLM report (26 breaks): in the client, thinking left on, streamed answers, tools or
  the schema never sent, odd answers not caught, no arguments left as None, a trailing
  slash kept, an empty setting not empty, the model split at the wrong colon, tool
  results sent as user messages; in the check, counts up to 100 or decimals exempt, a
  loose tolerance, the evidence or the tools' numbers not facts, numbers never checked;
  in the loop, missing weaknesses allowed, the model's order kept, one lookup round
  only, tool answers or tool names not kept, an unreachable model or a broken answer
  crashing, any skill name allowed, an unknown tool crashing, and the model never used.
  One first got through (the tools' numbers were tested only in `merge`, not in the
  whole loop); a test for it was added
- The report stream (12 breaks): always or never a stream, no blank line after an event,
  steps not passed on, no error event, the report never sent, cached or buffered by a
  proxy, steps left in the queue when the report is done, and no starting, lookup or
  writing step
- The evals (22 breaks): replays or vectors not saved, mods loaded as a list, played maps
  never marked, maps not picked again, no hardest map, cases unsorted, unsafe file
  names; the weakest skills strongest first, the wrong strengths, scores never compared,
  a model's numbers never checked, a tool's numbers not facts, the strongest skill taken
  as the weakest, mapsets never compared, played maps allowed, the range's top end
  included, no widening allowed, no clamp above the pool, needs never checked, the
  first-range share over 10, and `check` always passing. Three first got through, all
  gaps in the tests, and fixing one found a real bug (the sort order differing between
  Windows and Linux)

`.gitattributes` marks `.osu` files as binary: git's `\r\n` to `\n` conversion would change
a map file's MD5 and break the fixtures on other machines.
- `sync.py`: not removing duplicates, not setting `updated_at`, committing part-way,
  writing before all data is fetched, scores before the player, updating only some
  columns, ignoring the limit, a wrong play time, or dropping recent scores
- Plays without an id and the migration (13 breaks): failed plays dropped or kept as id
  0, passed id-0 plays kept as 0 or keyed too, the failed-play key ignoring the time, map
  or player, positive ids, ids too big for a BIGINT, every play's map stored, the skipped
  count not reported, and the migration removing nothing or every failed play (that one
  first got through: the test's only failed play was the id-0 row; one with its own id
  was added)
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
  api/         routers: /health, /players/{username}, /players/{osu_id}/skills and
               /recommendations, POST /players/{osu_id}/report, /replays/...
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
  judge.py     judge a replay hit by hit (lazer's rules): result, timing, aim, note lock
  analysis.py  analyze a stored replay: judge it, store UR, timing bias, aim, hit by hit
  charts.py    the data behind a replay's charts: each note's timing, aim, place; cursor grid
  attributes.py  osu!'s aim/speed difficulty for each played map and mods (fetch_attributes)
  skills.py    a player's six skills, 0-100, from their top plays (hand-set scales, for now),
               stored as their skill profile
  vectors.py   each map's pattern vector (jumps, streams, spacing, BPM, stars), in pgvector
  replays.py   store an upload (original, Parquet frames, row); fetch the replay's map
  sync.py      save a player and their scores from osu! (upserts, one transaction)
  config.py    settings from env / .env
  db.py        async SQLAlchemy engine + session dependency
  models.py    the database tables
  worker.py    Celery app and its tasks: osu_sync (on its own queue), fetch_beatmap,
               fetch_replay_map, analyze_replay, fetch_attributes, build_map_vectors,
               collect_candidates
  candidates.py  well-known ranked maps in each star band, for the recommender
  recommender.py  practice maps for a player's weakest skill, with reasons
  coach.py     a coaching report from a player's facts (the template report)
  coach_llm.py the report's words by an LLM: tools, the number check, the fallback
  llm.py       a small interface to chat models; Ollama (local, free) behind it
  evals/       eval cases (cases.py), the automatic checks (checks.py), the command
migrations/    Alembic
evals/cases/   the eval cases, one player each, as JSON
frontend/      React + TypeScript (Vite): src/api.ts calls the API, src/types.ts its responses,
               src/pages/ the pages (home, player, upload, replay), src/components/ the
               parts they share
tests/
  unit/        no services needed
  fixtures/osu/  saved real osu! API responses
  integration/ real Redis + osu! API
```
