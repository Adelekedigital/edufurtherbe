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
"""

from __future__ import annotations

import argparse
import subprocess
import sys

Step = tuple[str, list[str]]

STEPS: list[Step] = [
    ("format", ["ruff", "format", "--check", "."]),
    ("lint", ["ruff", "check", "."]),
    ("types", ["mypy", "src"]),
    ("layers", ["python", "scripts/check_layers.py"]),
    (
        "tests",
        [
            "pytest",
            # Four is a measured ceiling, not a CPU guess. Database tests clone
            # and drop PostgreSQL databases, so `auto` can turn extra cores into
            # server contention. The fast gate below stays sequential.
            "-n",
            "4",
            "--dist=worksteal",
            "--cov=src/app",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-fail-under=85",
        ],
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
FAST: list[Step] = [step for step in STEPS if step[0] in {"format", "lint", "types", "layers"}] + [
    ("tests-unit", ["pytest", "tests/unit", "-q"])
]


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

    steps = FAST if args.fast else select([name for name in args.only.split(",") if name])
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
