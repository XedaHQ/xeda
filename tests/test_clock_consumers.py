"""Regression tests for consumers of the legacy single-clock view."""

from xeda import Design
from xeda._internal.flow import FPGA
from xeda._internal.flows import Nextpnr
from xeda._internal.flows.nextpnr import NextpnrTool

from .test_nextpnr import write_nextpnr_config


def _design(root) -> Design:
    return Design(
        name="dummy",
        root=root,
        rtl={"sources": [], "top": "dummy", "clocks": [{"port": "clk_a"}]},
    )


def test_nextpnr_uses_first_unnamed_clock_as_frequency_target(tmp_path, monkeypatch):
    """The historical single-target nextpnr hint remains deterministic for multiple clocks."""
    design = _design(tmp_path)
    settings = Nextpnr.Settings(
        fpga=FPGA(family="ecp5", capacity="25k"),
        clocks={
            "clk_a": {"port": "clk_a", "period": "5 ns"},
            "clk_b": {"port": "clk_b", "period": "10 ns"},
        },
    )
    flow = Nextpnr(settings, design, tmp_path / "nextpnr")

    netlist = tmp_path / "yosys" / "netlist.json"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("{}")
    flow.inputs.netlist = netlist

    args = []
    monkeypatch.setattr(
        NextpnrTool,
        "run",
        lambda _tool, *values, env=None: (write_nextpnr_config(flow, values), args.extend(values)),
    )
    flow.prepare_inputs()
    flow.run()

    assert "--freq=200.0" in args
