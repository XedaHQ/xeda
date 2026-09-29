"""open_xc7's chip database: read where the user keeps it, generated only into xeda's cache."""

import re
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flows import OpenXC7
from xeda.run_dir import RunDirectory

PART = "xc7a35tcsg324-1"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.delenv("CHIPDB_DIR", raising=False)
    monkeypatch.delenv("NEXTPNR_XILINX_PYTHON_DIR", raising=False)
    (tmp_path / "design").mkdir()
    design = Design(name="d", design_root=tmp_path / "design", rtl={"sources": [], "top": "d"})
    run_path = tmp_path / "xeda_run" / "d" / "open_xc7"
    run_path.mkdir(parents=True)

    def make(**settings):
        settings = {"fpga": {"part": PART}, **settings}
        return OpenXC7(
            settings,
            design,
            run_path,
            run_directory=RunDirectory.claimed(run_path, tmp_path / "xeda_run"),
        )

    return make


def _generated(monkeypatch):
    """Stand in for bbaexport and bbasm: write the database where asked; what was asked."""
    asked = []

    def generate(self, python_dir, output_dir):
        asked.append(Path(output_dir))
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / f"{PART}.bin").write_bytes(b"db")
        return Path(output_dir) / f"{PART}.bin"

    monkeypatch.setattr(OpenXC7, "generate_chipdb", generate)
    monkeypatch.setenv("NEXTPNR_XILINX_PYTHON_DIR", "/opt/nextpnr-xilinx/python")
    return asked


def test_nothing_is_made_in_the_design_tree(flow, tmp_path, monkeypatch):
    asked = _generated(monkeypatch)
    database = flow().chip_database()
    assert asked == [tmp_path / "xeda_run" / ".cache" / "chipdb"]
    assert database == tmp_path / "xeda_run" / ".cache" / "chipdb" / f"{PART}.bin"
    assert not (tmp_path / "design" / "chipdb").exists()


def test_a_named_database_directory_is_read_not_written(flow, tmp_path, monkeypatch):
    asked = _generated(monkeypatch)
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "other.bin").write_bytes(b"x")
    database = flow(chipdb=str(mine)).chip_database()
    assert asked == [tmp_path / "xeda_run" / ".cache" / "chipdb"]
    assert sorted(p.name for p in mine.iterdir()) == ["other.bin"]
    (mine / f"{PART}.bin").write_bytes(b"theirs")
    assert flow(chipdb=str(mine)).chip_database() == mine / f"{PART}.bin"
    assert database.parent != mine


def test_a_missing_named_file_is_an_error(flow, tmp_path, monkeypatch):
    """A `chipdb` naming a path that is neither an existing file nor an existing directory must
    fail loudly, naming that path, rather than silently falling back to the cache."""
    asked = _generated(monkeypatch)
    missing = tmp_path / "nope" / "chipdb.bin"
    with pytest.raises(FlowFatalError, match=re.escape(str(missing))):
        flow(chipdb=str(missing)).chip_database()
    assert asked == []


def test_a_missing_chipdb_dir_is_an_error(flow, tmp_path, monkeypatch):
    """Same for CHIPDB_DIR: a nonexistent directory it names must not be silently ignored."""
    asked = _generated(monkeypatch)
    missing = tmp_path / "also-nope"
    monkeypatch.setenv("CHIPDB_DIR", str(missing))
    with pytest.raises(FlowFatalError, match=re.escape(str(missing))):
        flow().chip_database()
    assert asked == []
