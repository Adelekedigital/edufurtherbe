"""Every third-party package `src/` imports is a declared runtime dependency.

Railway installs with `uv sync --no-dev`, so the image holds `[project]
dependencies` and what they pull in, nothing else. A package the app imports
but only reaches transitively ships by accident: `httpx`, behind every outbound
client, arrived only through `fastapi[standard]` until #19 dropped that extra.
Removing the extra would have broken production with every local check green,
because dev dependencies still installed it here.
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


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_distributions() -> set[str]:
    """The distribution names in `[project] dependencies`, extras and bounds dropped."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = set()
    for requirement in project["dependencies"]:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
        assert match, requirement
        names.add(_canonical(match.group()))
    return names


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
    declared = declared_distributions()
    providers = importlib.metadata.packages_distributions()
    undeclared = {}
    for module in sorted(third_party_imports(SRC)):
        distributions = {_canonical(d) for d in providers.get(module, [])}
        if not distributions & declared:
            undeclared[module] = sorted(distributions) or "no installed distribution"
    assert not undeclared, (
        f"imported by src/ but not in [project] dependencies: {undeclared}. "
        "Declare each one directly, with a comment saying what uses it."
    )


#: Needed at runtime but never imported by `src/`, so the scan above cannot see
#: them. Each is loaded by a library or a command instead.
NEEDED_WITHOUT_AN_IMPORT = {
    "uvicorn": "Railway's start command",
    "asyncpg": "the driver the database URL selects",
    "python-multipart": "FastAPI parses `UploadFile` forms with it",
    "email-validator": "pydantic builds `EmailStr` with it",
    "alembic": "migrations run `uv run alembic upgrade head`",
}


def test_packages_needed_without_an_import_are_declared() -> None:
    missing = set(NEEDED_WITHOUT_AN_IMPORT) - declared_distributions()
    assert not missing, f"needed at runtime, not in [project] dependencies: {sorted(missing)}"


def test_the_scan_sees_imports_and_maps_them_to_distributions() -> None:
    """The positive case: an empty scan would pass the test above vacuously."""
    imports = third_party_imports(SRC)
    providers = importlib.metadata.packages_distributions()

    assert {"httpx", "starlette", "sentry_sdk", "cv2", "jwt"} <= imports
    assert "opencv-python-headless" in {_canonical(d) for d in providers["cv2"]}
    assert "pyjwt" in {_canonical(d) for d in providers["jwt"]}
    assert {"fastapi", "httpx", "pyjwt", "opencv-python-headless"} <= declared_distributions()
