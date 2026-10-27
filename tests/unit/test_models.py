"""Unit tests for osu_coach.models: the table definitions, checked without a database.

The most important check: a column is nullable exactly when the osu! API model field it
comes from can be None, so syncing real data never fails on NOT NULL. The same tables in
a real Postgres are covered in tests/integration/test_models_live.py.
"""

from typing import get_args

import pytest
from pydantic import BaseModel
from sqlalchemy import BigInteger, Column, Table

from osu_coach.models import Base
from osu_coach.osu.schemas import (
    OsuBeatmap,
    OsuScore,
    OsuUser,
    ScoreBeatmapset,
    ScoreStatistics,
    UserStats,
)

PLAYERS: Table = Base.metadata.tables["players"]
SCORES: Table = Base.metadata.tables["scores"]
BEATMAPS: Table = Base.metadata.tables["beatmaps"]


def col(table: Table, name: str) -> Column[object]:
    column: Column[object] = table.c[name]
    return column


def field_allows_none(model: type[BaseModel], field: str) -> bool:
    """True for fields typed `X | None`."""
    return type(None) in get_args(model.model_fields[field].annotation)


# ---------- Tables and keys ----------


def test_tables() -> None:
    assert set(Base.metadata.tables) == {"players", "scores", "beatmaps"}


@pytest.mark.parametrize(("table", "key"), [(PLAYERS, "osu_id"), (SCORES, "id"), (BEATMAPS, "id")])
def test_primary_keys_are_osu_ids(table: Table, key: str) -> None:
    assert [c.name for c in table.primary_key] == [key]
    pk = col(table, key)
    # osu! assigns these ids: the database must not generate its own.
    assert pk.autoincrement is False
    # Newer osu! score ids are past the 32-bit limit (~2.1 billion).
    assert isinstance(pk.type, BigInteger)


def test_scores_reference_players_with_cascade() -> None:
    (fk,) = col(SCORES, "player_id").foreign_keys

    assert fk.target_fullname == "players.osu_id"
    # Deleting a player removes their scores instead of failing or leaving orphans.
    assert fk.ondelete == "CASCADE"


def test_scores_reference_beatmaps_without_cascade() -> None:
    (fk,) = col(SCORES, "beatmap_id").foreign_keys

    assert fk.target_fullname == "beatmaps.id"
    # Deleting a map that has scores is almost certainly a mistake: Postgres refuses it.
    assert fk.ondelete is None


def test_scores_have_exactly_two_foreign_keys() -> None:
    assert {fk.parent.name for fk in SCORES.foreign_keys} == {"player_id", "beatmap_id"}


def test_beatmaps_reference_nothing() -> None:
    assert not BEATMAPS.foreign_keys


def test_beatmap_checksum_is_unique() -> None:
    # A replay finds its map by checksum: one row per .osu file, and a fast lookup.
    assert col(BEATMAPS, "checksum").unique is True


# ---------- Constraint names ----------


@pytest.mark.parametrize(
    ("table", "want"),
    [
        (PLAYERS, {"players_pkey"}),
        (SCORES, {"scores_pkey", "scores_player_id_fkey", "scores_beatmap_id_fkey"}),
        (BEATMAPS, {"beatmaps_pkey", "beatmaps_checksum_key"}),
    ],
)
def test_constraint_names_follow_the_convention(table: Table, want: set[str]) -> None:
    # Named like Postgres names them by itself, so migrations can refer to them (an
    # unnamed foreign key can't be dropped on downgrade) and constraints made before
    # the convention keep their names.
    assert {c.name for c in table.constraints} == want


# ---------- Nullability ----------


@pytest.mark.parametrize(
    ("table", "nullable"),
    [
        (PLAYERS, {"global_rank"}),  # inactive players have no rank
        (SCORES, {"pp"}),  # failed plays and unranked maps have no pp
        (BEATMAPS, {"max_combo", "checksum"}),  # a score's map data has no max combo
    ],
)
def test_only_these_columns_are_nullable(table: Table, nullable: set[str]) -> None:
    assert {c.name for c in table.columns if c.nullable} == nullable


