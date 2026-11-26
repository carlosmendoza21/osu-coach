"""Unit tests for osu_coach.coach_llm: the tools, the number check, merging the model's
words into the template report, and the whole loop with its fallback. The model is a
fake that replays scripted answers; tests/integration/test_coach_llm_live.py runs a real
one.
"""

import json
from typing import Any

import pytest

from osu_coach.coach import CoachFacts, ReplayFacts, ReportMap, template_report
from osu_coach.coach_llm import (
    MAX_TOOL_ROUNDS,
    TOOLS,
    Rejected,
    Written,
    fact_numbers,
    llm_report,
    merge,
    run_tool,
    tool_numbers,
    unknown_numbers,
    writing_schema,
)
from osu_coach.llm import LLMError, Message, Reply, Tool, ToolCall
from osu_coach.skills import Play

PLAYS = [
    Play(0.99, 0, 500, 500, 3.0, 2.5, 300, 9.7, 9, ("HD",)),
    Play(0.95, 2, 250, 500, 2.0, 1.5, 500, 9.0, 8, ()),
]
SCORES = {"aim": 90.0, "speed": 40.0, "accuracy": 75.0, "consistency": 30.0, "stamina": 55.5,
          "reading": 60.0}  # fmt: skip
REPLAYS = ReplayFacts(count=2, mean_ur=85.25, miss_rates={"jump": 0.02, "stream": 0.05},
                      notes={"jump": 100, "stream": 200})  # fmt: skip
REASON = "12% of its notes in streams of 16+ at 180 BPM; 5.1★, 0.4★ above ..."
MAP = ReportMap(beatmap_id=1, name="Artist - Title [Insane]", star_rating=5.12, reason=REASON)


def facts(replays: ReplayFacts | None = REPLAYS, maps: list[ReportMap] | None = None) -> CoachFacts:
    return CoachFacts(player_id=2, username="peppy", scores=SCORES, plays=PLAYS, replays=replays,
                      maps=[MAP] if maps is None else maps)  # fmt: skip


# The template's weaknesses, weakest first: consistency (30), speed (40), stamina (55.5).
WEAK = ["consistency", "speed", "stamina"]


def written(**changes: Any) -> dict[str, Any]:
    """Words for every weakness, with numbers that are all facts."""
    base: dict[str, Any] = {
        "summary": "You aim very well; consistency is where to work.",
        "weaknesses": [
            {"skill": s, "explanation": f"About {s}: 75% of the combo held."} for s in WEAK
        ],
        "plan": [{"skill": s, "text": f"Practise {s} for 3 sessions."} for s in WEAK],
    }
    return {**base, **changes}


class FakeModel:
    """Replays scripted answers in order, and records what it was asked."""

    name = "fake:model"

    def __init__(self, *answers: Reply | Exception) -> None:
        self.answers = list(answers)
        self.asked: list[dict[str, Any]] = []

    async def chat(self, messages: list[Message], tools: list[Tool] | None = None,
                   schema: dict[str, Any] | None = None) -> Reply:  # fmt: skip
        self.asked.append({"messages": list(messages), "tools": tools, "schema": schema})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def calls(*names: str) -> Reply:
    tool_calls = [ToolCall(n, {}) for n in names]
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": n}} for n in names],
    }
    return Reply("", tool_calls, message)


def text(content: str) -> Reply:
    return Reply(content, [], {"role": "assistant", "content": content})


def words(**changes: Any) -> Reply:
    return text(json.dumps(written(**changes)))


# ---------- The tools ----------


def test_the_tools_names() -> None:
    # The brief's names for the coach's tools.
    assert set(TOOLS) == {
        "get_skill_profile",
        "get_replay_analysis",
        "get_miss_patterns",
        "search_beatmaps",
    }
    assert all(tool.name == name and tool.description for name, (tool, _) in TOOLS.items())


def test_the_skill_profile_tool() -> None:
    out = json.loads(run_tool("get_skill_profile", facts()))

    assert out["player"] == "peppy"
    assert out["scores_0_to_100"] == SCORES
    assert out["evidence"]["consistency"][0].startswith("You hold 75% of a map's combo")
    assert out["evidence"]["accuracy"][-1].startswith("Your 2 uploaded replay(s) average 85.2 UR")


def test_the_replay_tools() -> None:
    assert json.loads(run_tool("get_replay_analysis", facts())) == {
        "replays": 2,
        "mean_unstable_rate": 85.25,
        "notes_by_pattern": {"jump": 100, "stream": 200},
    }
    # Worst first, as percentages the way the evidence writes them.
    assert json.loads(run_tool("get_miss_patterns", facts())) == {
        "miss_rate_by_pattern": {"stream": "5.0%", "jump": "2.0%"},
        "notes_by_pattern": {"stream": 200, "jump": 100}}  # fmt: skip


def test_the_replay_tools_without_replays() -> None:
    assert json.loads(run_tool("get_replay_analysis", facts(replays=None)))["replays"] == 0
    assert "note" in json.loads(run_tool("get_miss_patterns", facts(replays=None)))


