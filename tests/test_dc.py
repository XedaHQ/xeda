"""Tests for the Synopsys Design Compiler (`dc`) flow."""

import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda.flows import Dc
from xeda.utils import WorkingDirectory

from .tool_utils import fake_calls, use_fake_tools


def _dc_design(root: Path) -> Design:
    """A minimal single-file Verilog design."""
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.v").write_text("module top(input a, output y); assign y = ~a; endmodule\n")
    return Design(
        name="dcdesign",
        design_root=root,
        rtl={"sources": ["rtl/top.v"], "top": "top", "clock": {"port": "clk"}},
    )


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh is needed to run the TCL script")
@pytest.mark.parametrize("sdf_version", [None, "3.0"])
def test_dc_records_the_outputs_its_script_writes(sdf_version, tmp_path, monkeypatch) -> None:
    """`dc_script.tcl` writes the mapped netlist (Verilog and VHDL), the `.ddc` checkpoint, SDF
    and SDC on every run, and `Dc` declared none of them. The SDF was not even written: with
    `sdf_version` unset, the script asked for `write_sdf -version None`."""
    use_fake_tools(monkeypatch)
    lib = tmp_path / "pdk" / "cells.db"
    lib.parent.mkdir()
    lib.write_text("")
    settings = Dc.Settings.from_input(
        {"target_libraries": [str(lib)], "clock": {"period": 5.0}, "sdf_version": sdf_version}
    )
    run_path = tmp_path / "run"
    run_path.mkdir()
    flow = Dc(settings, _dc_design(tmp_path / "design"), run_path)
    flow.init()
    with WorkingDirectory(run_path):
        flow.run()  # the fake `dc_shell` runs the script, its commands recorded

    written = {
        label: str(flow.settings.outputs_dir / f"top.mapped.{suffix}")
        for label, suffix in (
            ("netlist", "v"),
            ("netlist_vhdl", "vhd"),
            ("checkpoint", "ddc"),
            ("sdf", "sdf"),
            ("sdc", "sdc"),
        )
    }
    version = ["-version", sdf_version] if sdf_version else []
    calls = fake_calls(run_path)
    for call in (
        ["write", "-hierarchy", "-format", "verilog", "-output", written["netlist"]],
        ["write", "-hierarchy", "-format", "vhdl", "-output", written["netlist_vhdl"]],
        ["write", "-hierarchy", "-format", "ddc", "-compress", "gzip", "-output"]
        + [written["checkpoint"]],
        ["write_sdf", *version, written["sdf"]],
        ["write_sdc", "-nosplit", written["sdc"]],
    ):
        assert call in calls
    assert {label: str(path) for label, path in flow.artifacts.items()} == written
