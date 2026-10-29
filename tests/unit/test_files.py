"""Unit tests for osu_coach.osu.files: downloading and caching .osu files.

Nothing real is contacted: respx fakes osu.ppy.sh (and blocks anything unmocked),
fakeredis backs the rate limiter, retry waits are only recorded, and files go to pytest's
temporary folder. The served files are two real ones saved in tests/fixtures/osu/: map
22423 (\\r\\n line endings) and map 75 (\\n). tests/integration/test_files_live.py downloads
for real.
"""

import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis

from osu_coach.osu.client import OsuApiError, OsuNotFoundError
from osu_coach.osu.files import OSU_FILE_URL, BeatmapFileError, BeatmapFiles, md5_hex
from osu_coach.osu.ratelimiter import DEFAULT_KEY as RATE_KEY
from osu_coach.osu.ratelimiter import RateLimiter
from tests.support import FIXTURES

MAP = 22423
MAP_BYTES = (FIXTURES / "22423.osu").read_bytes()  # \r\n line endings
MAP_MD5 = "e13c28db91ef96be62bd503f0def5560"  # what osu! reports as its checksum
LF_MAP = 75
LF_BYTES = (FIXTURES / "75.osu").read_bytes()  # \n line endings
SSL_CONTEXT = ssl.create_default_context()  # loading certificates is slow on Windows


def url(beatmap_id: int) -> str:
    return OSU_FILE_URL.format(beatmap_id=beatmap_id)


class Env:
    def __init__(self, router: respx.MockRouter, redis: FakeAsyncRedis, folder: Path) -> None:
        self.router = router
        self.redis = redis
        self.folder = folder
        self.waits: list[float] = []

    async def retry_sleep(self, seconds: float) -> None:
        self.waits.append(seconds)

    def files(self, http: httpx.AsyncClient) -> BeatmapFiles:
        limiter = RateLimiter(self.redis)
        return BeatmapFiles(http, limiter, self.folder, sleep=self.retry_sleep, rand=lambda: 1.0)

    def serve(self, beatmap_id: int, *responses: httpx.Response | Exception) -> respx.Route:
        route = self.router.get(url(beatmap_id))
        if len(responses) == 1 and isinstance(responses[0], httpx.Response):
            return route.mock(return_value=responses[0])
        return route.mock(side_effect=list(responses))

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.folder.glob("*.tmp"))


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    redis = FakeAsyncRedis()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield Env(router, redis, tmp_path / "beatmaps")
    await redis.aclose()


@pytest.fixture
async def files(env: Env) -> AsyncIterator[BeatmapFiles]:
    async with httpx.AsyncClient(verify=SSL_CONTEXT) as http:
        yield env.files(http)


def ok(data: bytes = MAP_BYTES) -> httpx.Response:
    return httpx.Response(200, content=data, headers={"content-type": "text/plain"})


# ---------- md5_hex and paths ----------


@pytest.mark.parametrize(
    ("data", "want"),
    [
        (b"hello", "5d41402abc4b2a76b9719d911017c592"),
        (b"", "d41d8cd98f00b204e9800998ecf8427e"),
        (MAP_BYTES, MAP_MD5),  # the real file gives osu!'s own checksum
    ],
)
def test_md5_hex(data: bytes, want: str) -> None:
    assert md5_hex(data) == want


def test_folder_is_created_with_its_parents(env: Env, files: BeatmapFiles) -> None:
    assert env.folder.is_dir()


def test_path_for(env: Env, files: BeatmapFiles) -> None:
    assert files.path_for(MAP) == env.folder / "22423.osu"


# ---------- Downloading ----------


async def test_first_get_downloads_and_saves(env: Env, files: BeatmapFiles) -> None:
    route = env.serve(MAP, ok())

    path = await files.get(MAP, MAP_MD5)

    assert path == files.path_for(MAP)
    assert path.read_bytes() == MAP_BYTES
    assert route.call_count == 1
    request = route.calls.last.request
    assert str(request.url) == "https://osu.ppy.sh/osu/22423"
    assert "Authorization" not in request.headers, ".osu files need no token"


