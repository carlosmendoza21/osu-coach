"""Integration tests for osu_coach.osu.files: real downloads, and the Docker data volume.

Run with:  uv run pytest -m integration
Needs:     Redis (docker compose up -d redis) for the downloads, osu! credentials for the
           checksum check, and the running stack (docker compose up -d) for the volume
           tests. Each part skips with the reason if what it needs is missing.
Downloads go to pytest's temporary folder, not data/.
"""

import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from redis.asyncio import Redis

from osu_coach.config import get_settings
from osu_coach.osu.auth import TokenProvider
from osu_coach.osu.client import OsuClient, OsuNotFoundError
from osu_coach.osu.files import BeatmapFiles, md5_hex
from osu_coach.osu.ratelimiter import RateLimiter
from tests.support import FIXTURES, ROOT, load

pytestmark = pytest.mark.integration


@dataclass
class Live:
    files: BeatmapFiles
    http: httpx.AsyncClient
    redis: Redis
    downloads: list[str] = field(default_factory=list)


@pytest.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    url = get_settings().redis_url
    redis: Redis = Redis.from_url(url)
    try:
        await redis.ping()
    except Exception:
        await redis.aclose()
        pytest.skip(f"Redis not reachable at {url}. Start it: docker compose up -d redis")
    downloads: list[str] = []

    async def record(request: httpx.Request) -> None:
        downloads.append(request.url.path)

    async with httpx.AsyncClient(timeout=20, event_hooks={"request": [record]}) as http:
        files = BeatmapFiles(http, RateLimiter(redis), tmp_path / "beatmaps")
        yield Live(files, http, redis, downloads)
    await redis.aclose()


# ---------- Real downloads ----------


@pytest.mark.parametrize(("beatmap_id", "fixture"), [(22423, "22423.osu"), (75, "75.osu")])
async def test_download_matches_the_saved_file(live: Live, beatmap_id: int, fixture: str) -> None:
    saved = (FIXTURES / fixture).read_bytes()

    path = await live.files.get(beatmap_id, md5_hex(saved))

    # Ranked maps don't change: today's file is byte for byte the saved one, line endings
    # included (22423 uses \r\n, 75 uses \n).
    assert path.read_bytes() == saved


async def test_checksum_from_the_api_matches_the_download(live: Live) -> None:
    checksum = load("beatmap.json")["checksum"]

    path = await live.files.get(22423, checksum)

    assert md5_hex(path.read_bytes()) == checksum


async def test_second_get_uses_the_cached_file(live: Live) -> None:
    await live.files.get(22423, None)
    live.downloads.clear()

    await live.files.get(22423, None)

    assert live.downloads == []


async def test_unknown_map_is_not_found(live: Live) -> None:
    with pytest.raises(OsuNotFoundError):
        await live.files.get(999999999, None)

    assert not live.files.path_for(999999999).exists()


async def test_a_real_scores_map_downloads_and_verifies(live: Live) -> None:
    # The full flow: a score names a map, the API gives its checksum, and the file
    # downloaded from osu! must match it.
    s = get_settings()
    if not s.osu_client_id or not s.osu_client_secret.get_secret_value():
        pytest.skip("OSU_CLIENT_ID / OSU_CLIENT_SECRET not set in .env")
    tokens = TokenProvider(live.http, live.redis, s.osu_client_id, s.osu_client_secret)
    osu = OsuClient(live.http, tokens, RateLimiter(live.redis))
    score = (await osu.get_best_scores(2, limit=1))[0]
    assert score.beatmap.checksum is not None

    path = await live.files.get(score.beatmap.id, score.beatmap.checksum)

    assert md5_hex(path.read_bytes()) == score.beatmap.checksum


# ---------- The Docker data volume ----------


def docker() -> str:
    found = shutil.which("docker")
    desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/resources/bin"
    if found is None and (desktop / "docker.exe").exists():
        found = str(desktop / "docker.exe")
    if found is None:
        pytest.skip("docker isn't installed")
    return found


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker(), "compose", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def stack() -> None:
    running = compose("ps", "--status", "running", "--services").stdout.split()
    if not {"api", "worker"} <= set(running):
        pytest.skip("api and worker aren't running. Start them: docker compose up -d --build")


DOWNLOAD_IN_WORKER = """
import asyncio, httpx
from redis.asyncio import Redis
from osu_coach.config import get_settings
from osu_coach.osu.files import BeatmapFiles
from osu_coach.osu.ratelimiter import RateLimiter

async def main():
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    async with httpx.AsyncClient(timeout=20) as http:
        files = BeatmapFiles(http, RateLimiter(redis), s.data_dir / "beatmaps")
        print((await files.get(75, None)).resolve())
    await redis.aclose()

asyncio.run(main())
"""


def test_data_folder_belongs_to_the_app_user(stack: None) -> None:
    owner = compose("exec", "-T", "worker", "stat", "-c", "%U", "/app/data")

    # A new named volume copies this folder's owner from the image.
    assert owner.stdout.strip() == "app", owner.stderr


def test_worker_downloads_and_the_api_reads_the_same_file(stack: None) -> None:
    downloaded = compose("exec", "-T", "worker", "python", "-c", DOWNLOAD_IN_WORKER)
    assert downloaded.returncode == 0, downloaded.stderr
    assert downloaded.stdout.strip() == "/app/data/beatmaps/75.osu"

    read = compose("exec", "-T", "api", "md5sum", "/app/data/beatmaps/75.osu")

    assert read.returncode == 0, read.stderr
    assert read.stdout.split()[0] == md5_hex((FIXTURES / "75.osu").read_bytes())
