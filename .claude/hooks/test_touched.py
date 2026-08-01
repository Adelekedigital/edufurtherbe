"""Stop hook: run the unit tests when source or tests changed this session.

Catches the case where a change looks finished but the fast suite disagrees.
Only the unit tier runs — it is the tier that must stay fast enough to be worth
running unprompted.

Honours ``stop_hook_active``: if the agent is already responding to this hook,
exit 0. Without that guard a persistently failing suite becomes an infinite loop.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHED = ("src/", "tests/")


def find_pytest() -> str | None:
    candidates = [
        PROJECT_ROOT / ".venv" / "Scripts" / "pytest.exe",
        PROJECT_ROOT / ".venv" / "bin" / "pytest",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("pytest")


def touched_watched_paths() -> bool:
    """True if the working tree has changes under src/ or tests/."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain", "-uall"],  # noqa: S607
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    if result.returncode != 0:
        return False

    for line in result.stdout.splitlines():
        path = line[3:].replace("\\", "/")
        if path.startswith(WATCHED):
            return True
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if payload.get("stop_hook_active"):
        return 0

    unit_dir = PROJECT_ROOT / "tests" / "unit"
    if not unit_dir.is_dir() or not touched_watched_paths():
        return 0

    pytest_bin = find_pytest()
    if pytest_bin is None:
        return 0

    try:
        result = subprocess.run(  # noqa: S603
            [pytest_bin, "tests/unit", "-q", "-p", "no:randomly", "--no-header"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=110,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    if result.returncode != 0:
        tail = (result.stdout or result.stderr).strip().splitlines()[-30:]
        print("Unit tests are failing:\n" + "\n".join(tail), file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
