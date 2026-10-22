"""A Bluespec design on yosys, with real bsc and yosys.

The tests copy the Bluespec example into `tmp_path`, edit its sources there, and judge each launch
and each dry run: what runs, what stays up to date, and what the dry run said before.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from xeda import Design
from xeda.flow_runner import DefaultRunner

from .launch_utils import empty_project
from .tool_utils import _require_command, require_bsc, require_nextpnr_ecp5, require_yosys

GCD = Path(__file__).parent.parent / "examples" / "bluespec" / "gcd"

EXTRA_VERILOG = "module spare(input a, output y); assign y = ~a; endmodule\n"
CONSTRAINTS = "# no constraints\n"


class Gcd:
    """The Bluespec example in a directory of the test, with a Verilog and a constraint source
    beside the Bluespec sources, and the flow `yosys` set up to read it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "gcd"
        shutil.copytree(GCD, self.root)
        (self.root / "rtl" / "spare.v").write_text(EXTRA_VERILOG)
        (self.root / "rtl" / "pins.xdc").write_text(CONSTRAINTS)
        document: dict[str, Any] = yaml.safe_load((self.root / "gcd.yaml").read_text())
        document["rtl"]["sources"] += ["rtl/spare.v", "rtl/pins.xdc"]
        document["flows"] = {"yosys": {"clock": {"period": "2 ns"}}}
        self.file = self.root / "gcd.yaml"
        self.file.write_text(yaml.safe_dump(document, sort_keys=False))
        self.runner = DefaultRunner(tmp_path / "run", display_results=False)

    def edit(self, source: str, old: str, new: str) -> None:
        path = self.root / source
        text = path.read_text()
        assert old in text
        path.write_text(text.replace(old, new, 1))

    def judged(self) -> dict[str, str]:
        """What a dry run says about each node, as `state` or `state: reason`."""
        _plan, states = self.runner.plan_with_freshness("yosys", Design.from_file(self.file))
        return {
            name: state.state + (f": {state.reason}" if state.state == "unknown" else "")
            for name, state in states.items()
        }

    def launch(self) -> dict[str, bool]:
        """Launch `yosys`; return, by flow, whether the run was reused."""
        self.runner = DefaultRunner(self.runner.run_root, display_results=False)
        flow = self.runner.run_flow("yosys", Design.from_file(self.file))
        assert flow is not None and flow.succeeded
        return {f.name: f.reused for f in self.runner.launched}


def test_bsc_converts_the_example_for_yosys_and_a_second_launch_runs_nothing(tmp_path) -> None:
    require_bsc()
    require_yosys()
    gcd = Gcd(tmp_path)
    assert gcd.judged()["bsc"] == "stale" and gcd.judged()["yosys"].startswith("unknown")
    assert gcd.launch() == {"bsc": False, "yosys": False}
    assert gcd.judged() == {"bsc": "fresh", "yosys": "fresh"}
    assert gcd.launch() == {"bsc": True, "yosys": True}


def test_what_an_edit_reruns_is_what_it_changes(tmp_path) -> None:
    require_bsc()
    require_yosys()
    gcd = Gcd(tmp_path)
    gcd.launch()

    # A comment in a Bluespec source: bsc runs again, writes the same Verilog, and yosys, which
    # reads that Verilog, stays up to date. The dry run knew yosys depended on bsc.
    gcd.edit("rtl/GcdUnit.bsv", "// GcdUnit", "// A changed GcdUnit")
    assert gcd.judged() == {"bsc": "stale", "yosys": "unknown: depends on bsc"}
    assert gcd.launch() == {"bsc": False, "yosys": True}
    assert gcd.judged() == {"bsc": "fresh", "yosys": "fresh"}

    # An edit that changes the Verilog: yosys runs again.
    gcd.edit("rtl/GcdEngine.bsv", "mkReg(False)", "mkReg(True)")
    assert gcd.judged() == {"bsc": "stale", "yosys": "unknown: depends on bsc"}
    assert gcd.launch() == {"bsc": False, "yosys": False}
    assert gcd.judged() == {"bsc": "fresh", "yosys": "fresh"}

    # A Verilog source that passes through bsc: bsc hashes all of `rtl`, so it runs again, and
    # yosys reads the file itself, so it runs again too.
    (gcd.root / "rtl" / "spare.v").write_text(EXTRA_VERILOG + "// edited\n")
    assert gcd.judged() == {"bsc": "stale", "yosys": "unknown: depends on bsc"}
    assert gcd.launch() == {"bsc": False, "yosys": False}

    # A constraint file passes through bsc too. yosys is judged on every source of `rtl`, a
    # constraint file included, as it is for a design of Verilog sources.
    (gcd.root / "rtl" / "pins.xdc").write_text(CONSTRAINTS + "# edited\n")
    assert gcd.judged() == {"bsc": "stale", "yosys": "unknown: depends on bsc"}
    assert gcd.launch() == {"bsc": False, "yosys": False}
    assert gcd.judged() == {"bsc": "fresh", "yosys": "fresh"}


