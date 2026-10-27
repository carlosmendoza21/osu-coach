"""Unit tests for osu_coach.osu.schemas.

The fixtures in tests/fixtures/osu/ are real osu! API v2 responses (public data), saved
so these tests run offline: peppy's profile, three best scores (one with HD+DT), a
failed recent play, and one full beatmap. Edge cases are made by editing copies of them.
tests/integration/test_schemas_live.py checks the models against today's live API.
"""

import copy
import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from osu_coach.osu.schemas import OsuBeatmap, OsuScore, OsuUser, ScoreBeatmap

FIXTURES = Path(__file__).parent.parent / "fixtures" / "osu"
SCORES = TypeAdapter(list[OsuScore])


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def user_json() -> dict[str, Any]:
    data: dict[str, Any] = load("user.json")
    return data


@pytest.fixture
def score_json() -> dict[str, Any]:
    data: dict[str, Any] = load("scores_best.json")[0]
    return data


def edit(data: dict[str, Any], path: list[str], value: Any = ...) -> dict[str, Any]:
    """A copy of `data` with the field at `path` set to `value`, or deleted if no value."""
    out = copy.deepcopy(data)
    target = out
    for key in path[:-1]:
        target = target[key]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return out


def error_at(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[str, str]]:
    return [(".".join(map(str, e["loc"])), e["type"]) for e in exc.value.errors()]


def has_error(exc: pytest.ExceptionInfo[ValidationError], path: list[str], kind: str) -> bool:
    """True if there's a `kind` error at `path` or inside it (e.g. mods.0 for mods)."""
    where = ".".join(path)
    return any(
        error == kind and (loc == where or loc.startswith(where + "."))
        for loc, error in error_at(exc)
    )


# ---------- Real responses ----------


def test_real_user(user_json: dict[str, Any]) -> None:
    user = OsuUser.model_validate(user_json)

    assert (user.id, user.username, user.country_code) == (2, "peppy", "AU")
    assert user.statistics.pp == user_json["statistics"]["pp"]
    assert user.statistics.global_rank == user_json["statistics"]["global_rank"]
    assert user.statistics.play_count == user_json["statistics"]["play_count"]
    # A percent on users (e.g. 96.85), unlike a score's 0-1 accuracy.
    assert 1 < user.statistics.hit_accuracy <= 100


def test_real_user_keeps_only_our_fields(user_json: dict[str, Any]) -> None:
    user = OsuUser.model_validate(user_json)

    # osu! sends about 70 fields; everything we don't model is dropped, not an error.
    assert set(user.model_dump()) == {"id", "username", "country_code", "statistics"}
    assert set(user.model_dump()["statistics"]) == {
        "pp",
        "global_rank",
        "country_rank",
        "play_count",
        "hit_accuracy",
    }


def test_real_best_scores() -> None:
    raw = load("scores_best.json")

    scores = SCORES.validate_python(raw)

    assert len(scores) == 3
    for score, data in zip(scores, raw, strict=True):
        assert score.id == data["id"]
        assert score.mods == data["mods"]
        assert score.pp == data["pp"]
        assert 0 <= score.accuracy <= 1
        assert score.statistics.count_300 == data["statistics"]["count_300"]
        assert score.statistics.count_miss == data["statistics"]["count_miss"]
        assert score.beatmapset.title == data["beatmapset"]["title"]
        assert score.created_at.tzinfo is not None
        assert score.created_at.utcoffset() == UTC.utcoffset(None)


@pytest.mark.parametrize(
    ("index", "want_mods"),
    [
        (0, ["PF"]),
        (1, []),  # no mods is an empty list, not null
        (2, ["HD", "DT"]),  # several mods keep their order
    ],
)
def test_mods_are_acronym_lists(index: int, want_mods: list[str]) -> None:
    score = OsuScore.model_validate(load("scores_best.json")[index])

    assert score.mods == want_mods


def test_beatmap_od_and_hp_come_from_accuracy_and_drain(score_json: dict[str, Any]) -> None:
    beatmap = OsuScore.model_validate(score_json).beatmap

    # On a beatmap, osu!'s "accuracy" is Overall Difficulty and "drain" is HP.
    assert beatmap.od == score_json["beatmap"]["accuracy"]
    assert beatmap.hp == score_json["beatmap"]["drain"]
    assert (beatmap.ar, beatmap.cs) == (score_json["beatmap"]["ar"], score_json["beatmap"]["cs"])


