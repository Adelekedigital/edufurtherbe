"""The file-size rule: no Python file over 900 lines of code (owner, 2026-09-29)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_file_size", PROJECT_ROOT / "scripts" / "check_file_size.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load_check()


def code(n: int) -> str:
    return "".join(f"x{i} = {i}\n" for i in range(n))


def a_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_file_one_line_over_the_limit_fails(tmp_path: Path) -> None:
    root = a_tree(tmp_path, {"src/big.py": code(check.MAX_CODE_LINES + 1)})

    assert check.oversized(root) == [(Path("src/big.py"), check.MAX_CODE_LINES + 1)]


def test_a_file_exactly_at_the_limit_passes(tmp_path: Path) -> None:
    root = a_tree(tmp_path, {"src/full.py": code(check.MAX_CODE_LINES)})

    assert check.oversized(root) == []


def test_comments_docstrings_and_blank_lines_are_free(tmp_path: Path) -> None:
    """Explaining a decision never pushes a file over."""
    doc = '"""' + "\n".join("a line of prose" for _ in range(1000)) + '"""\n'
    comments = "".join(f"# a comment {i}\n\n" for i in range(1000))
    function = 'def f() -> None:\n    """Line one.\n\n    Line two.\n    """\n    return None\n'
    root = a_tree(tmp_path, {"tests/wordy.py": doc + comments + function + code(10)})

    assert check.code_lines((root / "tests/wordy.py").read_text()) == 12
    assert check.oversized(root) == []


def test_a_string_that_is_not_a_docstring_counts() -> None:
    """Only docstrings are free; a long SQL literal is code a reader holds."""
    source = 'QUERY = """\n' + "SELECT 1\n" * 10 + '"""\n'

    assert check.code_lines(source) == 12


def test_migrations_are_exempt(tmp_path: Path) -> None:
    root = a_tree(tmp_path, {"migrations/versions/seed.py": code(check.MAX_CODE_LINES * 5)})

    assert check.oversized(root) == []


def test_scripts_and_tests_are_in_scope(tmp_path: Path) -> None:
    root = a_tree(
        tmp_path,
        {
            "scripts/big.py": code(check.MAX_CODE_LINES + 1),
            "tests/big.py": code(check.MAX_CODE_LINES + 1),
        },
    )

    assert {path.as_posix() for path, _ in check.oversized(root)} == {
        "scripts/big.py",
        "tests/big.py",
    }


def test_the_repository_is_within_the_limit() -> None:
    """The rule holds today, so a regression names its file here as well as in CI."""
    assert check.oversized(PROJECT_ROOT) == []
