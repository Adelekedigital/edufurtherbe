"""What every loader script needs before it can load anything.

Extracted when the second loader arrived, which is the rule: a thing
demonstrated twice is a thing that gets a home. It lives in ``infra/`` rather
than in ``scripts/`` because ``scripts/`` is not a package — a cross-script
import resolves only when the repository root happens to be on ``sys.path``,
which is true under pytest and false under ``uv run python scripts/...``. That
asymmetry is exactly what produced two copies of the export timezone, and it
would have produced two copies of everything below.

Nothing here makes a decision. The exit codes are the one piece of judgement and
they are documented rather than inferred, because a runbook is read by somebody
under time pressure during a freeze.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from app.domain.bubble import EXPORT_TIMEZONE
from app.infra.clients.bubble import JsonExportSource

#: Everything the operator expected happened.
EXIT_OK = 0

#: Refused before writing anything. The snapshot has a problem the loader will
#: not guess its way past — the database is untouched and a re-run after a fix is
#: the whole recovery.
EXIT_REFUSED = 1

#: Loaded, and something needs a human. A name resolved to nothing, or a value
#: was dropped from an otherwise-good row. **Distinct from 1 on purpose:** a
#: runbook has to tell "done, now decide something" from "done nothing", and exit
#: 0 is the one signal that says nothing needs attention.
EXIT_UNRESOLVED = 2


class ReconciliationError(RuntimeError):
    """Raised inside the load transaction so the context manager rolls it back.

    **Here rather than in each loader script.** It began in
    ``load_availability.py`` and the sessions loader needed the same class, which
    is the moment a second copy gets written — two exception types with one name
    and one meaning, where ``except ReconciliationError`` in a future runner
    would catch one and miss the other. Settled decision #43, caught before the
    copy existed rather than after.

    It lives beside the exit codes because it is the same kind of thing: part of
    the contract between a loader script and whoever reads its result.
    """


def configure_streams() -> None:
    """Make the console survive the data.

    Bubble field names contain emoji and a Windows console defaults to cp1252, so
    the first ``print`` of a field name kills the run with a ``UnicodeEncodeError``
    — after the load, in the middle of reporting it.
    """
    for stream in (sys.stdout, sys.stderr):
        # Guarded rather than cast. Under pytest these are replaced by capture
        # objects with no `reconfigure`, and the same is true of any caller that
        # redirects them — so the check is behaviour, not an appeasement of the
        # type checker. It went unnoticed while this lived in `scripts/`, which
        # `mypy src` does not read.
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def open_export(directory: Path) -> JsonExportSource:
    """A reader over a Data-tab export, in the zone that export renders in.

    The zone is not a parameter. It is a property of the Bubble application, and
    a loader that let the caller choose would let two runs of the same snapshot
    disagree about what time anything happened.
    """
    return JsonExportSource(directory, timezone=EXPORT_TIMEZONE)


def report_unresolved(label: str, names: tuple[str, ...]) -> bool:
    """Name what did not resolve, and say whether anything did not.

    **By name, never by count.** The next action is always to decide an alias or
    widen a seed, and a number supports neither. On stderr, because that is the
    stream that survives a caller keeping stdout for the load summary.
    """
    if not names:
        return False
    print(f"UNRESOLVED {label}: {', '.join(names)}", file=sys.stderr)
    return True
