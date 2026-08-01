"""The full local gate.

Single source of truth for what "green" means. ``make check`` is a thin wrapper
around this file, so a machine without ``make`` — which is most Windows dev
boxes — runs the identical sequence via ``uv run python scripts/check.py``.
Two hand-maintained copies of a gate drift, and the copy you forget is the one
that stops catching things.

Every step runs even after an earlier one fails, so one invocation shows all the
problems rather than only the first.
"""

from __future__ import annotations

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
            "--cov=src/app",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-fail-under=85",
        ],
    ),
    ("security", ["bandit", "-q", "-ll", "-c", "pyproject.toml", "-r", "src"]),
]


def run(step: Step) -> bool:
    name, command = step
    print(f"\n=== {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(["uv", "run", *command], check=False)  # noqa: S603, S607
    return result.returncode == 0


def main() -> int:
    failed = [name for step in STEPS if not run(step) for name, _ in (step,)]

    print("\n" + "=" * 60)
    for name, _ in STEPS:
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
