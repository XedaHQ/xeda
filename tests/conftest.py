"""The suite's own isolation (D21): a test works under `tmp_path`, never in the checkout."""

import os
import shutil
from pathlib import Path

import pytest

from .tool_utils import FAKE_TOOLS_DIR, _opted_in

CHECKOUT = Path(__file__).parent.parent

#: The opt-in layers (Vivado, Docker, external Bluespec repositories) work in the checkout's
#: `xeda_run/`, which a container can mount where the system temporary directory is not
#: (`tool_utils.checkout_work_dir`, `test_bsc_external`): the one entry they may add.
OPT_IN_LAYERS = ("XEDA_TESTS_VIVADO", "XEDA_TESTS_DOCKER", "XEDA_TESTS_EXTERNAL")
OPT_IN_WORK_DIR = "xeda_run"
#: The suite's own bytecode cache, written as pytest imports the test modules: the one
#: `__pycache__` it may add. Nothing xeda starts may add one elsewhere (a cocotb testbench's is
#: cached in the run directory).
TESTS_PYCACHE = CHECKOUT / "tests" / "__pycache__"


def _watched() -> list[Path]:
    """The checkout's top level, `tests/`, and every directory holding an example design."""
    examples = {
        p.parent
        for p in (CHECKOUT / "examples").rglob("*")
        if p.suffix in (".toml", ".yaml", ".yml") and "xeda_run" not in p.parts
    }
    return [CHECKOUT, CHECKOUT / "tests", *sorted(examples)]


def _entries(directories: list[Path]) -> set[str]:
    return {
        str(path.relative_to(CHECKOUT))
        for directory in directories
        for path in directory.iterdir()
        if not path.name.startswith(".") and path != TESTS_PYCACHE
    }


@pytest.fixture(scope="session", autouse=True)
def _nothing_is_written_into_the_checkout():
    directories = _watched()
    before = _entries(directories)
    yield
    new = _entries(directories) - before
    if any(_opted_in(layer) for layer in OPT_IN_LAYERS):
        new.discard(OPT_IN_WORK_DIR)
    assert not new, f"tests wrote into the checkout: {sorted(new)}"


# ------------------------------------------------------------- the environment is left as found

#: Set and removed by pytest itself around every phase of a test.
PYTEST_OWN_VARIABLES = ("PYTEST_CURRENT_TEST",)


def _environment() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in PYTEST_OWN_VARIABLES}


#: What the environment was when this file was imported, before any test module was.
ENVIRONMENT_AT_START = _environment()


@pytest.fixture(scope="session", autouse=True)
def _importing_the_tests_leaves_the_environment_alone():
    """No test module changes `os.environ` as it is imported (collection runs in every worker,
    so a module-level write is in force for every test of the run)."""
    now = _environment()
    if now != ENVIRONMENT_AT_START:
        changed = {
            name: (ENVIRONMENT_AT_START.get(name), now.get(name))
            for name in sorted(ENVIRONMENT_AT_START.keys() | now.keys())
            if ENVIRONMENT_AT_START.get(name) != now.get(name)
        }
        os.environ.clear()
        os.environ.update(ENVIRONMENT_AT_START)
        pytest.fail(f"importing the test modules changed the environment (was, now): {changed}")


@pytest.fixture(autouse=True)
def _environment_is_left_as_found():
    """A test changes `os.environ` only through `monkeypatch` (or a copy of it): a direct write
    outlives the test, and in the worker that ran it every later test sees it. That is how a
    module-level `PATH` append of the fake toolchain made `nextpnr-ecp5` resolve to the fake
    wherever the real one was not on `PATH`, and the real-tool tests then failed only in a full
    run (`test_environment_isolation.py`). Set up before, hence torn down after, every
    `monkeypatch` of the test, so what is judged is what survived all of them; the environment is
    put back, so one offender is not reported against every test after it."""
    before = _environment()
    yield
    after = _environment()
    if after != before:
        os.environ.clear()
        os.environ.update(before)
        changed = {
            name: (before.get(name), after.get(name))
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        }
        pytest.fail(f"the test left the environment changed (was, now): {changed}")


# ----------------------------------------------------------------- no test programs a device

#: Stands first on every test's `PATH` under the programmer's name: a launch that reaches
#: `openFPGALoader` without the fake toolchain in front of it starts this, never a programmer
#: installed on the machine. It says so, leaves a marker and fails.
SENTINEL = """#!/bin/sh
echo "a test started openFPGALoader without the fake: no test programs a device" >&2
: > "$(dirname "$0")/REACHED"
exit 97
"""


class ProgrammerGuard:
    """The sentinel `openFPGALoader` of this test process, and what became of it."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.sentinel = directory / "openFPGALoader"
        self.marker = directory / "REACHED"

    def reached(self) -> bool:
        """Whether the sentinel was started since the last call."""
        if not os.path.exists(self.marker):
            return False
        os.remove(self.marker)
        return True

    def check(self, path: str | None = None) -> None:
        """Fail unless the `openFPGALoader` that `path` (default: `PATH` now) selects is the
        sentinel or the fake -- in this process and whatever it starts with this `PATH`."""
        resolved = shutil.which("openFPGALoader", path=path)
        if resolved is None:
            return
        # the built-in `open`: a test may have replaced `Path.open` for its own purposes

        def content(file: object) -> bytes:
            with open(str(file), "rb") as stream:
                return stream.read()

        allowed = (content(self.sentinel), content(FAKE_TOOLS_DIR / "fake_fpga_tool.py"))
        assert (
            content(resolved) in allowed
        ), f"openFPGALoader resolves to {resolved}, which is neither the fake nor the sentinel"


@pytest.fixture(scope="session")
def _programmer_sentinel(tmp_path_factory) -> ProgrammerGuard:
    guard = ProgrammerGuard(tmp_path_factory.mktemp("no-programmer"))
    guard.sentinel.write_text(SENTINEL)
    guard.sentinel.chmod(0o755)
    return guard


@pytest.fixture(autouse=True)
def programmer_guard(
    _environment_is_left_as_found: None, _programmer_sentinel: ProgrammerGuard, monkeypatch
):
    """Every test, whether or not it remembers the fake: the sentinel is first on `PATH` before
    any of the test's own fixtures (the fake toolchain goes in front of it), a child process or
    remote worker given this `PATH` inherits it, and the test fails if the sentinel was
    started or if its final `PATH` selects any other `openFPGALoader`."""
    guard = _programmer_sentinel
    monkeypatch.setenv("PATH", str(guard.directory) + os.pathsep + os.environ.get("PATH", ""))
    yield guard
    guard.check()
    assert not guard.reached(), "the test started openFPGALoader without the fake toolchain"
