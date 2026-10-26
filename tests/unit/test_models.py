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
from osu_coach.osu.schemas import OsuScore, OsuUser, ScoreStatistics, UserStats

PLAYERS: Table = Base.metadata.tables["players"]
SCORES: Table = Base.metadata.tables["scores"]


def col(table: Table, name: str) -> Column[object]:
    column: Column[object] = table.c[name]
    return column


def field_allows_none(model: type[BaseModel], field: str) -> bool:
    """True for fields typed `X | None`."""
    return type(None) in get_args(model.model_fields[field].annotation)


# ---------- Tables and keys ----------


def test_tables() -> None:
    assert set(Base.metadata.tables) == {"players", "scores"}


@pytest.mark.parametrize(("table", "key"), [(PLAYERS, "osu_id"), (SCORES, "id")])
def test_primary_keys_are_osu_ids(table: Table, key: str) -> None:
    assert [c.name for c in table.primary_key] == [key]
    pk = col(table, key)
    # osu! assigns these ids: the database must not generate its own.
    assert pk.autoincrement is False
    # Newer osu! score ids are past the 32-bit limit (~2.1 billion).
    assert isinstance(pk.type, BigInteger)


def test_scores_reference_players_with_cascade() -> None:
    (fk,) = SCORES.foreign_keys

    assert fk.parent.name == "player_id"
    assert fk.target_fullname == "players.osu_id"
    # Deleting a player removes their scores instead of failing or leaving orphans.
    assert fk.ondelete == "CASCADE"


def test_beatmap_id_has_no_foreign_key_yet() -> None:
    # The beatmaps table arrives on Day 3; until then this is a plain column.
    assert not col(SCORES, "beatmap_id").foreign_keys


# ---------- Nullability ----------


@pytest.mark.parametrize(
    ("table", "nullable"),
    [
        (PLAYERS, {"global_rank"}),  # inactive players have no rank
        (SCORES, {"pp"}),  # failed plays and unranked maps have no pp
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


def test_updated_at_is_set_by_the_database() -> None:
    updated_at = col(PLAYERS, "updated_at")

    assert updated_at.server_default is not None
    assert updated_at.onupdate is not None
    assert updated_at.type.timezone is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(("table", "column"), [(PLAYERS, "updated_at"), (SCORES, "date")])
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


def test_username_is_not_unique() -> None:
    # osu! names can be changed and later taken by someone else.
    (index,) = PLAYERS.indexes

    assert index.unique is False