def test_real_failed_play() -> None:
    score = OsuScore.model_validate(load("score_recent_failed.json"))

    assert score.passed is False
    assert score.pp is None
    assert score.rank == "F"


# ---------- Optional values ----------


@pytest.mark.parametrize(
    "path",
    [
        ["statistics", "global_rank"],  # inactive players have no rank
        ["statistics", "country_rank"],
    ],
)
def test_user_ranks_can_be_null(user_json: dict[str, Any], path: list[str]) -> None:
    user = OsuUser.model_validate(edit(user_json, path, None))

    assert getattr(user.statistics, path[-1]) is None


def test_score_pp_can_be_null(score_json: dict[str, Any]) -> None:
    # Unranked and loved maps give no pp.
    assert OsuScore.model_validate(edit(score_json, ["pp"], None)).pp is None


def test_extra_fields_are_ignored(user_json: dict[str, Any]) -> None:
    user = OsuUser.model_validate(edit(user_json, ["brand_new_field"], {"x": 1}))

    assert "brand_new_field" not in user.model_dump()


# ---------- Missing fields ----------


@pytest.mark.parametrize(
    "path",
    [
        ["id"],
        ["username"],
        ["statistics"],
        ["statistics", "pp"],
        ["statistics", "play_count"],
    ],
)
def test_user_missing_field_is_named(user_json: dict[str, Any], path: list[str]) -> None:
    with pytest.raises(ValidationError) as exc:
        OsuUser.model_validate(edit(user_json, path))

    assert (".".join(path), "missing") in error_at(exc)


@pytest.mark.parametrize(
    "path",
    [
        ["id"],
        ["mods"],
        ["passed"],
        ["created_at"],
        ["statistics", "count_300"],
        ["statistics", "count_miss"],
        ["beatmap", "ar"],
        ["beatmap", "accuracy"],  # OD: needed for hit windows on Day 4
        ["beatmapset", "title"],
    ],
)
def test_score_missing_field_is_named(score_json: dict[str, Any], path: list[str]) -> None:
    with pytest.raises(ValidationError) as exc:
        OsuScore.model_validate(edit(score_json, path))

    assert (".".join(path), "missing") in error_at(exc)


def test_od_must_come_from_accuracy_not_od(score_json: dict[str, Any]) -> None:
    # Only osu!'s real field name counts. A field literally called "od" is ignored, so
    # nothing slips through if the alias is ever broken.
    data = edit(score_json, ["beatmap", "accuracy"])
    data["beatmap"]["od"] = 9.0

    with pytest.raises(ValidationError) as exc:
        OsuScore.model_validate(data)

    assert ("beatmap.accuracy", "missing") in error_at(exc)


# ---------- Wrong types ----------