@pytest.mark.parametrize(
    ("table", "column", "model", "field"),
    [
        (PLAYERS, "osu_id", OsuUser, "id"),
        (PLAYERS, "username", OsuUser, "username"),
        (PLAYERS, "country", OsuUser, "country_code"),
        (PLAYERS, "global_rank", UserStats, "global_rank"),
        (PLAYERS, "pp", UserStats, "pp"),
        (PLAYERS, "playcount", UserStats, "play_count"),
        (PLAYERS, "accuracy", UserStats, "hit_accuracy"),
        (SCORES, "id", OsuScore, "id"),
        (SCORES, "player_id", OsuScore, "user_id"),
        (SCORES, "score", OsuScore, "score"),
        (SCORES, "max_combo", OsuScore, "max_combo"),
        (SCORES, "count_300", ScoreStatistics, "count_300"),
        (SCORES, "count_100", ScoreStatistics, "count_100"),
        (SCORES, "count_50", ScoreStatistics, "count_50"),
        (SCORES, "count_miss", ScoreStatistics, "count_miss"),
        (SCORES, "rank", OsuScore, "rank"),
        (SCORES, "pp", OsuScore, "pp"),
        (SCORES, "mods", OsuScore, "mods"),
        (SCORES, "date", OsuScore, "created_at"),
        (SCORES, "accuracy", OsuScore, "accuracy"),
        (SCORES, "passed", OsuScore, "passed"),
        (BEATMAPS, "id", OsuBeatmap, "id"),
        (BEATMAPS, "beatmapset_id", OsuBeatmap, "beatmapset_id"),
        (BEATMAPS, "artist", ScoreBeatmapset, "artist"),
        (BEATMAPS, "title", ScoreBeatmapset, "title"),
        (BEATMAPS, "creator", ScoreBeatmapset, "creator"),
        (BEATMAPS, "version", OsuBeatmap, "version"),
        (BEATMAPS, "star_rating", OsuBeatmap, "difficulty_rating"),
        (BEATMAPS, "ar", OsuBeatmap, "ar"),
        (BEATMAPS, "od", OsuBeatmap, "od"),
        (BEATMAPS, "cs", OsuBeatmap, "cs"),
        (BEATMAPS, "hp", OsuBeatmap, "hp"),
        (BEATMAPS, "bpm", OsuBeatmap, "bpm"),
        (BEATMAPS, "length_s", OsuBeatmap, "total_length"),
        (BEATMAPS, "hit_length_s", OsuBeatmap, "hit_length"),
        (BEATMAPS, "count_circles", OsuBeatmap, "count_circles"),
        (BEATMAPS, "count_sliders", OsuBeatmap, "count_sliders"),
        (BEATMAPS, "count_spinners", OsuBeatmap, "count_spinners"),
        (BEATMAPS, "max_combo", OsuBeatmap, "max_combo"),
        (BEATMAPS, "checksum", OsuBeatmap, "checksum"),
        (BEATMAPS, "status", OsuBeatmap, "status"),
        (BEATMAPS, "mode", OsuBeatmap, "mode"),
    ],
)
def test_nullability_matches_the_api_model(
    table: Table, column: str, model: type[BaseModel], field: str
) -> None:
    allows_none = field_allows_none(model, field)

    assert col(table, column).nullable is allows_none, (
        f"{table.name}.{column} nullable={col(table, column).nullable}, but "
        f"{model.__name__}.{field} {'can' if allows_none else 'cannot'} be None"
    )


# ---------- Defaults ----------


def test_play_date_has_no_default() -> None:
    # It must come from osu!; a default would silently record the sync time instead.
    date = col(SCORES, "date")

    assert date.server_default is None
    assert date.default is None


@pytest.mark.parametrize("table", [PLAYERS, BEATMAPS])
def test_updated_at_is_set_by_the_database(table: Table) -> None:
    updated_at = col(table, "updated_at")

    assert updated_at.server_default is not None
    assert updated_at.onupdate is not None
    assert updated_at.type.timezone is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("table", "column"), [(PLAYERS, "updated_at"), (SCORES, "date"), (BEATMAPS, "updated_at")]
)
def test_times_keep_their_timezone(table: Table, column: str) -> None:
    assert col(table, column).type.timezone is True  # type: ignore[attr-defined]


# ---------- Indexes ----------


def test_indexes() -> None:
    def names(table: Table) -> dict[str, list[str]]:
        return {i.name or "": [c.name for c in i.columns] for i in table.indexes}

    # Only what queries use: each index slows down every write.
    assert names(PLAYERS) == {"ix_players_username": ["username"]}
    assert names(SCORES) == {
        "ix_scores_beatmap_id": ["beatmap_id"],
        # Player first, then pp: "this player's scores by pp" in one scan.
        "ix_scores_player_pp": ["player_id", "pp"],
    }
    # The checksum's unique constraint already gives it an index; nothing else is needed.
    assert names(BEATMAPS) == {}


def test_username_is_not_unique() -> None:
    # osu! names can be changed and later taken by someone else.
    (index,) = PLAYERS.indexes

    assert index.unique is False
