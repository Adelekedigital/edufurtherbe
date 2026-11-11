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


def test_the_coverage_floor_has_one_definition() -> None:
    check = load_check()

    full_tests = next(command for name, command in check.STEPS if name == "tests")
    report = next(command for name, command in check.COMBINE if name == "coverage-report")

    assert f"--cov-fail-under={check.COVERAGE_FAIL_UNDER}" in full_tests
    assert f"--fail-under={check.COVERAGE_FAIL_UNDER}" in report
    assert check.COVERAGE_FAIL_UNDER == 85
    # The workflows call check.py for it; a literal there would be a second floor.
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        assert "fail-under" not in workflow.read_text(encoding="utf-8"), workflow.name


def test_shards_cover_every_test_file_exactly_once() -> None:
    check = load_check()
    files = check.test_files()

    shards = check.split(files, check.load_durations(), 3)

    assert len(shards) == 3
    assert all(shards), "a shard with no files would run the whole suite"
    flat = [path for shard in shards for path in shard]
    assert sorted(flat) == sorted(files)
    assert len(flat) == len(set(flat))
    assert "tests/unit/test_check_script.py" in files


def test_the_split_is_deterministic_and_balanced_by_duration() -> None:
    check = load_check()
    files = ["a", "b", "c", "d", "e"]
    durations = {"a": 10.0, "b": 6.0, "c": 5.0, "d": 4.0}

    first = check.split(files, durations, 2)

    assert first == check.split(list(reversed(files)), durations, 2)
    # Longest first onto the lightest shard; "e" is unknown, so it weighs the median (5.5).
    assert first == [["a", "c"], ["b", "d", "e"]]


def test_a_shard_runs_its_files_without_the_floor() -> None:
    check = load_check()

    name, command = check.shard_step("2/3")
    files = check.split(check.test_files(), check.load_durations(), 3)[1]

    assert name == "tests-shard-2"
    assert command[: len(files) * -1] == [*check.TEST_RUN, "--cov-report="]
    assert command[-len(files) :] == files
    assert not any("fail-under" in part for part in command)


@pytest.mark.parametrize("spec", ["0/3", "4/3", "3", "a/b", "1/0"])
def test_a_malformed_shard_is_rejected(spec: str) -> None:
    check = load_check()

    with pytest.raises(SystemExit) as excinfo:
        check.shard_step(spec)

    assert excinfo.value.code != 0


def test_an_empty_shard_fails_rather_than_running_everything() -> None:
    check = load_check()

    with pytest.raises(SystemExit, match="no test files"):
        check.shard_step("3/3", files=["tests/unit/test_a.py", "tests/unit/test_b.py"])


def test_durations_are_summed_per_file_from_junit(tmp_path: Path) -> None:
    check = load_check()
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest">'
        '<testcase classname="tests.unit.test_check_script" name="t1" time="1.5"/>'
        '<testcase classname="tests.unit.test_check_script" name="t2" time="2.0"/>'
        '<testcase classname="tests.integration.test_nope.TestX" name="t3" time="9"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    durations = check.durations_from_junit([junit])

    assert durations == {"tests/unit/test_check_script.py": 3.5}
