"""The guard against tests that write into the checkout (`conftest._nothing_is_written_into_the_checkout`).

The guard compares the entries of the directories it watches with what they were when
`conftest.py` was imported, which is before pytest imports any test module. It must see a new
entry in every place where the suite reads or runs files of the checkout: not only the top level,
`tests/` and the example designs, but also the workflows and scripts of `.github/` (a name that
starts with a dot) and the scripts of `tools/`. A test module that loads such a script writes its
bytecode there as it is imported, and nothing else tells.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from . import conftest as guard


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A checkout of the shape the guard knows, in `tmp_path`, as it was when `conftest.py` was
    imported."""
    for directory in (
        "tests",
        "examples/design",
        ".github/workflows",
        ".github/scripts",
        "tools",
    ):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "examples" / "design" / "design.yaml").write_text("name: design\n")
    monkeypatch.setattr(guard, "CHECKOUT", tmp_path)
    monkeypatch.setattr(guard, "TESTS_PYCACHE", tmp_path / "tests" / "__pycache__")
    directories = guard._watched()
    monkeypatch.setattr(guard, "DIRECTORIES_AT_START", directories)
    monkeypatch.setattr(guard, "ENTRIES_AT_START", guard._entries(directories))
    return tmp_path


def write(checkout: Path, name: str) -> None:
    path = checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


@pytest.mark.parametrize(
    "made",
    [
        "new.txt",
        "tests/new.txt",
        "examples/design/new.txt",
        ".github/new.txt",
        ".github/workflows/new.yml",
        ".github/scripts/__pycache__/script.cpython-311.pyc",
        ".github/other/script.py",
        "tools/new.py",
        "tools/__pycache__/tool.cpython-311.pyc",
    ],
)
def test_a_new_file_where_the_suite_reads_the_checkout_is_seen(checkout, made):
    write(checkout, made)
    assert guard._entries_added_since_the_start()


def test_a_file_that_was_there_at_the_start_is_not_new(tmp_path, monkeypatch):
    """The bytecode that an earlier run left in `.github/scripts/` is no write of this run."""
    (tmp_path / ".github" / "scripts" / "__pycache__").mkdir(parents=True)
    (tmp_path / ".github" / "scripts" / "__pycache__" / "script.cpython-311.pyc").write_text("")
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(guard, "CHECKOUT", tmp_path)
    directories = guard._watched()
    monkeypatch.setattr(guard, "DIRECTORIES_AT_START", directories)
    monkeypatch.setattr(guard, "ENTRIES_AT_START", guard._entries(directories))
    assert not guard._entries_added_since_the_start()


def test_the_bytecode_of_the_test_modules_is_the_one_entry_that_is_allowed(checkout):
    write(checkout, "tests/__pycache__/test_x.cpython-311.pyc")
    assert not guard._entries_added_since_the_start()


@pytest.mark.parametrize("opted_in", [False, True])
def test_the_work_directory_of_the_opt_in_layers_is_allowed_only_for_them(
    checkout, monkeypatch, opted_in
):
    monkeypatch.delenv("XEDA_TESTS_VIVADO", raising=False)
    monkeypatch.delenv("XEDA_TESTS_DOCKER", raising=False)
    monkeypatch.delenv("XEDA_TESTS_EXTERNAL", raising=False)
    if opted_in:
        monkeypatch.setenv("XEDA_TESTS_DOCKER", "1")
    write(checkout, f"{guard.OPT_IN_WORK_DIR}/run/file")
    assert bool(guard._entries_added_since_the_start()) is not opted_in


def test_a_directory_that_a_checkout_lacks_is_no_error(tmp_path, monkeypatch):
    """An older branch has no `tools/`, and a branch may lack `.github/scripts/`."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "examples").mkdir()
    monkeypatch.setattr(guard, "CHECKOUT", tmp_path)
    assert guard._entries(guard._watched()) == {"examples", "tests"}


# ------------------------------------------------------- the guard, in a session of its own
#
# The tests above call the guard's functions. Whether the guard runs them at the right time is a
# matter of the session: pytest imports `conftest.py`, then the test modules, then runs the
# session fixtures. Each case below is a pytest session on a small tree in `tmp_path`, with the
# conftest of the suite, a stub of its helper module, and one test module.

STUB_TOOL_UTILS = """
from pathlib import Path

