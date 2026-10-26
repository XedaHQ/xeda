"""`out_of_context` makes `yosys_fpga` synthesize a block that a larger design instantiates.

The synthesis pass of the family then puts no IO buffer on the ports of the top module and no
global clock buffer on the clock. The expected flags come from `yosys -h synth_<family>` (checked
against the sources of yosys 0.63 and 0.69, and the help of 0.70):

- `synth_xilinx` inserts `IBUF`, `OBUF` (`iopadmap`) and `BUFG` (`clkbufmap`), unless told
  `-noiopad` and `-noclkbuf`.
- `synth_gowin` inserts `IBUF` and `OBUF` unless told `-noiopads`.
- `synth_lattice` (`synth_ecp5` is `synth_lattice -family ecp5`) inserts `IB` and `OB` only with
  `-iopad`, which is the default for Nexus (`lifcl`, `lfd2nx`) only. `-noiopad` ends it. ECP5
  inserts none by default, so it gets no flag.
- `synth_ice40` has no IO buffer and no clock buffer step, so there is no flag to give.

The setting means: the netlist has no IO buffer and no clock buffer that the synthesis inserted.
"""

import re

import pytest

from xeda.flow import FlowSettingsError
from xeda.flows import YosysFpga

from .test_yosys_fpga_clean_netlist import MAPPED_CELLS, XC7, _cells_of, _netlist, _synthesize
from .test_yosys_fpga_flags import RELEASES, TARGETS
from .tool_utils import require_yosys

#: The flags that `out_of_context` adds, by target of `test_yosys_fpga_flags.TARGETS`.
ADDED = {
    "xilinx": ["-noiopad", "-noclkbuf"],
    "xilinx-lut4": ["-noiopad", "-noclkbuf"],
    "ecp5": [],
    "ice40-hx": [],
    "ice40-up": [],
    "crosslink-nx": ["-noiopad"],
    "certus-nx": ["-noiopad"],
    "gw1n": ["-noiopads"],
    "gw2a": ["-noiopads"],
    "gw5a": ["-noiopads"],
}

BUFFERS = ("IBUF", "OBUF", "BUFG")


def test_every_target_has_its_expected_flags() -> None:
    assert set(ADDED) == set(TARGETS)


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("target", TARGETS)
def test_the_flags_follow_the_family_flags(target, release) -> None:
    fpga = TARGETS[target]
    plain = YosysFpga.Settings(fpga=fpga).synth_command(release)
    asked = YosysFpga.Settings(fpga=fpga, out_of_context=True).synth_command(release)
    assert asked == [*plain, *ADDED[target]]


@pytest.mark.parametrize("target", TARGETS)
def test_the_flags_come_before_the_flags_of_the_user(target) -> None:
    settings = YosysFpga.Settings(
        fpga=TARGETS[target], out_of_context=True, synth_flags=["-nodffe"]
    )
    command = settings.synth_command((0, 69))
    assert command[-1] == "-nodffe"
    assert command[-1 - len(ADDED[target]) : -1] == ADDED[target]


@pytest.mark.parametrize("target", TARGETS)
def test_the_flags_apply_in_the_pass_only_mode(target) -> None:
    fpga = TARGETS[target]
    pass_only = {"synth_pass_only": True, "read_verilog_flags": [], "systemverilog": "default"}
    plain = YosysFpga.Settings(fpga=fpga, **pass_only).synth_command((0, 69))
    asked = YosysFpga.Settings(fpga=fpga, out_of_context=True, **pass_only).synth_command((0, 69))
    assert asked == [*plain, *ADDED[target]]


def test_the_setting_is_off_by_default() -> None:
    assert YosysFpga.Settings().out_of_context is False


@pytest.mark.parametrize("target", TARGETS)
def test_the_setting_is_a_real_part_of_the_identity(target) -> None:
    fpga = TARGETS[target]
    assert (
        YosysFpga.Settings(fpga=fpga).model_dump()
        != YosysFpga.Settings(fpga=fpga, out_of_context=True).model_dump()
    )