@pytest.mark.parametrize(
    ("beatmap_id", "data"),
    [
        (MAP, MAP_BYTES),  # \r\n
        (LF_MAP, LF_BYTES),  # \n
    ],
)
async def test_bytes_are_saved_untouched(
    env: Env, files: BeatmapFiles, beatmap_id: int, data: bytes
) -> None:
    # Line endings differ between real maps; any change would break the MD5.
    env.serve(beatmap_id, ok(data))

    path = await files.get(beatmap_id, md5_hex(data))

    assert path.read_bytes() == data


async def test_checksum_is_compared_in_lowercase(env: Env, files: BeatmapFiles) -> None:
    env.serve(MAP, ok())

    path = await files.get(MAP, MAP_MD5.upper())

    assert path.read_bytes() == MAP_BYTES


async def test_without_a_checksum_the_download_is_taken_as_it_is(
    env: Env, files: BeatmapFiles
) -> None:
    env.serve(MAP, ok())

    path = await files.get(MAP, None)

    assert path.read_bytes() == MAP_BYTES


# ---------- The cache ----------


@pytest.mark.parametrize(
    "checksum",
    [
        MAP_MD5,
        MAP_MD5.upper(),
        None,  # unknown checksum: any cached copy is used
    ],
)
async def test_a_good_cached_copy_means_no_request(
    env: Env, files: BeatmapFiles, checksum: str | None
) -> None:
    files.path_for(MAP).write_bytes(MAP_BYTES)
    route = env.serve(MAP, ok())

    path = await files.get(MAP, checksum)

    assert path.read_bytes() == MAP_BYTES
    assert route.call_count == 0
    assert await env.redis.zcard(RATE_KEY) == 0, "no rate-limit slot either"


async def test_a_corrupted_cached_copy_is_downloaded_again(env: Env, files: BeatmapFiles) -> None:
    files.path_for(MAP).write_bytes(MAP_BYTES[:100])  # e.g. half a file from a crash
    route = env.serve(MAP, ok())

    path = await files.get(MAP, MAP_MD5)

    assert route.call_count == 1
    assert path.read_bytes() == MAP_BYTES


async def test_an_updated_map_replaces_the_cached_copy(env: Env, files: BeatmapFiles) -> None:
    # The cached copy is the old version; the stored checksum is the new version's.
    files.path_for(LF_MAP).write_bytes(LF_BYTES + b"old version")
    env.serve(LF_MAP, ok(LF_BYTES))

    path = await files.get(LF_MAP, md5_hex(LF_BYTES))

    assert path.read_bytes() == LF_BYTES


# ---------- Failures ----------


async def test_empty_body_is_not_found(env: Env, files: BeatmapFiles) -> None:
    # osu! answers an unknown map with 200 and an empty body, not 404.
    env.serve(999999999, httpx.Response(200, content=b""))

    with pytest.raises(OsuNotFoundError):
        await files.get(999999999, None)

    assert not files.path_for(999999999).exists()


async def test_checksum_mismatch_writes_nothing(env: Env, files: BeatmapFiles) -> None:
    env.serve(LF_MAP, ok(LF_BYTES))

    with pytest.raises(BeatmapFileError) as exc:
        await files.get(LF_MAP, "0" * 32)

    assert isinstance(exc.value, OsuApiError), "callers catching osu! errors catch this too"
    assert md5_hex(LF_BYTES) in str(exc.value) and "0" * 32 in str(exc.value)
    assert not files.path_for(LF_MAP).exists()
    assert env.leftovers() == []


