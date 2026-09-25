"""Render-level tests for the yosys script templates.

These do not need a yosys binary. They guard the `.ys` templates (used by yosys
builds without TCL support, e.g. oss-cad-suite) against TCL leaking in, and pin
the `.tcl` templates to their historical escaping.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FPGA
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys.common import process_parameters
from xeda.flow import FlowException

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


@pytest.mark.parametrize(
    "fpga,flags,absent",
    [
        (
            {"family": "ecp5", "vendor": "lattice", "capacity": "25k"},
            [],
            ["-abc9", "-nowidelut", "-flatten", "-retime"],
        ),
        (
            {"family": "ice40", "vendor": "lattice", "device": "ice40UP5K"},
            ["-device u"],
            ["-abc9", "-nowidelut", "-flatten"],
        ),
        (
            {"family": "artix-7", "vendor": "xilinx", "part": "xc7a12tcsg325-1"},
            [],
            ["-nowidelut", "-abc9"],
        ),
    ],
)
def test_fpga_defaults_use_valid_target_flags(fpga, flags, absent):
    settings = YosysFpga.Settings(fpga=fpga)
    actual = settings.device_synth_flags()
    assert all(flag in actual for flag in flags)
    assert all(flag not in actual for flag in absent)


def test_abc9_false_has_a_real_effect_or_fails():
    ice40 = YosysFpga.Settings(fpga={"family": "ice40", "device": "ice40HX1K"}, abc9=False)
    assert "-noabc" in ice40.device_synth_flags()
    ecp5 = YosysFpga.Settings(fpga={"family": "ecp5", "capacity": "25k"}, abc9=False)
    with pytest.raises(FlowException, match="always uses ABC9"):
        ecp5.device_synth_flags()


def test_ice40_ultraplus_resources_are_opt_in():
    fpga = {"family": "ice40", "device": "ice40UP5K"}
    default = YosysFpga.Settings(fpga=fpga).device_synth_flags()
    tuned = YosysFpga.Settings(fpga=fpga, ice40_dsp=True, ice40_spram=True).device_synth_flags()
    assert "-dsp" not in default and "-spram" not in default
    assert "-dsp" in tuned and "-spram" in tuned


def test_ice40_ultraplus_resources_reject_other_devices():
    settings = YosysFpga.Settings(fpga={"family": "ice40", "device": "ice40HX1K"}, ice40_dsp=True)
    with pytest.raises(FlowException, match="require an UltraPlus"):
        settings.device_synth_flags()


@pytest.mark.parametrize(
    "fpga,command,family_flags",
    [
        (
            {"family": "gw2a", "vendor": "gowin", "device": "GW2A-18"},
            "synth_gowin",
            ["-family", "gw2a"],
        ),
        (
            {"family": "nexus", "vendor": "lattice", "device": "LFD2NX-40"},
            "synth_lattice",
            ["-family", "lfd2nx"],
        ),
        ({"family": "nexus", "vendor": "lattice", "device": "LIFCL-40"}, "synth_nexus", []),
        (
            {"family": "kintex-us", "vendor": "xilinx", "device": "xcku035"},
            "synth_xilinx",
            ["-family", "xcu"],
        ),
        (
            {"family": "virtex-usp", "vendor": "xilinx", "device": "xcvu3p"},
            "synth_xilinx",
            ["-family", "xcup"],
        ),
    ],
)
def test_fpga_synthesis_selects_the_real_device_pass(fpga, command, family_flags):
    settings = YosysFpga.Settings(fpga=fpga)
    assert settings.synth_command() == command
    assert settings.synth_family_flags() == family_flags


# yosys commands that expand a file name as a glob pattern, so their file arguments go through
# `read_path` (`frontend_name`); `test_yosys.py::test_yosys_reads_every_input_by_its_own_name`
# pins the list against a real yosys.
EXPANDING_COMMANDS = re.compile(r"\b(read_verilog|read_liberty|techmap -map|dfflibmap)\b")
TEMPLATES_DIR = Path(__file__).parent.parent / "src" / "xeda" / "flows" / "yosys" / "templates"


def test_a_file_yosys_expands_the_name_of_is_named_by_read_path() -> None:
    """Every path a template hands a command that expands it as a pattern goes through
    `read_path`, and no other path does: escaped, a name finds no file where it is taken as is."""
    for template in sorted(TEMPLATES_DIR.glob("*")):
        if template.suffix not in (".ys", ".tcl"):
            continue
        for n, line in enumerate(template.read_text().splitlines(), 1):
            where = f"{template.name}:{n}: {line.strip()}"
            if EXPANDING_COMMANDS.search(line):
                assert "|path}}" not in line, where
            elif "|read_path}}" in line:
                raise AssertionError(where)


@pytest.mark.parametrize("script_format", ["ys", "tcl"])
def test_a_source_name_with_pattern_characters_reaches_yosys_escaped(
    script_format, tmp_path: Path
) -> None:
    """`top[1].v` is a file name, and yosys' `read_verilog` expands it as a pattern: the script
    names it `top[[]1].v`, which matches only itself -- in a `.tcl` script as the word TCL hands
    yosys."""
    root = tmp_path / "d"
    root.mkdir()
    (root / "top[1].v").write_text("module top; endmodule\n")
    design = Design(name="d", design_root=root, rtl={"sources": ["top[1].v"], "top": "top"})
    flow = Yosys(Yosys.Settings(script_format=script_format), design, tmp_path)
    flow.init()
    script = (
        tmp_path
        / flow.copy_from_template(
            f"read_files{flow.script_ext}",
            lstrip_blocks=True,
            trim_blocks=False,
            ghdl_args=[],
            parameters={},
            defines=[],
        )
    ).read_text()
    (line,) = [line for line in script.splitlines() if "read_verilog" in line]
    word = line.split()[-1]
    if script_format == "ys":
        assert word == f"{root}/top[[]1].v"
    else:
        tclsh = shutil.which("tclsh")
        if tclsh is None:
            pytest.skip("tclsh is needed to evaluate the TCL word")
        evaluated = subprocess.run(
            [tclsh], input=f"puts {word}\n", capture_output=True, text=True, check=True
        ).stdout.strip()
        assert evaluated == f"{root}/top[[]1].v"
        (log_line,) = [
            line for line in script.splitlines() if "log -stdout" in line and "Reading" in line
        ]
        check_script = tmp_path / "check_log.tcl"
        check_script.write_text("proc yosys {args} {puts [lindex $args end]}\n" + log_line + "\n")
        logged = subprocess.run(
            [tclsh, str(check_script)], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert logged == f"** Reading {root}/top[1].v **"
