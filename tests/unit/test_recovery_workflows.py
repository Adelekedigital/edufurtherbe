"""The manual recovery workflows pass what their job reads.

``settle-sessions.yml`` re-runs settlement by hand when a QStash run fails, and
settlement cleans up calendar events through the platform calendar. Every
setting that calendar reads must reach the run: a missing
``GOOGLE_CALENDAR_ID`` falls back to ``primary``, which the platform token's
``calendar.app.created`` scope cannot reach, so the cleanup fails only on the
day somebody needs the button.

Read as text, as ``test_check_script`` reads the workflows: no YAML dependency.
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: What the platform calendar is built from (`calendar_for` in
#: `infra/clients/meetings.py`): the client, the token it issued, the calendar.
PLATFORM_CALENDAR = {
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_ID",
}

_ENV_LINE = re.compile(r"^\s+([A-Z][A-Z0-9_]*):\s*(\$\{\{[^}]*\}\})\s*$", re.MULTILINE)


def step_env(workflow: str, step: str) -> dict[str, str]:
    """The ``NAME: ${{ ... }}`` lines of one step, up to the next step."""
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    start = text.find(f"- name: {step}\n")
    assert start != -1, f"{workflow} has no step named {step!r}"
    end = text.find("\n      - ", start + 1)
    block = text[start : end if end != -1 else len(text)]
    return dict(_ENV_LINE.findall(block))


def test_settle_recovery_passes_the_whole_platform_calendar() -> None:
    env = step_env("settle-sessions.yml", "Settle sessions")
    assert env.keys() >= PLATFORM_CALENDAR, PLATFORM_CALENDAR - env.keys()


def test_the_calendar_id_comes_from_the_chosen_environment() -> None:
    env = step_env("settle-sessions.yml", "Settle sessions")
    assert env.get("GOOGLE_CALENDAR_ID") == "${{ vars.GOOGLE_CALENDAR_ID }}"
