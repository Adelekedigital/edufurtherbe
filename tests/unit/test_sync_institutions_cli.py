"""What the sync script decides before it touches a database.

`scripts/` is checked by ruff alone — no mypy, no bandit, nothing counted
against the coverage floor. These are the only tests that read it, which is why
the refusal below is tested here rather than trusted to a comment.

The refusal matters more than it looks. `HipolabsCatalogue.fetch` already
refuses an empty source, and this is the same rule one layer up, where the
emptiness arrives from *refusals* instead. Without it an upstream key rename
mirrors nothing, prints its refusals, exits 0, and leaves a green weekly check
over a catalogue that has quietly stopped updating.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.sync_institutions import run

from app.infra.etl.cli import EXIT_REFUSED

USABLE: dict[str, Any] = {
    "name": "University of Lagos",
    "domains": ["unilag.edu.ng"],
    "web_pages": ["https://unilag.edu.ng/"],
    "alpha_two_code": "NG",
}


def args_for(path: Path, *, dry_run: bool = True) -> argparse.Namespace:
    return argparse.Namespace(from_file=path, dry_run=dry_run)


def snapshot(tmp_path: Path, records: list[dict[str, Any]]) -> Path:
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_a_catalogue_whose_records_are_all_refused_refuses_the_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Upstream renaming `domains` to `domain` is the realistic shape change —
    the source is a third party that commits every ~2 days."""
    renamed = [{**USABLE, "domain": USABLE["domains"]} for _ in range(3)]
    for record in renamed:
        del record["domains"]

    exit_code = asyncio.run(run(args_for(snapshot(tmp_path, renamed))))

    assert exit_code == EXIT_REFUSED
    assert "refusing to sync" in capsys.readouterr().out


def test_the_refusal_beats_the_dry_run_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run is what CI runs first, and it is the step that should catch a
    shape change. Exiting 0 there means the real step runs anyway."""
    exit_code = asyncio.run(run(args_for(snapshot(tmp_path, [{"name": "No Domain Here"}]))))

    out = capsys.readouterr().out
    assert exit_code == EXIT_REFUSED
    assert "dry run" not in out, "the dry run reported success on an unusable catalogue"


def test_one_usable_record_is_enough_to_proceed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The positive case. Without it, refusing everything would pass every test
    above — and the refusal must not fire on a catalogue that merely has some
    bad records among good ones.
    """
    mixed = [USABLE, {"name": "No Domain Here"}]

    exit_code = asyncio.run(run(args_for(snapshot(tmp_path, mixed))))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "dry run — nothing written" in out
    assert "usable rows    1" in out
    # Refusals are still named, not swallowed, when the sync goes ahead.
    assert "No Domain Here: no domain" in out
