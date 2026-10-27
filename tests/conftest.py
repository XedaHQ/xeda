"""The suite's own isolation: a test works under `tmp_path`, never in the checkout."""

import os
import shutil
import stat
from pathlib import Path
from typing import Any, Iterator

import pytest

from .sharding import parse_shard, shard_of
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


# ------------------------------------------------------------------------------ the shards


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        type=parse_shard,
        metavar="K/N",
        help="run only the K-th of N disjoint parts of the collected tests (see tests/sharding.py)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """With `--shard K/N`, deselect every test that is not in shard K of N. Runs in each
    pytest-xdist worker too, where the same node ids give the same shards."""
    shard = config.getoption("--shard")
    if shard is None:
        return
    index, count = shard
    kept, dropped = [], []
    for item in items:
        (kept if shard_of(item.nodeid, count) == index else dropped).append(item)
    items[:] = kept
    config.hook.pytest_deselected(items=dropped)


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


# ----------------------------------------------------------------- the flow registry


#: The test modules that keep the flows they define registered as they are imported, because
#: their tests launch or resolve those flows by name (a chain `a+b`, a binding, a producer found
#: by its name): module -> why. Any other module that leaves a flow registered at import fails the
#: run, and so does a name that two test modules register.
REVIEWED_IMPORT_REGISTRATIONS = {
    "tests.io_flows": "the declared-I/O flows of the chain, binding and resolver tests; the remote "
    "fixtures import it on the host for the same flows",
    "tests.test_asic_platform_settings": "an ASIC producer and consumer resolved by name",
    "tests.test_chain_resolver": "a consumer that the chain resolver finds by name",
    "tests.test_dse_run": "the flows that a design space exploration launches by name",
    "tests.test_flow_io": "a flow whose outputs a binding switches by name",
    "tests.test_fpga_chains": "the producers of FPGA chains resolved by name",
    "tests.test_fpga_io_specialization": "a consumer resolved by name",
    "tests.test_output_records": "the producers whose records the consumers find by name",
    "tests.test_prepare_inputs": "a consumer whose producer is found by name",
    "tests.test_required_model_fields": "flows launched by name",
    "tests.test_run_directory": "flows launched by name",
    "tests.test_run_root_links": "a flow launched by name",
    "tests.test_stale_reports": "flows launched by name",
    "tests.test_trace_directories": "a flow launched by name",
    "tests.test_trace_programs": "a flow launched by name",
}


def _import_registration_problems() -> list[str]:
    """What the imported test modules left in the flow registry that no review allows: a flow of
    a module not in `REVIEWED_IMPORT_REGISTRATIONS`, a name that a built-in flow or a flow of a
    reviewed module registered but that now leads to another class (two flows registered one
    name, their snake-case name, class name or alias, and the later one won), and a name of a
    built-in flow that is gone from the registry."""
    import inspect

    from xeda.flow import Flow, registered_flows

    problems = sorted(
        f"{name} ({cls.__module__})"
        for name, (_module, cls) in registered_flows.items()
        if not cls.__module__.startswith("xeda.")
        and cls.__module__ not in REVIEWED_IMPORT_REGISTRATIONS
    )

    def subclasses(cls: type) -> Iterator[type]:
        for subclass in cls.__subclasses__():
            yield subclass
            yield from subclasses(subclass)

    for cls in dict.fromkeys(subclasses(Flow)):
        if inspect.isabstract(cls) or not (
            cls.__module__.startswith("xeda.") or cls.__module__ in REVIEWED_IMPORT_REGISTRATIONS
        ):
            continue
        builtin = cls.__module__.startswith("xeda.")
        for name in (cls.name,):
            if name not in registered_flows:
                if builtin:  # a reviewed module may unregister its own flow, never a built-in
                    problems.append(f"{name}: {cls.__module__}.{cls.__qualname__} is unregistered")
                continue
            holder = registered_flows[name][1]
            if holder is not cls:
                problems.append(
                    f"{name}: {cls.__module__}.{cls.__qualname__} and "
                    f"{holder.__module__}.{holder.__qualname__}"
                )
    return problems


@pytest.fixture(scope="session", autouse=True)
def _importing_the_tests_leaves_the_flow_registry_alone():
    """No test module leaves a flow registered as it is imported, unless a review allows it
    (`REVIEWED_IMPORT_REGISTRATIONS`). Collection imports every test module in every worker, so
    such a flow is in the registry of every test of the run, and a test that checks every flow
    finds it. A module unregisters what it defines at import (`test_delivery`), or defines its
    flows in a test, which leaves nothing registered (`_flow_registry_is_put_back`)."""
    from .flow_registry import load_builtin_flows

    load_builtin_flows()
    problems = _import_registration_problems()
    if problems:
        pytest.fail(f"importing the test modules left these flows registered: {problems}")


@pytest.fixture(autouse=True)
def _flow_registry_is_put_back():
    """Every test runs under `preserved_flow_registry`: a flow class that a test defines, or a
    built-in that it removes, is gone from the registry after the test, so no later test in the
    worker sees it."""
    from .flow_registry import preserved_flow_registry

    with preserved_flow_registry():
        yield


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


@pytest.fixture(autouse=True)
def _the_script_format_parameter_forces_the_format(request, monkeypatch):
    """Xeda selects the format of a yosys script from the settings. A test with a parameter
    named `script_format` (`ys` or `tcl`) checks the template set of that format, for the same
    settings: every yosys flow writes a script of that format in the test."""
    callspec = getattr(request.node, "callspec", None)
    script_format = callspec.params.get("script_format") if callspec is not None else None
    if script_format is not None:
        from .yosys_format import force_script_format

        force_script_format(monkeypatch, script_format)


@pytest.fixture
def tool_output_on_terminal():
    """The output of tools goes to the standard streams, as it does for the command line. A test
    that reads what a tool printed uses this; a launch from Python writes it to the log only."""
    from .tool_utils import terminal_output

    with terminal_output():
        yield
