"""open_xc7's chip database: read where the user keeps it, generated only into xeda's cache."""

import os
import re
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flow_runner import DefaultRunner
from xeda.flows import OpenXC7
from xeda.run_dir import RunDirectory
from xeda.run_root import ensure_run_root

from .tool_utils import launch_until_fresh, require_yosys

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


#: nextpnr-xilinx, as far as the flow needs it: the log and the FASM file it is asked for
FAKE_NEXTPNR_XILINX = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    --log=*) echo "Info: Program finished normally." > "${arg#--log=}" ;;
    --fasm=*) : > "${arg#--fasm=}" ;;
  esac
done
"""


@pytest.mark.parametrize("where", ["CHIPDB_DIR", "cache"])
def test_a_replaced_database_makes_the_next_launch_stale(where, tmp_path, monkeypatch):
    """The database handed to nextpnr is an input of the run wherever it was found -- also
    where no setting names it (`CHIPDB_DIR`, xeda's cache) -- so replacing it runs again."""
    require_yosys()
    monkeypatch.delenv("CHIPDB_DIR", raising=False)
    monkeypatch.delenv("NEXTPNR_XILINX_PYTHON_DIR", raising=False)
    monkeypatch.delenv("PRJXRAY_DB_DIR", raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nextpnr = bin_dir / "nextpnr-xilinx"
    nextpnr.write_text(FAKE_NEXTPNR_XILINX)
    nextpnr.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    root = ensure_run_root(tmp_path / "xeda_run")
    assert root is not None
    if where == "CHIPDB_DIR":
        database = tmp_path / "chipdb" / f"{PART}.bin"
        monkeypatch.setenv("CHIPDB_DIR", str(database.parent))
    else:
        database = root / ".cache" / "chipdb" / f"{PART}.bin"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"a database")
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "inv.v").write_text(
        "module inv(input clk, input a, output reg y); always @(posedge clk) y <= ~a; endmodule\n"
    )
    design = Design(
        name="inv",
        design_root=tmp_path / "design",
        rtl={"sources": ["inv.v"], "top": "inv", "clock": {"port": "clk"}},
    )
    runner = DefaultRunner(root, display_results=False)
    settings = {"fpga": {"part": PART}, "clock": {"period": 10.0}}

    first = runner.launch_flow("open_xc7", design, settings)
    assert first.succeeded and first.settings.chipdb == database
    launch_until_fresh(runner, lambda: runner.launch_flow("open_xc7", design, settings))
    database.write_bytes(b"another database")
    again = runner.launch_flow("open_xc7", design, settings)
    assert not again.reused and again.stale_reason == f"input changed: {database.resolve()}"
