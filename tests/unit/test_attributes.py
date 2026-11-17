"""Unit tests for osu_coach.attributes and the attributes model.

The model is checked against osu!'s real answers for map 22423 (saved in
tests/fixtures/osu/), the mod keys on hand-made lists, and storing with a fake session
that keeps each statement compiled to Postgres SQL. Which pairs to fetch and fetching
them, against Postgres, are in tests/integration/test_attributes_live.py.
"""

from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from osu_coach.attributes import (
    NO_DIFFICULTY,
    SAME_AS,
    difficulty_mods,
    mods_key,
    store_attributes,
)
from osu_coach.osu.schemas import AttributesResponse, DifficultyAttributes
from tests.support import load

PG = postgresql.dialect()  # type: ignore[no-untyped-call]  # SQLAlchemy leaves it untyped


# ---------- The model ----------


def test_osus_answer_parses() -> None:
    attrs = AttributesResponse.model_validate(load("attributes_22423.json")).attributes

    assert attrs.star_rating == pytest.approx(3.5647, rel=1e-3)  # the map's own star rating
    assert attrs.max_combo == 450
    assert attrs.aim_difficulty == pytest.approx(1.8561, rel=1e-3)
    assert attrs.speed_difficulty == pytest.approx(1.5995, rel=1e-3)
    assert attrs.speed_note_count == pytest.approx(33.65, rel=1e-3)
    assert attrs.slider_factor == pytest.approx(0.927, rel=1e-3)
    assert attrs.aim_difficult_strain_count == pytest.approx(68.88, rel=1e-3)
    assert attrs.speed_difficult_strain_count == pytest.approx(28.91, rel=1e-3)


def test_dt_makes_it_harder() -> None:
    nomod = AttributesResponse.model_validate(load("attributes_22423.json")).attributes
    dt = AttributesResponse.model_validate(load("attributes_22423_dt.json")).attributes

    assert dt.star_rating > nomod.star_rating
    assert dt.aim_difficulty > nomod.aim_difficulty
    assert dt.speed_difficulty > nomod.speed_difficulty
    assert dt.max_combo == nomod.max_combo  # the same notes


def test_fields_osu_adds_are_ignored() -> None:
    data = load("attributes_22423.json")
    data["attributes"]["some_new_field"] = 1  # osu! adds fields over time
    # aim_difficult_slider_count is already one we don't keep.
    assert "aim_difficult_slider_count" in data["attributes"]

    AttributesResponse.model_validate(data)


@pytest.mark.parametrize("missing", ["star_rating", "aim_difficulty", "speed_difficulty"])
def test_a_missing_field_is_an_error(missing: str) -> None:
    data = load("attributes_22423.json")
    del data["attributes"][missing]

    with pytest.raises(ValidationError, match=missing):
        AttributesResponse.model_validate(data)


def test_attributes_are_read_only() -> None:
    attrs = AttributesResponse.model_validate(load("attributes_22423.json")).attributes

    with pytest.raises(ValidationError):
        attrs.star_rating = 1  # type: ignore[misc]


# ---------- Mod keys ----------


@pytest.mark.parametrize(
    ("mods", "key"),
    [
        ([], ""),
        (["HD"], "HD"),  # HD changes osu!'s star rating: kept
        (["DT", "HD"], "DT,HD"),
        (["HD", "DT"], "DT,HD"),  # sorted: the order played doesn't matter
        (["NC"], "DT"),  # NC plays the same as DT (osu!'s answers are identical)
        (["NC", "HD"], "DT,HD"),
        (["DC"], "HT"),  # lazer's Daycore is HT's NC
        (["NF"], ""),  # never changes difficulty
        (["SD", "PF"], ""),
        (["HR", "PF", "SD", "NF"], "HR"),
        (["EZ", "FL"], "EZ,FL"),  # FL changes the star rating too
        (["DT", "NC"], "DT"),  # never both, but one key if so
        (["CL"], "CL"),  # lazer's Classic: kept, as it can change sliders' difficulty
    ],
)
def test_mods_key(mods: list[str], key: str) -> None:
    assert mods_key(mods) == key


def test_difficulty_mods_is_a_tuple() -> None:
    assert difficulty_mods(["HD", "NC"]) == ("DT", "HD")


def test_the_dropped_and_merged_mods() -> None:
    assert frozenset({"NF", "SD", "PF"}) == NO_DIFFICULTY
    assert SAME_AS == {"NC": "DT", "DC": "HT"}


# ---------- Storing ----------


class FakeSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> None:
        self.statements.append(statement)


async def test_store_attributes_is_one_upsert() -> None:
    session = FakeSession()
    attrs = AttributesResponse.model_validate(load("attributes_22423_dt.json")).attributes

    await store_attributes(cast(AsyncSession, session), 22423, "DT", attrs)

    [statement] = session.statements
    compiled = statement.compile(dialect=PG)
    sql = " ".join(str(compiled).split())
    assert sql.startswith("INSERT INTO beatmap_attributes")
    # Stored again: the new values replace the old, keyed by map and mods.
    assert "ON CONFLICT (beatmap_id, mods) DO UPDATE SET" in sql
    assert "fetched_at" not in sql.split("DO UPDATE SET")[1]  # the database's time, set once
    params = compiled.params
    assert (params["beatmap_id"], params["mods"]) == (22423, "DT")
    assert params["star_rating"] == pytest.approx(4.982, rel=1e-3)
    assert params["speed_difficult_strain_count"] == pytest.approx(27.764, rel=1e-3)


def test_every_attribute_is_stored() -> None:
    # Each field of the model has a column of the same name.
    from osu_coach.models import BeatmapAttributes

    columns = set(BeatmapAttributes.__table__.columns.keys())
    assert set(DifficultyAttributes.model_fields) <= columns
