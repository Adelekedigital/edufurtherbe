"""The local gate's step selection.

``--only`` decides which steps CI runs, so a name that matches nothing must fail
loudly. Filtering to an empty set and exiting 0 would be the "green check that
never ran" failure this repository has already recorded twice.

The last test is the one that matters most: it reads the workflows and asserts
every step name they select actually exists. A typo there would otherwise skip a
gate in CI while the job still reported success.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"


def load_check() -> ModuleType:
    """Import scripts/check.py, which is a script rather than a package module."""
    spec = importlib.util.spec_from_file_location("check", PROJECT_ROOT / "scripts" / "check.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_selection_means_every_step() -> None:
    check = load_check()

    assert check.select([]) == check.STEPS


def test_selection_returns_the_named_steps_in_declared_order() -> None:
    check = load_check()

    selected = [name for name, _ in check.select(["security", "lint"])]

    assert selected == ["lint", "security"]


def test_unknown_step_name_is_rejected() -> None:
    check = load_check()

    with pytest.raises(SystemExit) as excinfo:
        check.select(["lint", "typos"])

    assert excinfo.value.code != 0


def test_the_full_gate_parallelises_tests_but_the_fast_gate_does_not() -> None:
    check = load_check()

    full_tests = next(command for name, command in check.STEPS if name == "tests")
    fast_tests = next(command for name, command in check.FAST if name == "tests-unit")

    assert full_tests[1:3] == ["-n", "4"]
    assert "--dist=worksteal" in full_tests
    assert "-n" not in fast_tests


def test_every_step_name_used_by_a_workflow_exists() -> None:
    check = load_check()
    known = {name for name, _ in check.STEPS}

    referenced: set[str] = set()
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for match in re.findall(r"--only\s+([\w,]+)", workflow.read_text(encoding="utf-8")):
            referenced.update(match.split(","))

    assert referenced, "no workflow selects any step — the gate is not wired to CI"
    assert referenced <= known, f"workflows select unknown steps: {sorted(referenced - known)}"