# ------------------------------------------------------------------------------------- refusals

#: target, the flags of `synth_flags`, and the flag that the message names
REFUSED = [
    pytest.param("xilinx", ["-noiopad"], "-noiopad", id="xilinx-noiopad"),
    pytest.param("xilinx", ["-noclkbuf"], "-noclkbuf", id="xilinx-noclkbuf"),
    pytest.param("xilinx", ["-noiopad -noclkbuf"], "-noiopad", id="xilinx-both-in-one-item"),
    pytest.param("xilinx-lut4", ["-nodffe", "-noclkbuf"], "-noclkbuf", id="xilinx-among-others"),
    pytest.param("gw1n", ["-noiopads"], "-noiopads", id="gowin"),
    pytest.param("gw5a", ["-noiopads"], "-noiopads", id="gowin-5a"),
    pytest.param("ecp5", ["-iopad"], "-iopad", id="ecp5-iopad"),
    pytest.param("crosslink-nx", ["-noiopad"], "-noiopad", id="nexus-noiopad"),
    pytest.param("crosslink-nx", ["-iopad"], "-iopad", id="nexus-iopad"),
    pytest.param("certus-nx", ["-iopad"], "-iopad", id="certus-iopad"),
]


@pytest.mark.parametrize("target,flags,named", REFUSED)
def test_a_flag_that_repeats_or_contradicts_the_setting_is_refused(target, flags, named) -> None:
    settings = YosysFpga.Settings(fpga=TARGETS[target], out_of_context=True, synth_flags=flags)
    with pytest.raises(FlowSettingsError) as refused:
        YosysFpga.check_settings_supported(settings)
    text = str(refused.value)
    assert "out_of_context" in text
    assert f"`{named}`" in text
    assert "synth_flags" in text


@pytest.mark.parametrize("target,flags,named", REFUSED)
def test_the_same_flags_pass_without_the_setting(target, flags, named) -> None:
    """The setting adds nothing then, so `synth_flags` is free, as before."""
    YosysFpga.check_settings_supported(YosysFpga.Settings(fpga=TARGETS[target], synth_flags=flags))


@pytest.mark.parametrize("target", TARGETS)
def test_the_refusal_comes_with_the_pass_only_mode_too(target) -> None:
    flag = (ADDED[target] or ["-noiopad"])[0]  # a target without a flag has nothing to repeat
    settings = YosysFpga.Settings(
        fpga=TARGETS[target],
        out_of_context=True,
        synth_flags=[flag],
        synth_pass_only=True,
        read_verilog_flags=[],
        systemverilog="default",
    )
    if not ADDED[target]:
        YosysFpga.check_settings_supported(settings)  # ECP5, iCE40: the setting adds no flag
        return
    with pytest.raises(FlowSettingsError, match="out_of_context"):
        YosysFpga.check_settings_supported(settings)


def test_a_flag_that_only_looks_alike_is_no_conflict() -> None:
    """The check reads whole words: `-noiopads` is not `-noiopad`."""
    YosysFpga.check_settings_supported(
        YosysFpga.Settings(
            fpga=TARGETS["crosslink-nx"], out_of_context=True, synth_flags=["-noiopads"]
        )
    )


def test_a_flag_among_the_words_of_one_item_is_found() -> None:
    settings = YosysFpga.Settings(
        fpga=TARGETS["crosslink-nx"], out_of_context=True, synth_flags=["-nodffe -noiopad"]
    )
    with pytest.raises(FlowSettingsError, match="`-noiopad`"):
        YosysFpga.check_settings_supported(settings)


def test_xilinx_takes_iopad_without_a_conflict() -> None:
    """`synth_xilinx` accepts `-iopad` and ignores it, so it contradicts nothing."""
    YosysFpga.check_settings_supported(
        YosysFpga.Settings(fpga=TARGETS["xilinx"], out_of_context=True, synth_flags=["-iopad"])
    )


def test_the_planning_check_needs_no_device() -> None:
    """Without `fpga`, `required_settings` names what is missing; this check says nothing."""
    YosysFpga.check_settings_supported(YosysFpga.Settings(out_of_context=True))


