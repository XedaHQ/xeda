"""The suite's own isolation: a test works under `tmp_path`, never in the checkout."""

import os
import shutil
import stat
from pathlib import Path
from typing import Any

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
#: Directories, with every directory below them, whose scripts and workflows the suite reads or
#: loads as modules. Their names start with a dot, or they are no package: nothing else would
#: notice the bytecode that loading a script there writes.
WATCHED_TREES = (".github", "tools")


def _watched() -> list[Path]:
    """The checkout's top level, `tests/`, every directory holding an example design, and
    `WATCHED_TREES` (those that exist) with each directory below them."""
    examples = {
        p.parent
        for p in (CHECKOUT / "examples").rglob("*")
        if p.suffix in (".toml", ".yaml", ".yml") and "xeda_run" not in p.parts
    }
    trees = [
        directory
        for name in WATCHED_TREES
        if (CHECKOUT / name).is_dir()
        for directory in (
            CHECKOUT / name,
            *sorted(p for p in (CHECKOUT / name).rglob("*") if p.is_dir()),
        )
    ]
    return [CHECKOUT, CHECKOUT / "tests", *sorted(examples), *trees]


def _state(path: Path) -> tuple[Any, ...] | None:
    """What the guard compares of an entry: a file by its size and modification time, a link by
    its target, a directory by being one (the entries of a watched directory are watched in their
    own right). None for an entry that vanished meanwhile."""
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            return ("link", os.readlink(path))
    except OSError:
        return None
    if stat.S_ISDIR(status.st_mode):
        return ("directory",)
    return ("file", status.st_size, status.st_mtime_ns)


def _entries(directories: list[Path]) -> dict[str, tuple[Any, ...]]:
    """The state of every entry of the watched directories, by its path in the checkout."""
    states = {}
    for directory in directories:
        try:
            paths = list(directory.iterdir())
        except FileNotFoundError:  # removed since it was listed: its entries are gone
            continue
        for path in paths:
            if path.name.startswith(".") or path == TESTS_PYCACHE:
                continue
            if (state := _state(path)) is not None:
                states[str(path.relative_to(CHECKOUT))] = state
    return states


#: What the checkout held when this file was imported, before pytest imported any test module. A
#: fixture would be too late: collection runs first, and a module that writes as it is imported
#: (the bytecode of a script it loads) would be part of the picture the fixture takes.
DIRECTORIES_AT_START = _watched()
ENTRIES_AT_START = _entries(DIRECTORIES_AT_START)


def _changes_since_the_start() -> dict[str, list[str]]:
    """The entries of the watched directories that are new, changed (rewritten, even with the
    text they had, or replaced by something else) and removed since this file was imported, by
    kind. Empty when the checkout is as it was. Rewriting a file adds no name, so a record of
    names alone would not tell."""
    now = _entries(DIRECTORIES_AT_START)
    new = set(now) - set(ENTRIES_AT_START)
    if any(_opted_in(layer) for layer in OPT_IN_LAYERS):
        new.discard(OPT_IN_WORK_DIR)
    kinds = {
        "new": new,
        "changed": {
            name
            for name in now.keys() & ENTRIES_AT_START.keys()
            if now[name] != ENTRIES_AT_START[name]
        },
        "removed": set(ENTRIES_AT_START) - set(now),
    }
    return {kind: sorted(names) for kind, names in kinds.items() if names}


@pytest.fixture(scope="session", autouse=True)
def _nothing_is_written_into_the_checkout():
    yield
    changes = _changes_since_the_start()
    assert not changes, f"tests wrote into the checkout: {changes}"


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
        changed = sorted(
            name
            for name in sorted(ENVIRONMENT_AT_START.keys() | now.keys())
            if ENVIRONMENT_AT_START.get(name) != now.get(name)
        )
        os.environ.clear()
        os.environ.update(ENVIRONMENT_AT_START)
        pytest.fail(f"importing the test modules changed these environment variables: {changed}")


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
        changed = sorted(
            name
            for name in sorted(before.keys() | after.keys())
            if before.get(name) != after.get(name)
        )
        pytest.fail(f"the test left these environment variables changed: {changed}")


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
