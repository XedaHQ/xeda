"""The guard against tests that write into the checkout (`conftest._nothing_is_written_into_the_checkout`).

The guard compares the entries of the directories it watches with what they were when
`conftest.py` was imported, which is before pytest imports any test module. It must see a new
entry in every place where the suite reads or runs files of the checkout: not only the top level,
`tests/` and the example designs, but also the workflows and scripts of `.github/` (a name that
starts with a dot) and the scripts of `tools/`. A test module that loads such a script writes its
bytecode there as it is imported, and nothing else tells.
"""

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
