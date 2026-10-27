"""Typed views of the osu! API v2 responses we use.

Scores use the legacy format (we don't send an `x-api-version` header): hit counts are
`count_300` / `count_100` / `count_50` / `count_miss` and mods are acronyms like "HD",
matching osu! stable replays (.osr). If osu! drops that format, this file is the place to
change.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OsuModel(BaseModel):
    """Base model for osu! data."""

    # Ignore the many fields osu! sends that we don't use, and keep parsed data read-only.
    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------- GET /users/{user}/osu ----------


class UserStats(OsuModel):
    """User statistics (the `statistics` object of a user)."""

    pp: float
    global_rank: int | None  # null for inactive players
    country_rank: int | None
    play_count: int
    hit_accuracy: float  # a percent (96.85), unlike a score's accuracy (0.9685)


class OsuUser(OsuModel):
    """osu! user."""

    id: int
    username: str
    country_code: str
    statistics: UserStats


# ---------- GET /users/{id}/scores/best and /scores/recent ----------


class ScoreStatistics(OsuModel):
    """Hit counts of a score."""

    count_300: int
    count_100: int
    count_50: int
    count_miss: int


class ScoreBeatmap(OsuModel):
    """Beatmap information for a score."""

    id: int
    beatmapset_id: int
    version: str
    difficulty_rating: float
    bpm: float
    total_length: int  # seconds, including breaks
    hit_length: int  # seconds of actual play
    count_circles: int
    count_sliders: int
    count_spinners: int
    ar: float
    cs: float
    # osu! calls OD "accuracy" and HP "drain" on beatmaps; use the real names here.
    od: float = Field(alias="accuracy")
    hp: float = Field(alias="drain")
    checksum: str | None  # MD5 of the .osu file; replays identify maps by it
    status: str  # "ranked", "loved", "graveyard", ...
    mode: str  # "osu" for standard, the only mode this app analyzes


class ScoreBeatmapset(OsuModel):
    """Beatmapset information for a score."""

    id: int
    artist: str
    title: str
    creator: str


class OsuScore(OsuModel):
    """osu! score."""

    id: int
    user_id: int
    score: int
    accuracy: float  # 0-1
    max_combo: int
    mods: list[str]  # e.g. ["HD", "DT"]; empty for no mods
    pp: float | None  # null for unranked/loved maps and some recent plays
    rank: str  # "X" = SS, "XH" = silver SS, "S", "SH", "A", ..., "F" = failed
    passed: bool  # recent scores can include failed plays
    created_at: datetime
    statistics: ScoreStatistics
    beatmap: ScoreBeatmap
    beatmapset: ScoreBeatmapset


# ---------- GET /beatmaps/{id} and /beatmaps/lookup ----------


class OsuBeatmap(ScoreBeatmap):
    """A full beatmap: everything a score's beatmap has, plus the two fields below."""

    max_combo: int | None  # osu! can send null for maps it hasn't processed
    beatmapset: ScoreBeatmapset  # artist, title, creator
