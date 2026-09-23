"""Regression tests for consumers of the legacy single-clock view."""

from xeda import Design
from xeda.flow import FPGA
from xeda.flows import Nextpnr, YosysFpga
from xeda.flows.nextpnr import NextpnrTool


def _design() -> Design:
    return Design(
        name="dummy",
        rtl={"sources": [], "top": "dummy", "clock": {"port": "clk_a"}},
    )


def test_nextpnr_uses_first_unnamed_clock_as_frequency_target(tmp_path, monkeypatch):
    """The historical single-target nextpnr hint remains deterministic for multiple clocks."""
    design = _design()
    settings = Nextpnr.Settings(
        fpga=FPGA(family="ecp5"),
        clocks={
            "clk_a": {"port": "clk_a", "period": 5.0},
            "clk_b": {"port": "clk_b", "period": 10.0},
        },
    )
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")

    dependency = YosysFpga(YosysFpga.Settings(fpga=FPGA(family="ecp5")), design, tmp_path / "yosys")
    dependency.run_path.mkdir(parents=True)
    (dependency.run_path / "netlist.json").write_text("{}")
    flow.completed_dependencies = [dependency]

    args = []
    monkeypatch.setattr(NextpnrTool, "run", lambda _tool, *values: args.extend(values))
    flow.run()

    assert "--freq=200.0" in args


def test_yosys_abc9_hint_uses_first_unnamed_clock(tmp_path):
    """The abc9 delay hint must not disappear solely because a design has auxiliary clocks."""
    design = _design()
    settings = YosysFpga.Settings(
        fpga=FPGA(family="ecp5"),
        clocks={
            "clk_a": {"port": "clk_a", "period": 5.0},
            "clk_b": {"port": "clk_b", "period": 10.0},
        },
    )
    flow = YosysFpga(settings, design, tmp_path)
    flow.add_template_helpers()
    flow.artifacts.update(
        utilization_report="utilization.json",
        timing_report="timing.rpt",
        netlist_json=None,
        netlist_verilog=None,
    )
    rendered = flow.jinja_env.get_template("yosys_fpga_synth.tcl").render(
        settings=settings,
        design=design,
        artifacts=flow.artifacts,
        ghdl_args=[],
        parameters={},
        defines=[],
        abc_constr_file=None,
    )

    assert "abc9.D 3333.333333333334" in rendered
