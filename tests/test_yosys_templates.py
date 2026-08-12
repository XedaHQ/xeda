"""Render-level tests for the yosys script templates.

These do not need a yosys binary. They guard the `.ys` templates (used by yosys
builds without TCL support, e.g. oss-cad-suite) against TCL leaking in, and pin
the `.tcl` templates to their historical escaping.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FPGA
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys.common import process_parameters

TESTS_DIR = Path(__file__).parent.absolute()
RESOURCES_DIR = TESTS_DIR / "resources"

# constructs that are only valid in a TCL script and must never reach a .ys script
TCL_RESIDUE = {
    "yosys command prefix": re.compile(r"^\s*yosys\s+\S", re.MULTILINE),
    "puts": re.compile(r"^\s*puts\b", re.MULTILINE),
    "escaped quote": re.compile(r'\\"'),
    "escaped bracket": re.compile(r"\\\["),
    "argument expansion": re.compile(r"\{\*\}"),
    "variable reference": re.compile(r"\$\w+"),
    "set command": re.compile(r"^\s*set\s+\w+\s", re.MULTILINE),
    "procs (TCL-only alias)": re.compile(r"^\s*procs\b", re.MULTILINE),
    "exit (not a yosys command)": re.compile(r"^\s*exit\b", re.MULTILINE),
}

ATTRS: dict[str, Any] = {"keep": {"top": "true"}, "ram_style": {"mem": "block"}}


def _render(flow_cls, settings: dict[str, Any], tmp_path: Path) -> str:
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    flow = flow_cls(flow_cls.Settings(**settings), design, tmp_path)
    flow.init()  # registers the `esc` template filter and the netlist artifacts
    flow.artifacts.utilization_report = "reports/utilization.json"
    flow.artifacts.timing_report = "reports/timing.rpt"
    stem = "yosys_fpga_synth" if flow_cls is YosysFpga else "yosys_synth"
    script = flow.copy_from_template(
        f"{stem}{flow.script_ext}",
        lstrip_blocks=True,
        trim_blocks=False,
        ghdl_args=[],
        parameters=process_parameters(design.rtl.parameters),
        defines=[],
        abc_constr_file=None,
    )
    return (tmp_path / script).read_text()


def _fpga_settings(**kw: Any) -> dict[str, Any]:
    return dict(fpga=FPGA(part="xc7a12tcsg325-1"), clock_period=5.0, set_attribute=ATTRS, **kw)


def _asic_settings(**kw: Any) -> dict[str, Any]:
    return dict(clock_period=5.0, set_attribute=ATTRS, **kw)


@pytest.mark.parametrize(
    "flow_cls, settings",
    [(Yosys, _asic_settings()), (YosysFpga, _fpga_settings())],
    ids=["yosys_synth", "yosys_fpga_synth"],
)
def test_ys_template_has_no_tcl_residue(flow_cls, settings, tmp_path: Path) -> None:
    script = _render(flow_cls, settings, tmp_path)
    for name, pattern in TCL_RESIDUE.items():
        match = pattern.search(script)
        assert match is None, f"TCL residue ({name}) in .ys script: {match.group(0)!r}"


@pytest.mark.parametrize(
    "flow_cls, settings",
    [(Yosys, _asic_settings()), (YosysFpga, _fpga_settings())],
    ids=["yosys_synth", "yosys_fpga_synth"],
)
def test_ys_template_quotes_values_plainly(flow_cls, settings, tmp_path: Path) -> None:
    script = _render(flow_cls, settings, tmp_path)
    # string parameters and attributes reach yosys in plain double quotes
    assert 'chparam -set G_STR "abcd"' in script
    assert 'setattr -set keep "true" top' in script
    # non-string parameters stay bare
    assert "chparam -set G_IN_WIDTH 32" in script
    assert "chparam -set G_BITVECTOR 7'b0101001" in script
    assert "chparam -set G_ITERATIVE 1'b1" in script


def test_ys_leaves_plugin_and_flag_args_unquoted(tmp_path: Path) -> None:
    """Quoting rules differ per command in a `.ys` script.

    TCL strips quotes before yosys ever sees a word, so the `.tcl` templates can
    quote everything. Yosys' own tokenizer keeps them and leaves stripping to
    each command: its built-in frontends strip, but plugins (`ghdl`, `read_slang`,
    `read_systemverilog`) do not, and `-I<dir>` is one token parsed as a flag.
    """
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    flow = YosysFpga(YosysFpga.Settings(**_fpga_settings()), design, tmp_path)
    flow.init()
    script = (
        tmp_path
        / flow.copy_from_template(
            "read_files.ys",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=["--std=08"],
            parameters={},
            defines=[],
        )
    ).read_text()
    for bad in ['"-I', 'read_slang "', 'read_systemverilog -defer "']:
        assert bad not in script, f"{bad!r} would not have its quotes stripped by yosys"
    # ghdl source paths must be bare
    for line in script.splitlines():
        if line.startswith("ghdl "):
            assert '"' not in line, f"ghdl plugin does not strip quotes: {line!r}"


def test_tcl_template_keeps_legacy_escaping(tmp_path: Path) -> None:
    script = _render(YosysFpga, _fpga_settings(script_format="tcl"), tmp_path)
    assert 'chparam -set G_STR \\"abcd\\"' in script
    assert 'setattr -set keep \\"true\\" top' in script
    assert "chparam -set G_BITVECTOR 7'b0101001" in script


def test_script_format_selects_template_and_flag(tmp_path: Path) -> None:
    design = Design.from_toml(RESOURCES_DIR / "design0/design0.toml")
    ys = YosysFpga(YosysFpga.Settings(**_fpga_settings()), design, tmp_path)
    tcl = YosysFpga(YosysFpga.Settings(**_fpga_settings(script_format="tcl")), design, tmp_path)
    assert (ys.script_ext, ys.script_flag) == (".ys", "-s")
    assert (tcl.script_ext, tcl.script_flag) == (".tcl", "-c")


def test_stop_after_rtl_omits_synthesis(tmp_path: Path) -> None:
    """`.ys` has no `exit`, so stop_after=rtl must omit the post-RTL commands."""
    script = _render(YosysFpga, _fpga_settings(stop_after="rtl", rtl_json="rtl.json"), tmp_path)
    assert "write_json rtl.json" in script
    assert "synth_xilinx" not in script
    assert "write_netlist" not in script
    assert "opt_clean" not in script
