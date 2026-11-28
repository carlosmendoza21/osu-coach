"""Run the evals from the command line.

  uv run python -m osu_coach.evals export --all        save every scored player as a case
  uv run python -m osu_coach.evals export 2 7562902    ...or these players
  uv run python -m osu_coach.evals check               the checks on template reports
  uv run python -m osu_coach.evals check --model ollama:qwen3:8b   ...on a model's reports

`export` reads the database; `check` reads only the saved cases (evals/cases/). `check`
exits with 1 if any check fails, so it can gate CI.
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from osu_coach.coach import Report, template_report
from osu_coach.coach_llm import llm_report
from osu_coach.config import get_settings
from osu_coach.evals.cases import CASES_DIR, Case, export_case, load_facts, read_cases, write_case
from osu_coach.evals.checks import CaseResult, check_case
from osu_coach.llm import LLMClient, client_from_setting
from osu_coach.models import SkillProfile

LLM_TIMEOUT_S = 900


async def export(osu_ids: Sequence[int], everyone: bool, directory: Path) -> int:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with async_sessionmaker(engine)() as session:
            if everyone:
                osu_ids = list(await session.scalars(select(SkillProfile.player_id)))
            for osu_id in osu_ids:
                case = await export_case(session, osu_id)
                if case is None:
                    print(f"{osu_id}: not scored yet, skipped")
                    continue
                if not case.facts.plays:
                    print(f"{case.name}: no plays, skipped")
                    continue
                path = write_case(case, directory)
                print(f"{case.name}: {len(case.facts.plays)} plays, "
                      f"{len(case.practice.maps)} maps -> {path}")  # fmt: skip
    finally:
        await engine.dispose()
    return 0


async def write_reports(cases: Sequence[Case], model: str) -> list[tuple[Case, Report, float]]:
    """Each case's report (template, or by the named model) and the seconds it took."""
    out: list[tuple[Case, Report, float]] = []
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as http:
        settings = get_settings()
        llm: LLMClient | None = client_from_setting(model, http, settings.ollama_url)
        for case in cases:
            facts = load_facts(case.facts)
            started = time.monotonic()
            report = template_report(facts) if llm is None else await llm_report(facts, llm)
            out.append((case, report, time.monotonic() - started))
            if llm is not None:
                print(f"  {case.name}: {report.source} in {out[-1][2]:.0f}s", flush=True)
    return out


def summary(results: Sequence[CaseResult]) -> str:
    """Each check's pass rate over the cases, then the cases that failed something."""
    passed: dict[str, int] = defaultdict(int)
    seen: dict[str, int] = defaultdict(int)
    values: dict[str, list[float]] = defaultdict(list)
    for r in results:
        for c in r.checks:
            seen[c.name] += 1
            passed[c.name] += c.passed
            if c.value is not None:
                values[c.name].append(c.value)
    lines = [f"{'check':<48} passed"]
    for name in seen:
        extra = ""
        if values[name]:
            extra = f"   (mean {sum(values[name]) / len(values[name]):.0%})"
        lines.append(f"{name:<48} {passed[name]}/{seen[name]}{extra}")
    llm = sum(r.source == "llm" for r in results)
    lines.append(f"\nreports with the model's words: {llm}/{len(results)}")
    for r in results:
        if r.fallback_reason:
            lines.append(f"  {r.case}: fell back: {r.fallback_reason}")
    failing = [r for r in results if not r.passed]
    if failing:
        lines.append("\nfailed:")
        for r in failing:
            for c in r.checks:
                if not c.passed:
                    lines.append(f"  {r.case}: {c.name}: {c.detail}")
    return "\n".join(lines)


async def check(model: str, directory: Path, limit: int | None) -> int:
    cases = read_cases(directory)[:limit]
    if not cases:
        print(f"no cases in {directory}: run `export` first")
        return 1
    print(f"{len(cases)} cases, reports by {model or 'templates'}")
    reports = await write_reports(cases, model)
    results = [check_case(case, report) for case, report, _ in reports]
    print(summary(results))
    return 0 if all(r.passed for r in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m osu_coach.evals")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="save players from the database as eval cases")
    exp.add_argument("osu_ids", nargs="*", type=int)
    exp.add_argument("--all", action="store_true", help="every scored player")
    exp.add_argument("--cases", type=Path, default=CASES_DIR)
    chk = sub.add_parser("check", help="write each case's report and run the checks")
    chk.add_argument("--model", default="", help='e.g. "ollama:qwen3:8b"; empty: templates')
    chk.add_argument("--cases", type=Path, default=CASES_DIR)
    chk.add_argument("--limit", type=int, default=None, help="only the first N cases")
    args = parser.parse_args(argv)

    if args.command == "export":
        if not args.osu_ids and not args.all:
            parser.error("export: give osu! ids, or --all")
        run = export(args.osu_ids, args.all, args.cases)
    else:
        run = check(args.model, args.cases, args.limit)
    return asyncio.run(run, loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    sys.exit(main())
