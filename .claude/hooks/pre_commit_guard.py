"""PreToolUse hook: refuse the shell commands that bypass the local gate.

The gate in .pre-commit-config.yaml is only worth having if it cannot be skipped
casually. `--no-verify` disables the secret scan and the layer check along with
whatever the author meant to skip, which is exactly how a credential reaches a
public history.

Exit 2 blocks the call and returns stderr to the agent.

This hook deliberately does NOT fail open. An earlier version caught every parse
error and returned 0, so a byte-order mark on stdin silently disabled the guard
while the hook still reported success — the check was gone and nothing said so.
When the payload cannot be parsed we scan the raw text instead: a malformed
envelope can no longer turn the guard off, and we still never block blindly.
"""

from __future__ import annotations

import json
import re
import sys

BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bgit\s+commit\b.*?(--no-verify|(?<!\w)-n(?!\w))"),
        "git commit --no-verify skips the secret scan, the layer check, and the unit tests.\n"
        "Fix what the hooks are objecting to, or say why the hook itself is wrong.",
    ),
    (
        re.compile(r"\bgit\s+push\b.*?(--force(?!-with-lease)|(?<!\w)-f(?!\w))"),
        "Plain --force can discard a teammate's commits. Use --force-with-lease.",
    ),
    (
        re.compile(r"\bpre-commit\s+uninstall\b"),
        "Uninstalling the hooks disables every local gate at once.",
    ),
]


def extract_command(raw: str) -> str:
    """Return the command to inspect.

    Falls back to the raw payload when it will not parse, so an encoding quirk
    cannot disable the guard. The raw envelope still contains the command text,
    so the patterns below continue to match.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw

    if not isinstance(payload, dict):
        return raw

    command = payload.get("tool_input", {}).get("command")
    return command if isinstance(command, str) else raw


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    if not raw.strip():
        return 0

    command = extract_command(raw)

    for pattern, message in BLOCKED:
        if pattern.search(command):
            print(f"Blocked: {command.strip()}\n\n{message}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