async def test_mismatch_keeps_the_existing_cached_copy(env: Env, files: BeatmapFiles) -> None:
    # A bad download must never replace a file we already have.
    files.path_for(MAP).write_bytes(MAP_BYTES)
    env.serve(MAP, ok(b"something else entirely"))

    with pytest.raises(BeatmapFileError):
        await files.get(MAP, "f" * 32)  # neither the cached copy nor the download match

    assert files.path_for(MAP).read_bytes() == MAP_BYTES


@pytest.mark.parametrize(
    "status",
    [
        404,  # not retried
        403,
        500,  # retried, then given up
        503,
    ],
)
async def test_http_errors_raise_api_error(env: Env, files: BeatmapFiles, status: int) -> None:
    env.serve(MAP, httpx.Response(status))

    with pytest.raises(OsuApiError) as exc:
        await files.get(MAP, MAP_MD5)

    assert not isinstance(exc.value, (OsuNotFoundError, BeatmapFileError))
    assert str(status) in str(exc.value)
    assert not files.path_for(MAP).exists()


async def test_temporary_errors_are_retried(env: Env, files: BeatmapFiles) -> None:
    route = env.serve(MAP, httpx.ConnectError("refused"), httpx.Response(503), ok())

    path = await files.get(MAP, MAP_MD5)

    assert path.read_bytes() == MAP_BYTES
    assert route.call_count == 3
    assert env.waits == [1.0, 2.0]


async def test_every_attempt_takes_a_rate_limit_slot(env: Env, files: BeatmapFiles) -> None:
    # Same server as the API: retries count toward the shared limit too.
    env.serve(MAP, httpx.Response(503), ok())

    await files.get(MAP, MAP_MD5)

    assert await env.redis.zcard(RATE_KEY) == 2


async def test_network_error_after_all_retries_is_raised(env: Env, files: BeatmapFiles) -> None:
    env.serve(MAP, *[httpx.ConnectError("refused")] * 4)

    with pytest.raises(httpx.ConnectError):
        await files.get(MAP, MAP_MD5)


# ---------- Safe writes ----------


async def test_no_temporary_files_are_left(env: Env, files: BeatmapFiles) -> None:
    env.serve(MAP, ok())
    env.serve(LF_MAP, ok(LF_BYTES))

    await files.get(MAP, MAP_MD5)
    await files.get(LF_MAP, None)

    assert env.leftovers() == []
    assert sorted(p.name for p in env.folder.iterdir()) == ["22423.osu", "75.osu"]


async def test_a_failed_write_never_leaves_a_half_file(
    env: Env, files: BeatmapFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The disk fills up halfway through writing. The real file must stay as it was:
    # writing to a temporary file and renaming it is what guarantees that.
    files.path_for(LF_MAP).write_bytes(LF_BYTES + b"old version")
    env.serve(LF_MAP, ok(LF_BYTES))
    real_write = Path.write_bytes

    def half_then_fail(self: Path, data: bytes) -> int:
        real_write(self, data[: len(data) // 2])
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_bytes", half_then_fail)

    with pytest.raises(OSError, match="No space left"):
        await files.get(LF_MAP, md5_hex(LF_BYTES))

    monkeypatch.undo()
    assert files.path_for(LF_MAP).read_bytes() == LF_BYTES + b"old version"


async def test_a_leftover_temporary_file_is_overwritten(env: Env, files: BeatmapFiles) -> None:
    # A crash after writing the .tmp but before the rename left it behind.
    (env.folder / "22423.osu.tmp").write_bytes(b"half")
    env.serve(MAP, ok())

    path = await files.get(MAP, MAP_MD5)

    assert path.read_bytes() == MAP_BYTES
    assert env.leftovers() == []


def test_fixtures_survive_git() -> None:
    # .gitattributes keeps .osu files byte for byte; git's \r\n -> \n conversion would
    # change this file's MD5 and break every test that uses it.
    assert b"\r\n" in MAP_BYTES
    assert md5_hex(MAP_BYTES) == MAP_MD5