def test_the_maps_tool() -> None:
    assert json.loads(run_tool("search_beatmaps", facts())) == {
        "maps": [{"name": MAP.name, "stars": 5.12, "reason": MAP.reason}]
    }
    assert "note" in json.loads(run_tool("search_beatmaps", facts(maps=[])))


def test_an_unknown_tool_is_an_answer_not_a_crash() -> None:
    # Edge: a small model may make a name up; it's told so and can carry on.
    assert json.loads(run_tool("get_pp", facts())) == {"error": "no tool named get_pp"}


# ---------- The number check ----------


def test_the_facts_numbers() -> None:
    allowed = fact_numbers(template_report(facts()))

    # Scores as stored and rounded, evidence, the map's stars and reason, and 100 (/100).
    assert {
        30.0,
        40.0,
        55.5,
        56.0,
        90.0,
        75.0,
        100.0,
        1.0,
        85.2,
        5.12,
        12.0,
        16.0,
        180.0,
    } <= allowed


def test_the_tools_numbers() -> None:
    # The raw values the tools give, beyond the evidence's rounded ones.
    assert {85.25, 5.12, 100.0, 200.0, 90.0} <= tool_numbers(facts())


def test_the_check_uses_the_tools_numbers_too() -> None:
    # 85.3 is the replay tool's 85.25 rounded, though the evidence says 85.2.
    template = template_report(facts())
    words = Written.model_validate(written(summary="Your UR of 85.3 is steady."))

    with pytest.raises(Rejected):
        merge(template, words)  # the report's own numbers only
    assert merge(template, words, fact_numbers(template) | tool_numbers(facts())).source == "llm"


@pytest.mark.parametrize(
    ("sentence", "unknown"),
    [
        ("You hold 75% of a map's combo.", []),
        ("Your accuracy is 97.0%.", []),
        ("Hit 1,811 notes.", []),  # a thousands comma is read as one number
        ("Aim for 3 sessions a week, 2 maps each.", []),  # small counts written whole
        ("Your consistency is 30/100.", []),
        ("Play maps at AR 8.0 or higher.", [8.0]),  # a decimal reads as a measurement
        ("Your UR of 85.3 is good.", []),  # edge: 85.25 rounded (the replay tool's raw UR)
        ("Your UR of 85.4 is good.", [85.4]),  # edge: just over
        ("Push your stamina to 70.", [70.0]),  # a whole number over 10 must be a fact
        ("Try 11 maps.", [11.0]),  # edge: just over the small-count limit
        ("Try 10 maps.", []),  # edge: at it
    ],
)
def test_unknown_numbers(sentence: str, unknown: list[float]) -> None:
    allowed = fact_numbers(template_report(facts())) | tool_numbers(facts()) | {1811.0}
    model = Written.model_validate(written(summary=sentence))

    assert unknown_numbers(model, allowed) == unknown


def test_every_part_of_the_words_is_checked() -> None:
    allowed = fact_numbers(template_report(facts()))
    plan = [{"skill": s, "text": "Reach 99.9% accuracy."} for s in WEAK]
    explained = [{"skill": s, "explanation": "You miss 12.5% of streams."} for s in WEAK]

    assert unknown_numbers(Written.model_validate(written(plan=plan)), allowed) == [99.9] * 3
    assert (
        unknown_numbers(Written.model_validate(written(weaknesses=explained)), allowed)
        == [12.5] * 3
    )


# ---------- The shape and merging ----------


def test_the_writing_schema_names_the_weaknesses() -> None:
    schema = writing_schema(WEAK)

    assert schema["required"] == ["summary", "weaknesses", "plan"]
    for part in ("weaknesses", "plan"):
        assert schema["properties"][part]["minItems"] == schema["properties"][part]["maxItems"] == 3
        assert schema["properties"][part]["items"]["properties"]["skill"]["enum"] == WEAK


def test_merging_keeps_the_facts_and_adds_the_words() -> None:
    template = template_report(facts())

    report = merge(template, Written.model_validate(written()))

    assert report.source == "llm"
    assert report.summary == "You aim very well; consistency is where to work."
    # Code's choices are kept: which weaknesses, their order, scores and evidence; the maps.
    assert [(w.skill, w.score, w.evidence) for w in report.weaknesses] == [
        (w.skill, w.score, w.evidence) for w in template.weaknesses]  # fmt: skip
    assert report.strengths == template.strengths
    assert report.maps == template.maps
    assert [w.explanation for w in report.weaknesses] == [
        f"About {s}: 75% of the combo held." for s in WEAK
    ]
    assert [(p.skill, p.text) for p in report.plan] == [
        (s, f"Practise {s} for 3 sessions.") for s in WEAK
    ]


def test_the_models_order_doesnt_matter() -> None:
    # Edge: the words come strongest first (as qwen3 did); code's order still wins.
    shuffled = written()
    shuffled["weaknesses"].reverse()
    shuffled["plan"].reverse()

    report = merge(template_report(facts()), Written.model_validate(shuffled))

    assert [w.skill for w in report.weaknesses] == WEAK
    assert report.weaknesses[0].explanation == "About consistency: 75% of the combo held."


