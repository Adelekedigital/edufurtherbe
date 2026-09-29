"""No Python file over `MAX_CODE_LINES` lines of code.

Counts what a reader has to hold in their head: lines carrying a token that is
not a comment, and not part of a docstring. Blank lines, comments and
docstrings are free, so explaining a decision never pushes a file over. Keep
them succinct all the same. `migrations/` is exempt: a migration can carry
reference data measured in thousands of lines, and it is not code anyone edits.

Runs in pre-commit and in CI. Exits non-zero naming every file over the limit.
The answer to a failure is to split the file by area, never to raise the limit.
"""

from __future__ import annotations

import ast
import io
import sys
import tokenize
from collections.abc import Iterable
from pathlib import Path

MAX_CODE_LINES = 900
SCOPE = ("src", "scripts", "tests")
_NOT_CODE = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
}


def _docstring_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def code_lines(source: str) -> int:
    """Lines holding code: not blank, not only a comment, not in a docstring."""
    docstrings = _docstring_lines(ast.parse(source))
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in _NOT_CODE:
            continue
        lines.update(range(token.start[0], token.end[0] + 1))
    return len(lines - docstrings)


def files(root: Path) -> Iterable[Path]:
    for top in SCOPE:
        yield from sorted((root / top).rglob("*.py"))


def oversized(root: Path, limit: int = MAX_CODE_LINES) -> list[tuple[Path, int]]:
    found = []
    for path in files(root):
        count = code_lines(path.read_text(encoding="utf-8"))
        if count > limit:
            found.append((path.relative_to(root), count))
    return found


def main() -> int:
    over = oversized(Path.cwd())
    for path, count in over:
        print(f"{path.as_posix()}: {count} code lines (limit {MAX_CODE_LINES})", file=sys.stderr)
    if over:
        print("Split the file by area; do not raise the limit.", file=sys.stderr)
        return 1
    print(f"File size OK: every file is within {MAX_CODE_LINES} code lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
