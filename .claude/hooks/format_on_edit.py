"""PostToolUse hook: format and autofix a Python file right after it is edited.

Fails open. A formatter is a convenience; it must never be the reason a task
cannot proceed, so every unexpected condition exits 0 silently.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def find_ruff() -> str | None:
    """Prefer the project venv over whatever happens to be on PATH."""
    candidates = [
        PROJECT_ROOT / ".venv" / "Scripts" / "ruff.exe",
        PROJECT_ROOT / ".venv" / "bin" / "ruff",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ruff")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    raw_path = payload.get("tool_input", {}).get("file_path")
    if not raw_path:
        return 0

    target = Path(raw_path)
    if target.suffix != ".py" or not target.is_file():
        return 0

    # .claude/ is excluded from ruff in both pyproject.toml and pre-commit;
    # formatting it here would contradict both.
    try:
        if ".claude" in target.resolve().relative_to(PROJECT_ROOT).parts:
            return 0
    except ValueError:
        return 0

    ruff = find_ruff()
    if ruff is None:
        return 0

    for args in (["format", str(target)], ["check", "--fix", "--quiet", str(target)]):
        try:
            subprocess.run([ruff, *args], cwd=PROJECT_ROOT, capture_output=True, timeout=20)  # noqa: S603
        except (OSError, subprocess.SubprocessError):
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
