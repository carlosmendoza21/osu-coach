"""Download and cache .osu beatmap files, verified against their MD5 checksum."""

import asyncio
import hashlib
import random
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from osu_coach.osu.client import OsuApiError, OsuNotFoundError
from osu_coach.osu.ratelimiter import RateLimiter
from osu_coach.osu.retry import DEFAULT_POLICY, RetryPolicy, send_with_retries

OSU_FILE_URL = "https://osu.ppy.sh/osu/{beatmap_id}"


class BeatmapFileError(OsuApiError):
    """The downloaded file doesn't match the map's checksum."""


def md5_hex(data: bytes) -> str:
    """The MD5 of some bytes, as 32 lowercase hex characters (how osu! writes checksums)."""
    # MD5 is broken for security, but osu! only uses it to identify a file. Saying so
    # keeps Python builds that refuse MD5 for security purposes from blocking it.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class BeatmapFiles:
    """Gets .osu files from osu!, keeping a verified copy of each on disk."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        directory: Path,
        retry_policy: RetryPolicy = DEFAULT_POLICY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.http = http
        self.limiter = limiter
        self.directory = directory
        self.retry_policy = retry_policy
        self.sleep = sleep
        self.rand = rand
        directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, beatmap_id: int) -> Path:
        """Where a map's file lives on disk."""
        return self.directory / f"{beatmap_id}.osu"

    async def get(self, beatmap_id: int, checksum: str | None) -> Path:
        """The path to the map's .osu file, downloading it if there's no good copy.

        With a checksum, both a cached copy and a download must match it. Without one
        (not known yet), any cached copy is used and a download is taken as it is.
        """
        path = self.path_for(beatmap_id)
        expected = checksum.lower() if checksum is not None else None

        if path.exists() and (expected is None or md5_hex(path.read_bytes()) == expected):
            return path  # a good copy: no request

        data = await self._download(beatmap_id)
        if expected is not None and md5_hex(data) != expected:
            # The map was updated since its checksum was stored, or the download broke.
            # Nothing is written, so a bad file never looks like a good cached one.
            raise BeatmapFileError(
                f"beatmap {beatmap_id}: downloaded file has MD5 {md5_hex(data)}, "
                f"expected {expected}"
            )

        # Write to a temporary file, then rename: the rename is all at once, so a crash
        # or two workers downloading the same map never leave a half-written .osu file.
        tmp = path.with_suffix(".osu.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    async def _download(self, beatmap_id: int) -> bytes:
        """The file's raw bytes from osu! (rate limited and retried, like API requests)."""
        url = OSU_FILE_URL.format(beatmap_id=beatmap_id)

        async def send() -> httpx.Response:
            # Same server as the API, so the same shared limit. No token needed.
            await self.limiter.acquire()
            return await self.http.get(url)

        response = await send_with_retries(send, self.retry_policy, self.sleep, self.rand)
        if not response.is_success:
            raise OsuApiError(f"osu! returned {response.status_code} for {url}")
        if not response.content:
            # osu! answers an unknown map with 200 and an empty body, not 404.
            raise OsuNotFoundError(f"no .osu file for beatmap {beatmap_id}")
        # The raw bytes, untouched: line endings differ between maps (\r\n or \n), and
        # changing them would change the MD5.
        return response.content
