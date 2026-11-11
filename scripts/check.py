"""The full local gate.

Single source of truth for what "green" means. ``make check`` is a thin wrapper
around this file, so a machine without ``make`` — which is most Windows dev
boxes — runs the identical sequence via ``uv run python scripts/check.py``.
Two hand-maintained copies of a gate drift, and the copy you forget is the one
that stops catching things.

Every step runs even after an earlier one fails, so one invocation shows all the
problems rather than only the first.

CI selects from these steps with ``--only`` rather than restating the commands,
so the command strings live in exactly one place. The workflows keep their own
job split — the parallelism and the separate check names are worth having — but
they no longer get to disagree with this file about what a step is.

CI also runs the suite in shards (``--shard K/N``) and combines their coverage
afterwards (``--combine-coverage``). The floor stays here: a shard runs without
it, because a third of the suite cannot reach it, and the combine enforces it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

from defusedxml import ElementTree

Step = tuple[str, list[str]]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / "tests"
#: Seconds per test file, from a full run's junit (``--record-durations``). Only
#: the balance of the shards depends on it: a stale or missing entry still runs.
DURATIONS_FILE = PROJECT_ROOT / "scripts" / "test_durations.json"

#: The coverage floor. The full gate and CI's combined shards both read it here.
COVERAGE_FAIL_UNDER = 85

#: The suite as both the full gate and each CI shard run it.
TEST_RUN = [
    "pytest",
    # Four is a measured ceiling, not a CPU guess. Database tests clone and drop
    # PostgreSQL databases, so `auto` can turn extra cores into server
    # contention. The fast gate below stays sequential.
    "-n",
    "4",
    "--dist=worksteal",
    "--cov=src/app",
    "--cov-branch",
]

STEPS: list[Step] = [
    ("format", ["ruff", "format", "--check", "."]),
    ("lint", ["ruff", "check", "."]),
    ("types", ["mypy", "src"]),
    ("layers", ["python", "scripts/check_layers.py"]),
    ("size", ["python", "scripts/check_file_size.py"]),
    (
        "tests",
        [*TEST_RUN, "--cov-report=term-missing", f"--cov-fail-under={COVERAGE_FAIL_UNDER}"],
    ),
    ("security", ["bandit", "-q", "-ll", "-c", "pyproject.toml", "-r", "src"]),
]

#: The inner loop: everything that answers in seconds, and unit tests without
#: coverage.
#:
#: **This exists because the full gate is the wrong feedback loop for a typo.**
#: The suite is ~95% of the gate's runtime — measured at 478s of a 500s run — so
#: every one-line lint error cost a full cycle to discover. Three separate
#: commits were rejected for something `ruff` answers in two seconds.
#:
#: It is deliberately **not** a substitute for the gate. It skips the database
#: tests, which is where every authorization defect this project has found was
#: found, and it skips coverage, so it cannot tell you the threshold still holds.
#: Run it while writing; run the full gate before committing.
_FAST_STEPS = {"format", "lint", "types", "layers", "size"}
FAST: list[Step] = [step for step in STEPS if step[0] in _FAST_STEPS] + [
    ("tests-unit", ["pytest", "tests/unit", "-q"])
]


#: After the shards: their data files (``COVERAGE_FILE=.coverage.<shard>``) into
#: one, then the same floor and the same report the full gate gives.
COMBINE: list[Step] = [
    ("coverage-combine", ["coverage", "combine"]),
    ("coverage-report", ["coverage", "report", f"--fail-under={COVERAGE_FAIL_UNDER}"]),
    ("coverage-xml", ["coverage", "xml"]),
]


def test_files() -> list[str]:
    """Every file pytest collects by its default patterns, relative to the root."""
    found = {*TEST_ROOT.rglob("test_*.py"), *TEST_ROOT.rglob("*_test.py")}
    return sorted(path.relative_to(PROJECT_ROOT).as_posix() for path in found)


def load_durations() -> dict[str, float]:
    if not DURATIONS_FILE.exists():
        return {}
    data: dict[str, float] = json.loads(DURATIONS_FILE.read_text(encoding="utf-8"))
    return data


def split(files: list[str], durations: dict[str, float], count: int) -> list[list[str]]:
    """Deal files onto ``count`` shards, longest first onto the lightest shard.

    By file, not by test, so a module's fixtures are set up in one shard only. A
    file with no recorded duration weighs the median. Ties break by name and by
    shard index, so the same inputs always give the same shards.
    """
    known = [durations[path] for path in files if path in durations]
    default = statistics.median(known) if known else 1.0
    weight = {path: durations.get(path, default) for path in files}
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for path in sorted(files, key=lambda path: (-weight[path], path)):
        lightest = min(range(count), key=lambda index: (loads[index], index))
        shards[lightest].append(path)
        loads[lightest] += weight[path]
    return [sorted(shard) for shard in shards]


def shard_step(spec: str, files: list[str] | None = None) -> Step:
    """The tests step for shard ``K/N``: its files, coverage data, no floor."""
    index, _, total = spec.partition("/")
    if not (index.isdigit() and total.isdigit() and 1 <= int(index) <= int(total)):
        raise SystemExit(f"--shard takes K/N with 1 <= K <= N, not {spec!r}.")
    chosen = split(files if files is not None else test_files(), load_durations(), int(total))
    mine = chosen[int(index) - 1]
    if not mine:
        # `pytest` with no paths runs the whole suite, which would pass as a shard.
        raise SystemExit(f"Shard {spec} has no test files.")
    return (f"tests-shard-{index}", [*TEST_RUN, "--cov-report=", *mine])


def durations_from_junit(paths: list[Path]) -> dict[str, float]:
    """Seconds per test file from pytest's junit, keyed as ``test_files`` names them.

    A classname is the module's dotted path plus any class, so the file is its
    longest prefix that exists. A test whose file no longer exists is dropped.
    """
    totals: dict[str, float] = {}
    for junit in paths:
        for case in ElementTree.parse(junit).iter("testcase"):
            parts = case.get("classname", "").split(".")
            for end in range(len(parts), 0, -1):
                relative = "/".join(parts[:end]) + ".py"
                if (PROJECT_ROOT / relative).is_file():
                    totals[relative] = totals.get(relative, 0.0) + float(case.get("time", 0))
                    break
    return {path: round(seconds, 2) for path, seconds in sorted(totals.items())}


def run(step: Step) -> bool:
    name, command = step
    print(f"\n=== {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(["uv", "run", *command], check=False)  # noqa: S603, S607
    return result.returncode == 0


def select(names: list[str]) -> list[Step]:
    """Return the named steps in declared order; an empty selection means all.

    An unrecognised name is fatal rather than ignored. Silently filtering a typo
    down to nothing would run zero steps and exit 0 — a green check that never
    ran, which is the failure this repository keeps meeting from other angles.
    """
    if not names:
        return list(STEPS)

    known = {name for name, _ in STEPS}
    unknown = sorted(set(names) - known)
    if unknown:
        raise SystemExit(
            f"Unknown step(s): {', '.join(unknown)}. Known steps: {', '.join(sorted(known))}."
        )

    wanted = set(names)
    return [step for step in STEPS if step[0] in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--only",
        default="",
        metavar="STEPS",
        help="comma-separated subset to run, e.g. --only lint,types. Default: every step.",
    )
    parser.add_argument(
        "--shard",
        metavar="K/N",
        help="CI: run shard K of N of the tests step, writing coverage data without the floor",
    )
    parser.add_argument(
        "--combine-coverage",
        action="store_true",
        help="CI: combine the shards' coverage data and enforce the floor",
    )
    parser.add_argument(
        "--record-durations",
        nargs="+",
        type=Path,
        metavar="JUNIT",
        help=f"rewrite {DURATIONS_FILE.name} from a full run's junit file(s) and exit",
    )
    parser.add_argument("--list", action="store_true", help="print the step names and exit")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="the inner loop: lint, types, layers and unit tests, no database, no coverage",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name, command in STEPS:
            print(f"{name}: {' '.join(command)}")
        return 0

    if args.fast and args.only:
        # Two selections would silently pick one. Refusing is the same reasoning
        # `select` uses for an unknown name: a check that quietly ran something
        # other than what was asked for is worse than one that refused.
        raise SystemExit("--fast and --only cannot be combined; --fast is a preset of --only.")
    chosen = (args.fast or args.only, args.shard, args.combine_coverage, args.record_durations)
    if sum(map(bool, chosen)) > 1:
        raise SystemExit(
            "Choose one of --fast/--only, --shard, --combine-coverage and --record-durations."
        )

    if args.record_durations:
        durations = durations_from_junit(args.record_durations)
        DURATIONS_FILE.write_text(json.dumps(durations, indent=1) + "\n", encoding="utf-8")
        print(f"Recorded {len(durations)} files to {DURATIONS_FILE.name}.")
        return 0

    if args.shard:
        steps = [shard_step(args.shard)]
    elif args.combine_coverage:
        steps = COMBINE
    elif args.fast:
        steps = FAST
    else:
        steps = select([name for name in args.only.split(",") if name])
    failed = [name for name, command in steps if not run((name, command))]

    print("\n" + "=" * 60)
    for name, _ in steps:
        print(f"  {'FAIL' if name in failed else 'ok  '}  {name}")
    print("=" * 60)

    if failed:
        print(f"\nGate failed: {', '.join(failed)}", file=sys.stderr)
        print(
            "Do not lower a threshold to go green. If a threshold is wrong, say so and ask.",
            file=sys.stderr,
        )
        return 1

    print("\nGate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