def test_a_small_design_with_a_verilog_module_beside_the_bluespec_runs_on_yosys(tmp_path) -> None:
    from .test_derived_design import _design

    require_bsc()
    require_yosys()
    design = _design(tmp_path / "d", vhdl=False)
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow("yosys", design, {"clock": {"period": "5 ns"}})
    assert flow is not None and flow.succeeded
    assert [f.name for f in runner.launched] == ["bsc", "yosys"]
    assert [f.reused for f in runner.launched] == [False, False]
    again = DefaultRunner(tmp_path / "run", display_results=False)
    flow = again.run_flow("yosys", design, {"clock": {"period": "5 ns"}})
    assert flow is not None and [f.reused for f in again.launched] == [True, True]


def test_outputs_to_delivers_what_yosys_wrote_and_nothing_of_its_converter(tmp_path) -> None:
    require_bsc()
    require_yosys()
    gcd = Gcd(tmp_path)
    out = tmp_path / "out"
    runner = DefaultRunner(tmp_path / "run", display_results=False, outputs_to=out)
    flow = runner.run_flow("yosys", Design.from_file(gcd.file))
    assert flow is not None and flow.succeeded
    delivered = sorted(p.name for p in out.rglob("*") if p.is_file())
    assert "netlist.v" in delivered
    assert not {"mkGcdUnit.v", "bsv_defines.v"} & set(delivered)


# --------------------------------------------------------- the FPGA targets of the design file


@pytest.mark.real_tool
def test_gcd_goes_to_ecp5_through_bsc_yosys_fpga_and_nextpnr(tmp_path) -> None:
    """`yosys_fpga+nextpnr --target ecp5` on the design file: `bsc` converts the design, and the
    part and the clock of `yosys_fpga` reach `nextpnr`, which places and routes the design without
    pins."""
    require_bsc()
    require_nextpnr_ecp5()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run(
        "yosys_fpga+nextpnr", GCD / "gcd.yaml", empty_project(tmp_path), target="ecp5"
    )
    assert flow is not None and flow.succeeded, flow and flow.results.get("error")
    assert [f.name for f in runner.launched] == ["bsc", "yosys_fpga", "nextpnr"]
    assert flow.name == "nextpnr"
    assert flow.settings.fpga.part == "LFE5U-85F-6BG381C"
    assert flow.results["lut"] > 0 and flow.results["ff"] > 0 and flow.results["fmax"] > 0
    for name in ("bsc", "yosys_fpga", "nextpnr"):
        assert (tmp_path / "run" / "gcd" / "ecp5" / name / "results.json").is_file(), name


@pytest.mark.real_tool
def test_gcd_on_ecp5_runs_nextpnr_alone_on_the_part_of_the_design_file(tmp_path) -> None:
    """A bare `nextpnr --target ecp5` plans `bsc` and `yosys_fpga` before it, and the `fpga.part`
    that the design file gives `yosys_fpga` reaches `nextpnr` along the edge."""
    require_bsc()
    require_nextpnr_ecp5()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run("nextpnr", GCD / "gcd.yaml", empty_project(tmp_path), target="ecp5")
    assert flow is not None and flow.succeeded, flow and flow.results.get("error")
    assert [f.name for f in runner.launched] == ["bsc", "yosys_fpga", "nextpnr"]
    assert flow.settings.fpga.part == "LFE5U-85F-6BG381C"