@pytest.mark.parametrize(
    ("path", "value", "error"),
    [
        (["statistics", "pp"], "abc", "float_parsing"),
        (["statistics", "global_rank"], "first", "int_parsing"),
        (["id"], "two", "int_parsing"),
        (["username"], 123, "string_type"),
    ],
)
def test_user_wrong_type_is_named(
    user_json: dict[str, Any], path: list[str], value: Any, error: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        OsuUser.model_validate(edit(user_json, path, value))

    assert has_error(exc, path, error), error_at(exc)


@pytest.mark.parametrize(
    ("path", "value", "error"),
    [
        (["mods"], "HDDT", "list_type"),  # must be a list of acronyms, not one string
        (["mods"], [64], "string_type"),  # nor the old v1 bitmask numbers
        (["statistics", "count_300"], "lots", "int_parsing"),
        (["created_at"], "yesterday", "datetime_from_date_parsing"),
        (["passed"], "maybe", "bool_parsing"),
        (["beatmap", "accuracy"], None, "float_type"),
    ],
)
def test_score_wrong_type_is_named(
    score_json: dict[str, Any], path: list[str], value: Any, error: str
) -> None:
    with pytest.raises(ValidationError) as exc:
        OsuScore.model_validate(edit(score_json, path, value))

    assert has_error(exc, path, error), error_at(exc)


def test_new_score_format_is_rejected(score_json: dict[str, Any]) -> None:
    # We read the legacy format. If osu! starts sending the new one (great/ok/meh/miss,
    # mods as objects), fail loudly instead of storing wrong numbers.
    data = edit(score_json, ["statistics"], {"great": 254})
    data["mods"] = [{"acronym": "HD"}]

    with pytest.raises(ValidationError) as exc:
        OsuScore.model_validate(data)

    errors = error_at(exc)
    assert ("statistics.count_300", "missing") in errors
    assert ("mods.0", "string_type") in errors


# ---------- Read-only ----------


def test_models_are_frozen(user_json: dict[str, Any], score_json: dict[str, Any]) -> None:
    user = OsuUser.model_validate(user_json)
    score = OsuScore.model_validate(score_json)

    with pytest.raises(ValidationError):
        user.username = "someone-else"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        score.statistics.count_miss = 0  # type: ignore[misc]


# ---------- Beatmaps (GET /beatmaps/{id} and /beatmaps/lookup) ----------


@pytest.fixture
def beatmap_json() -> dict[str, Any]:
    data: dict[str, Any] = load("beatmap.json")
    return data


def test_real_beatmap(beatmap_json: dict[str, Any]) -> None:
    beatmap = OsuBeatmap.model_validate(beatmap_json)

    assert (beatmap.id, beatmap.version) == (22423, "Hard")
    assert beatmap.max_combo == beatmap_json["max_combo"]
    assert beatmap.checksum == beatmap_json["checksum"]
    assert (beatmap.status, beatmap.mode) == ("ranked", "osu")
    assert beatmap.beatmapset.title == beatmap_json["beatmapset"]["title"]
    assert beatmap.beatmapset.artist == beatmap_json["beatmapset"]["artist"]
    assert beatmap.beatmapset.creator == beatmap_json["beatmapset"]["creator"]


def test_beatmap_od_and_hp_are_inherited_aliases(beatmap_json: dict[str, Any]) -> None:
    beatmap = OsuBeatmap.model_validate(beatmap_json)

    # Inherited from ScoreBeatmap: osu!'s "accuracy" is OD and "drain" is HP.
    assert isinstance(beatmap, ScoreBeatmap)
    assert (beatmap.od, beatmap.hp) == (beatmap_json["accuracy"], beatmap_json["drain"])
    assert (beatmap.ar, beatmap.cs, beatmap.bpm) == (
        beatmap_json["ar"],
        beatmap_json["cs"],
        beatmap_json["bpm"],
    )


def test_beatmap_keeps_only_our_fields(beatmap_json: dict[str, Any]) -> None:
    beatmap = OsuBeatmap.model_validate(beatmap_json)

    dumped = beatmap.model_dump()
    assert "failtimes" not in dumped and "playcount" not in dumped
    assert set(dumped["beatmapset"]) == {"id", "artist", "title", "creator"}


def test_beatmapset_is_a_mapset_not_a_difficulty(beatmap_json: dict[str, Any]) -> None:
    # The mapset has no version, AR or OD of its own. Typing it as a difficulty
    # (ScoreBeatmap) would demand those fields and reject every real beatmap.
    for field in ("version", "ar", "accuracy", "difficulty_rating"):
        assert field not in beatmap_json["beatmapset"]

    OsuBeatmap.model_validate(beatmap_json)  # must not raise


@pytest.mark.parametrize("field", ["max_combo", "checksum"])
def test_beatmap_optional_values_can_be_null(beatmap_json: dict[str, Any], field: str) -> None:
    beatmap = OsuBeatmap.model_validate(edit(beatmap_json, [field], None))

    assert getattr(beatmap, field) is None


@pytest.mark.parametrize(
    "path",
    [
        ["status"],
        ["mode"],
        ["beatmapset"],
        ["beatmapset", "title"],
        ["accuracy"],  # OD, from the inherited fields
        ["max_combo"],  # may be null, but must be present
    ],
)
def test_beatmap_missing_field_is_named(beatmap_json: dict[str, Any], path: list[str]) -> None:
    with pytest.raises(ValidationError) as exc:
        OsuBeatmap.model_validate(edit(beatmap_json, path))

    assert (".".join(path), "missing") in error_at(exc)


def test_a_scores_beatmap_is_not_a_full_beatmap(score_json: dict[str, Any]) -> None:
    # A score only carries part of the map: no max_combo and no mapset inside it.
    with pytest.raises(ValidationError) as exc:
        OsuBeatmap.model_validate(score_json["beatmap"])

    errors = error_at(exc)
    assert ("max_combo", "missing") in errors
    assert ("beatmapset", "missing") in errors
