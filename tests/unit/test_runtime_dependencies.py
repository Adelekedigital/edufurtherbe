"""Every package the running app needs is a declared runtime dependency.

Railway builds with Railpack, which installs `uv sync --locked --no-dev` (its
Python provider, `core/providers/python/python.go`; evidence in PR #452). So the
image holds `[project] dependencies` and what they pull in, nothing else. A
package the app imports but only reaches transitively ships by accident:
`httpx`, behind every outbound client, arrived only through `fastapi[standard]`
until #19 dropped that extra. Removing the extra would have broken production
with every local check green, because dev dependencies still installed it here.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "app"

#: Needed at runtime but never imported by `src/` under these names, so the
#: import scan cannot see them; extras included where the app needs the extra.
#: Reasons: the comment on each line in pyproject.toml.
NEEDED_WITHOUT_AN_IMPORT = (
    "uvicorn[standard]",
    "asyncpg",
    "python-multipart",
    "email-validator",
    "tzdata",
    "sqlalchemy[asyncio]",
)

_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse(requirement: str) -> tuple[str, frozenset[str]]:
    """A requirement's canonical name and its extras; bounds and markers dropped."""
    match = _REQUIREMENT.match(requirement)
    assert match, requirement
    extras = match.group(2) or ""
    return _canonical(match.group(1)), frozenset(
        _canonical(e) for e in extras.split(",") if e.strip()
    )


def declared() -> dict[str, frozenset[str]]:
    """`[project] dependencies` as canonical name to the extras declared with it."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return dict(_parse(requirement) for requirement in project["dependencies"])


def third_party_imports(root: Path) -> set[str]:
    """Top-level names of every absolute import under `root` that is not stdlib or `app`.

    Imports inside functions and `TYPE_CHECKING` blocks count too: a deferred
    import still fails when it runs.
    """
    names: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names - set(sys.stdlib_module_names) - {"app", "__future__"}


def test_every_imported_package_is_declared_at_runtime() -> None:
    names = set(declared())
    providers = importlib.metadata.packages_distributions()
    undeclared = {}
    for module in sorted(third_party_imports(SRC)):
        distributions = {_canonical(d) for d in providers.get(module, [])}
        if not distributions & names:
            undeclared[module] = sorted(distributions) or "no installed distribution"
    assert not undeclared, (
        f"imported by src/ but not in [project] dependencies: {undeclared}. "
        "Declare each one directly, with a comment saying what uses it."
    )


def test_packages_needed_without_an_import_are_declared_with_their_extras() -> None:
    have = declared()
    missing = []
    for requirement in NEEDED_WITHOUT_AN_IMPORT:
        name, extras = _parse(requirement)
        if name not in have or not extras <= have[name]:
            missing.append(requirement)
    assert not missing, f"needed at runtime, not in [project] dependencies: {missing}"


def test_the_scan_and_the_parser_see_what_they_should() -> None:
    """The positive cases: an empty scan or a parser dropping extras passes vacuously."""
    imports = third_party_imports(SRC)
    providers = importlib.metadata.packages_distributions()

    assert {"httpx", "starlette", "sentry_sdk", "cv2", "jwt"} <= imports
    assert "opencv-python-headless" in {_canonical(d) for d in providers["cv2"]}
    assert "pyjwt" in {_canonical(d) for d in providers["jwt"]}
    assert _parse("sqlalchemy[asyncio]>=2.0.36") == ("sqlalchemy", frozenset({"asyncio"}))
    assert _parse("pyjwt>=2.14.0") == ("pyjwt", frozenset())