@pytest.mark.real_tool
def test_gcd_on_artix7_is_a_synthesis_estimate_of_bsc_and_yosys_fpga(tmp_path) -> None:
    """`yosys_fpga --target artix7` synthesizes the converted design for the 7-series part of the
    design file."""
    require_bsc()
    require_yosys()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run("yosys_fpga", GCD / "gcd.yaml", empty_project(tmp_path), target="artix7")
    assert flow is not None and flow.succeeded, flow and flow.results.get("error")
    assert [f.name for f in runner.launched] == ["bsc", "yosys_fpga"]
    assert flow.settings.fpga.part == "xc7a100tcsg324-1"
    assert flow.results["lut"] > 0 and flow.results["ff"] > 0


@pytest.mark.real_tool
def test_gcd_on_ecp5_packs_a_bitstream_through_bsc_yosys_fpga_nextpnr_and_fpga_pack(
    tmp_path,
) -> None:
    """`yosys_fpga+nextpnr+fpga_pack --target ecp5` ends at the bitstream of `ecppack`. No
    programmer runs: the chain stops at `fpga_pack`."""
    require_bsc()
    require_nextpnr_ecp5()
    _require_command("ecppack", ["ecppack", "--help"])
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run(
        "yosys_fpga+nextpnr+fpga_pack", GCD / "gcd.yaml", empty_project(tmp_path), target="ecp5"
    )
    assert flow is not None and flow.succeeded, flow and flow.results.get("error")
    assert [f.name for f in runner.launched] == ["bsc", "yosys_fpga", "nextpnr", "fpga_pack"]
    assert flow.outputs.bitstream == flow.run_path / "outputs" / "gcd.bit"
    assert flow.outputs.bitstream.stat().st_size > 1000
    assert flow.inputs.config == tmp_path / "run" / "gcd" / "ecp5" / "nextpnr" / "config.txt"


# ------------------------------------------------------- a generator that writes Bluespec

GENERATOR = """\
import sys
from pathlib import Path

width = int(Path("spec.txt").read_text())
Path("rtl").mkdir(exist_ok=True)
Path("rtl/Counter.bsv").write_text(f\"\"\"package Counter;

// Written by gen.py.
(* synthesize *)
module mkCounter(Reg#(UInt#({width})));
   Reg#(UInt#({width})) count <- mkReg(0);
   rule tick;
      count <= count + 1;
   endrule
   return count;
endmodule

endpackage
\"\"\")
"""


@pytest.mark.real_tool
def test_a_generator_writes_the_bluespec_that_bsc_converts(tmp_path) -> None:
    """`rtl.generator` and the conversion compose: the generator writes the Bluespec source while
    the design loads, `bsc` converts it, and `yosys` reads the Verilog. A second launch runs
    nothing. A change to what the generator reads makes it write another source, and the flows
    after it run again."""
    require_bsc()
    require_yosys()
    root = tmp_path / "design"
    root.mkdir()
    (root / "gen.py").write_text(GENERATOR)
    (root / "spec.txt").write_text("8\n")
    file = root / "counter.yaml"
    file.write_text(
        yaml.safe_dump(
            {
                "name": "counter",
                "rtl": {
                    "sources": ["rtl/Counter.bsv"],
                    "top": "mkCounter",
                    "clock": {"port": "CLK"},
                    "generator": {
                        "executable": sys.executable,
                        "args": ["gen.py"],
                        "sources": ["gen.py", "spec.txt"],
                    },
                },
                "flows": {"yosys": {"clock": {"period": "5 ns"}}},
            },
            sort_keys=False,
        )
    )

    def launch() -> tuple[dict[str, bool], Any]:
        runner = DefaultRunner(tmp_path / "run", display_results=False)
        flow = runner.run("yosys", file, empty_project(tmp_path))
        assert flow is not None and flow.succeeded, flow and flow.results.get("error")
        return {f.name: f.reused for f in runner.launched}, flow

    first, _ = launch()
    assert first == {"bsc": False, "yosys": False}
    assert "UInt#(8)" in (root / "rtl" / "Counter.bsv").read_text()
    verilog = tmp_path / "run" / "counter" / "bsc"
    assert any("mkCounter" in path.name for path in verilog.rglob("*.v"))
    assert launch()[0] == {"bsc": True, "yosys": True}
    (root / "spec.txt").write_text("12\n")
    again, _ = launch()
    assert again == {"bsc": False, "yosys": False}
    assert "UInt#(12)" in (root / "rtl" / "Counter.bsv").read_text()