FAKE_TOOLS_DIR = Path(__file__).parent / "fake_tools"


def _opted_in(variable):
    return False
"""

#: The guard as it was: the same functions, but the snapshot is taken by the session fixture,
#: after the test modules are imported.
LATE_SNAPSHOT_CONFTEST = """
import pytest

from .the_suites_conftest import _entries, _watched


@pytest.fixture(scope="session", autouse=True)
def _nothing_is_written_into_the_checkout():
    directories = _watched()
    before = _entries(directories)
    yield
    new = _entries(directories) - before
    assert not new, f"tests wrote into the checkout: {sorted(new)}"
"""

WRITES_AS_IT_IS_IMPORTED = """
from pathlib import Path

(Path(__file__).parent.parent / ".github" / "scripts" / "script.cpython-311.pyc").write_bytes(b"")


def test_nothing_else_happens():
    pass
"""

WRITES_IN_A_TEST = """
from pathlib import Path


def test_a_test_writes():
    (Path(__file__).parent.parent / "tools" / "new.py").write_text("")
"""

WRITES_NOTHING = """
def test_nothing_happens():
    pass
"""


def pytest_session(tmp_path: Path, conftest: str, module: str) -> subprocess.CompletedProcess:
    """Run pytest on a checkout whose `tests/` hold `conftest` and `module`."""
    tree = tmp_path / "checkout"
    for directory in (
        "tests/fake_tools",
        ".github/scripts",
        ".github/workflows",
        "tools",
        "examples",
    ):
        (tree / directory).mkdir(parents=True)
    (tree / "pytest.ini").write_text("[pytest]\n")
    (tree / "tests" / "__init__.py").write_text("")
    (tree / "tests" / "tool_utils.py").write_text(STUB_TOOL_UTILS)
    (tree / "tests" / "fake_tools" / "fake_fpga_tool.py").write_text("")
    (tree / "tests" / "the_suites_conftest.py").write_text(Path(guard.__file__).read_text())
    (tree / "tests" / "conftest.py").write_text(conftest)
    (tree / "tests" / "test_module.py").write_text(module)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests"],
        cwd=tree,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


SUITES_CONFTEST = Path(guard.__file__).read_text()


@pytest.mark.parametrize(
    "conftest, module, reported",
    [
        pytest.param(
            SUITES_CONFTEST,
            WRITES_AS_IT_IS_IMPORTED,
            ".github/scripts/script.cpython-311.pyc",
            id="a module writes as it is imported",
        ),
        pytest.param(SUITES_CONFTEST, WRITES_IN_A_TEST, "tools/new.py", id="a test writes"),
        pytest.param(SUITES_CONFTEST, WRITES_NOTHING, None, id="nothing is written"),
        pytest.param(
            LATE_SNAPSHOT_CONFTEST,
            WRITES_AS_IT_IS_IMPORTED,
            None,
            id="a snapshot taken by the fixture misses a write at import",
        ),
        pytest.param(
            LATE_SNAPSHOT_CONFTEST,
            WRITES_IN_A_TEST,
            "tools/new.py",
            id="a snapshot taken by the fixture sees a write by a test",
        ),
    ],
)
def test_the_session_reports_what_was_written_into_the_checkout(
    tmp_path, conftest, module, reported
):
    """The snapshot is taken when `conftest.py` is imported. The case that takes it in the fixture
    shows what that is for: it passes a run whose module wrote as it was imported."""
    outcome = pytest_session(tmp_path, conftest, module)
    report = outcome.stdout + outcome.stderr
    if reported is None:
        assert outcome.returncode == 0, report
        assert "wrote into the checkout" not in report
    else:
        assert outcome.returncode != 0, report
        assert "tests wrote into the checkout" in report and reported in report, report
