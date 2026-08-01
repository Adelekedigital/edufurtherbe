"""Enforce architectural import boundaries.

The rule that carries most of the value:

    domain/ imports nothing from api/, infra/, or any web/DB framework.

Runs in pre-commit and in CI. Exits non-zero on violation.

CONFIGURATION
-------------
Zero-config for the standard layout (`src/<package>/{api,domain,infra,core}`).
The package name is auto-detected from `src/`, so this file is drop-in reusable
across projects without editing.

Override in pyproject.toml when your layout differs:

    [tool.check-layers]
    source = "src/myapp"
    package = "myapp"
    exempt = ["main.py", "api/deps.py"]

    [tool.check-layers.allowed]
    api = ["domain", "core", "observability"]
    domain = ["core"]
    infra = ["domain", "core", "observability"]
    core = []
    observability = ["core"]

    [tool.check-layers.forbidden-external]
    domain = ["fastapi", "sqlalchemy", "httpx"]

Do not weaken `allowed` or `forbidden-external` to make a failing check pass. If
a boundary genuinely needs to move, that is an ADR, not a config edit.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

DEFAULT_ALLOWED: dict[str, set[str]] = {
    "api": {"domain", "core", "observability"},
    "domain": {"core"},
    "infra": {"domain", "core", "observability"},
    "core": set(),
    "observability": {"core"},
}

# Third-party roots that must never appear inside the named layer.
DEFAULT_FORBIDDEN_EXTERNAL: dict[str, set[str]] = {
    "domain": {
        "fastapi",
        "starlette",
        "sqlalchemy",
        "alembic",
        "httpx",
        "requests",
        "redis",
        "boto3",
        "celery",
        "kombu",
        "pymongo",
    }
}

# Composition roots: allowed to see everything, by design.
DEFAULT_EXEMPT = ["main.py", "api/deps.py"]


class Config:
    def __init__(self, root: Path) -> None:
        data: dict[str, object] = {}
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            with pyproject.open("rb") as fh:
                data = tomllib.load(fh).get("tool", {}).get("check-layers", {})

        source = data.get("source")
        self.source: Path = root / str(source) if source else self._detect_source(root)

        package = data.get("package")
        self.package: str = str(package) if package else self.source.name

        allowed = data.get("allowed")
        self.allowed: dict[str, set[str]] = (
            {k: set(v) for k, v in allowed.items()}  # type: ignore[union-attr]
            if allowed
            else {k: set(v) for k, v in DEFAULT_ALLOWED.items()}
        )

        forbidden = data.get("forbidden-external")
        self.forbidden_external: dict[str, set[str]] = (
            {k: set(v) for k, v in forbidden.items()}  # type: ignore[union-attr]
            if forbidden
            else {k: set(v) for k, v in DEFAULT_FORBIDDEN_EXTERNAL.items()}
        )

        exempt = data.get("exempt") or DEFAULT_EXEMPT
        self.exempt: set[Path] = {self.source / rel for rel in exempt}  # type: ignore[union-attr]

    @staticmethod
    def _detect_source(root: Path) -> Path:
        """Find the single package under src/, so this script needs no editing."""
        src = root / "src"
        if src.is_dir():
            packages = [
                p for p in sorted(src.iterdir()) if p.is_dir() and (p / "__init__.py").is_file()
            ]
            if len(packages) == 1:
                return packages[0]
            if len(packages) > 1:
                print(
                    f"Multiple packages under src/: {[p.name for p in packages]}. "
                    "Set [tool.check-layers] source in pyproject.toml.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
        return root / "src" / "app"


def layer_of(path: Path, source: Path) -> str | None:
    rel = path.relative_to(source)
    return rel.parts[0] if len(rel.parts) > 1 else None


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


def main() -> int:
    root = Path.cwd()
    cfg = Config(root)

    if not cfg.source.is_dir():
        print(f"Source package not found: {cfg.source}", file=sys.stderr)
        print("Run from the repository root, or set [tool.check-layers] source.", file=sys.stderr)
        return 2

    violations: list[str] = []

    for path in sorted(cfg.source.rglob("*.py")):
        if path in cfg.exempt:
            continue
        layer = layer_of(path, cfg.source)
        if layer is None or layer not in cfg.allowed:
            continue

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path}:{exc.lineno}: could not parse ({exc.msg})")
            continue

        forbidden = cfg.forbidden_external.get(layer, set())

        for module, lineno in imported_modules(tree):
            root_pkg = module.split(".")[0]

            if root_pkg == cfg.package:
                parts = module.split(".")
                if len(parts) < 2:
                    continue
                target = parts[1]
                if target in cfg.allowed and target != layer and target not in cfg.allowed[layer]:
                    permitted = ", ".join(sorted(cfg.allowed[layer])) or "nothing"
                    violations.append(
                        f"{path}:{lineno}: '{layer}' may not import '{target}' "
                        f"(may import: {permitted})"
                    )
            elif root_pkg in forbidden:
                violations.append(
                    f"{path}:{lineno}: '{layer}' must stay framework-free; remove the "
                    f"'{root_pkg}' import and express the need as a Protocol in "
                    f"{layer}/ports.py"
                )

    if violations:
        print("Architectural boundary violations:\n", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nSee the project-structure skill. Do not weaken [tool.check-layers] to pass.",
            file=sys.stderr,
        )
        return 1

    print(f"Layer boundaries OK ({cfg.source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
