"""Integration tests: a real model, served by a local Ollama, writes a coaching report.

Run with:  uv run pytest -m integration tests/integration/test_coach_llm_live.py
Needs:     Ollama running (https://ollama.com) with the model pulled: `ollama pull qwen3:8b`,
           or another named by OLLAMA_TEST_MODEL. Skips with the reason otherwise (as in
           CI, which has no model). Slow on a CPU: a few minutes per report.
The facts are written by hand, so no database is needed. A model's words can't be
checked word for word; these check what must hold for any model: the facts are code's,
every number is one of them, and a failure falls back to the template with a reason.
"""

import os
from collections.abc import AsyncIterator

import httpx
import pytest

from osu_coach.coach import CoachFacts, ReplayFacts, ReportMap, template_report
from osu_coach.coach_llm import Written, fact_numbers, llm_report, tool_numbers, unknown_numbers
from osu_coach.config import get_settings
from osu_coach.llm import OllamaClient
from osu_coach.skills import Play

pytestmark = pytest.mark.integration

MODEL = os.environ.get("OLLAMA_TEST_MODEL", "qwen3:8b")
TIMEOUT_S = 900

FACTS = CoachFacts(
    player_id=2,
    username="peppy",
    scores={
        "aim": 41.1,
        "speed": 32.9,
        "accuracy": 69.4,
        "consistency": 80.9,
        "stamina": 38.3,
        "reading": 8.0,
    },
    plays=[
        Play(0.99, 0, 500, 500, 2.0, 1.6, 150, 8.5, 8, ()),
        Play(0.93, 3, 210, 480, 1.7, 1.9, 223, 9.0, 7, ()),
        Play(0.96, 1, 300, 420, 1.5, 1.2, 90, 6.0, 6, ()),
    ],
    replays=ReplayFacts(
        count=1,
        mean_ur=95.5,
        miss_rates={"jump": 0.0, "stream": 0.014},
        notes={"jump": 115, "stream": 556},
    ),
    maps=[
        ReportMap(
            beatmap_id=1,
            name="Hige Driver - Miracle Sugite Yabai [Insane]",
            star_rating=4.05,
            reason="AR 8.5; 4.0★, 0.4★ above your comfort level (3.7★)",
        )
    ],
)


@pytest.fixture
async def ollama() -> AsyncIterator[OllamaClient]:
    url = get_settings().ollama_url
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as http:
        try:
            tags = (await http.get(f"{url}/api/tags", timeout=3)).json()
        except httpx.HTTPError:
            pytest.skip(f"Ollama isn't running at {url}. Start it, or install it from ollama.com")
        names = {m["name"] for m in tags.get("models", [])}
        if MODEL not in names and f"{MODEL}:latest" not in names:
            pytest.skip(f"Model {MODEL} isn't pulled. Run: ollama pull {MODEL}")
        yield OllamaClient(http, url, MODEL)


async def test_a_real_models_report(ollama: OllamaClient) -> None:
    report = await llm_report(FACTS, ollama)
    template = template_report(FACTS)

    assert report.model == f"ollama:{MODEL}"
    # Whoever wrote the words, the facts are code's.
    assert [(w.skill, w.score, w.evidence) for w in report.weaknesses] == [
        (w.skill, w.score, w.evidence) for w in template.weaknesses
    ]
    assert report.strengths == template.strengths
    assert report.maps == template.maps
    if report.source == "template":
        # The model's words were refused: it must say why.
        assert report.fallback_reason
        pytest.xfail(f"{MODEL} fell back to the template: {report.fallback_reason}")
    # Accepted: words for every weakness and step, and no number that isn't a fact.
    assert report.fallback_reason is None
    assert report.summary and all(w.explanation for w in report.weaknesses)
    assert all(p.text for p in report.plan)
    written = Written(
        summary=report.summary,
        weaknesses=[{"skill": w.skill, "explanation": w.explanation} for w in report.weaknesses],
        plan=[{"skill": p.skill, "text": p.text} for p in report.plan],
    )
    assert unknown_numbers(written, fact_numbers(template) | tool_numbers(FACTS)) == []
