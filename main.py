"""Entrypoint shim, and nothing else. No logic belongs in this file.

FastAPI Cloud runs `fastapi run` with no configurable command, and that CLI
discovers its target by looking for exactly six paths relative to the working
directory: `main.py`, `app.py`, `api.py`, `app/main.py`, `app/app.py`,
`app/api.py` (`fastapi_cli/discover.py::get_default_path`). The application lives
at `src/app/main.py`, which matches none of them.

Setting the platform's Application Directory to `src` does not help: it scopes
where the project is found rather than where the run step starts, so it empties
the build — `pyproject.toml` and `uv.lock` are at the repository root — while
leaving discovery exactly where it was. Tried on 2026-08-06; the build log came
back empty and the runtime error was unchanged.

**Why this file is a re-export and must stay one.** It sits outside `src/`, so
`mypy src`, `scripts/check_layers.py` and the coverage floor all skip it — the
pre-commit hooks filter on `^src/` too. Ruff is the only gate that reads it.
Anything real written here is code no boundary check can see, in the one file
whose whole purpose is to be found by a heuristic.

**Other platforms do not need this.** Railway, a container, or a plain
`uvicorn app.main:app --app-dir src` all take an explicit command and never
consult it. One caveat if Railway is ever used without an explicit start command:
its autodetection favours a root `main.py`, and `python main.py` on this file
defines an app and exits 0 — a container that starts, stops, and reports success.
Set the start command there rather than relying on detection.
"""

from app.main import app

__all__ = ["app"]