@pytest.mark.parametrize("part", ["weaknesses", "plan"])
def test_a_missing_weakness_is_rejected(part: str) -> None:
    w = written()
    w[part] = w[part][:2]  # stamina left out

    with pytest.raises(Rejected, match="no words for stamina"):
        merge(template_report(facts()), Written.model_validate(w))


def test_made_up_numbers_are_rejected() -> None:
    with pytest.raises(Rejected, match=r"numbers not in the facts: 8, 99\.5"):
        merge(
            template_report(facts()), Written.model_validate(written(summary="AR 8.0 and 99.5%."))
        )


# ---------- The whole loop ----------


async def test_a_report_written_by_the_model() -> None:
    model = FakeModel(
        calls("get_skill_profile", "search_beatmaps"), text("I have what I need."), words()
    )

    report = await llm_report(facts(), model)

    assert (report.source, report.model, report.fallback_reason) == ("llm", "fake:model", None)
    assert report.tools_used == ["get_skill_profile", "search_beatmaps"]
    assert report.weaknesses[0].explanation == "About consistency: 75% of the combo held."


async def test_what_the_model_is_asked() -> None:
    model = FakeModel(calls("get_skill_profile"), text("Done."), words())

    await llm_report(facts(), model)

    first, second, writing = model.asked
    # Looking up: the system rules, the request, and every tool on offer.
    assert first["messages"][0]["role"] == "system"
    assert "Every number you write must come from a tool result" in first["messages"][0]["content"]
    assert first["messages"][1] == {
        "role": "user",
        "content": "Coach peppy. Look up what you need first.",
    }
    assert {t.name for t in first["tools"]} == set(TOOLS)
    assert first["schema"] is None
    # The tool's answer goes back as a tool message after the call.
    assert second["messages"][-2]["tool_calls"][0]["function"]["name"] == "get_skill_profile"
    assert second["messages"][-1]["role"] == "tool"
    assert json.loads(second["messages"][-1]["content"])["player"] == "peppy"
    # Writing: no tools, the schema, and the weaknesses as code ranked them.
    assert writing["tools"] is None
    assert writing["schema"] == writing_schema(WEAK)
    prompt = writing["messages"][-1]["content"]
    assert (
        "weaknesses, weakest first: Consistency (30/100), Speed (40/100), Stamina (55.5/100)"
        in prompt
    )


async def test_looking_up_stops_after_max_rounds() -> None:
    # Edge: a model that never stops calling tools is cut off and made to write.
    rounds = [calls("get_skill_profile") for _ in range(MAX_TOOL_ROUNDS)]
    model = FakeModel(*rounds, words())

    report = await llm_report(facts(), model)

    assert report.source == "llm"
    assert len(model.asked) == MAX_TOOL_ROUNDS + 1
    assert model.asked[-1]["schema"] is not None
    assert report.tools_used == ["get_skill_profile"] * MAX_TOOL_ROUNDS


async def test_a_number_from_a_tool_is_accepted() -> None:
    # The replay tool says 85.25; "85.3" rounds it, though the evidence says 85.2.
    model = FakeModel(calls("get_replay_analysis"), text("ok"), words(summary="Your UR is 85.3."))

    report = await llm_report(facts(), model)

    assert (report.source, report.fallback_reason) == ("llm", None)


async def test_a_model_that_writes_at_once() -> None:
    model = FakeModel(text("Here goes."), words())

    report = await llm_report(facts(), model)

    assert (report.source, report.tools_used) == ("llm", [])


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        (
            (LLMError("ollama:x: ConnectError"),),
            "the model couldn't be reached (ollama:x: ConnectError)",
        ),
        ((text("ok"), LLMError("timeout")), "the model couldn't be reached (timeout)"),
        ((text("ok"), text("not json at all")), "the model's answer wasn't in the report's shape"),
        (
            (text("ok"), text('{"summary": "hi"}')),
            "the model's answer wasn't in the report's shape",
        ),
        ((text("ok"), words(summary="Reach 99.9%.")), "numbers not in the facts: 99.9"),
        ((text("ok"), words(plan=[])), "no words for consistency, speed, stamina"),
    ],
    ids=[
        "unreachable",
        "fails-while-writing",
        "not-json",
        "missing-parts",
        "made-up-number",
        "no-plan",
    ],
)
async def test_falls_back_to_the_template(
    answers: tuple[Reply | Exception, ...], reason: str
) -> None:
    report = await llm_report(facts(), FakeModel(*answers))

    template = template_report(facts())
    assert report.source == "template"
    assert report.fallback_reason == reason
    assert report.model == "fake:model"  # who was tried
    # Otherwise the template report exactly.
    about_the_model = {"model", "fallback_reason", "tools_used"}
    assert report.model_dump(exclude=about_the_model) == template.model_dump(
        exclude=about_the_model
    )


async def test_a_fallback_says_which_tools_were_called() -> None:
    model = FakeModel(calls("get_miss_patterns"), text("ok"), text("broken"))

    report = await llm_report(facts(), model)

    assert (report.source, report.tools_used) == ("template", ["get_miss_patterns"])