# --------------------------------------------------------------------------- the real yosys


def _types(flow) -> set[str]:
    return {t for cells in _cells_of(_netlist(flow)).values() for t in cells}


def test_the_xilinx_netlist_has_no_io_or_clock_buffer(tmp_path) -> None:
    require_yosys()
    plain = _synthesize(tmp_path / "plain", MAPPED_CELLS, XC7)
    block = _synthesize(tmp_path / "block", MAPPED_CELLS, XC7, out_of_context=True)
    assert set(BUFFERS) <= _types(plain), "the pass should insert the buffers by default"
    assert not set(BUFFERS) & _types(block)
    assert set(BUFFERS) <= set(plain.results["cell_types"])
    assert not set(BUFFERS) & set(block.results["cell_types"])
    # the EDIF netlist lists library cells that nothing instantiates; look at the instances
    edif = (block.run_path / "netlist.edif").read_text()
    assert not re.search(r"\(cellRef (IBUF|OBUF|BUFG)\b", edif)
    assert re.search(r"\(cellRef (IBUF|OBUF|BUFG)\b", (plain.run_path / "netlist.edif").read_text())
    verilog = (block.run_path / "netlist.v").read_text()
    assert not re.search(r"^\s*(IBUF|OBUF|BUFG)\b", verilog, re.MULTILINE)
    # the logic is the same
    assert {t for t in _types(plain) if t not in BUFFERS} == _types(block)


ADDER = """\
module top (input logic clock, input logic [3:0] a, output logic [3:0] q);
  always_ff @(posedge clock) q <= q + a;
endmodule
"""


def test_the_nexus_netlist_has_no_io_buffer(tmp_path) -> None:
    require_yosys()
    nexus = {"part": "LIFCL-40-9BG400C"}
    plain = _synthesize(tmp_path / "plain", ADDER, nexus)
    block = _synthesize(tmp_path / "block", ADDER, nexus, out_of_context=True)
    assert {"IB", "OB"} <= _types(plain), "the pass should insert the buffers by default"
    assert not {"IB", "OB"} & _types(block)


def test_the_ecp5_netlist_has_none_with_or_without_the_setting(tmp_path) -> None:
    """The ECP5 pass inserts IO buffers only with `-iopad`, so the setting adds no flag."""
    require_yosys()
    ecp5 = {"part": "LFE5U-25F-6BG256C"}
    plain = _synthesize(tmp_path / "plain", ADDER, ecp5)
    block = _synthesize(tmp_path / "block", ADDER, ecp5, out_of_context=True)
    assert "TRELLIS_IO" not in _types(plain)
    assert _cells_of(_netlist(block)) == _cells_of(_netlist(plain))
    pads = _synthesize(tmp_path / "pads", ADDER, ecp5, synth_flags=["-iopad"])
    assert "TRELLIS_IO" in _types(pads)


def test_the_ice40_netlist_has_no_io_or_clock_buffer(tmp_path) -> None:
    """The iCE40 pass has no buffer step: `nextpnr` adds the `SB_IO` cells."""
    require_yosys()
    ice40 = {"part": "iCE40HX1K-TQ144"}
    plain = _synthesize(tmp_path / "plain", ADDER, ice40)
    block = _synthesize(tmp_path / "block", ADDER, ice40, out_of_context=True)
    assert not {"SB_IO", "SB_GB"} & _types(plain)
    assert _cells_of(_netlist(block)) == _cells_of(_netlist(plain))


def test_the_gowin_netlist_has_no_io_buffer(tmp_path) -> None:
    require_yosys()
    gowin = {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"}
    plain = _synthesize(tmp_path / "plain", ADDER, gowin)
    block = _synthesize(tmp_path / "block", ADDER, gowin, out_of_context=True)
    assert {"IBUF", "OBUF"} <= _types(plain), "the pass should insert the buffers by default"
    assert not {"IBUF", "OBUF"} & _types(block)
