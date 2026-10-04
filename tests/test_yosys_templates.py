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
from xeda.flows.yosys.common import NEWEST_CHECKED_YOSYS, process_parameters

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
    design = Design.from_file(RESOURCES_DIR / "design0/design0.toml")
    flow = flow_cls(flow_cls.Settings(**settings), design, tmp_path)
    flow.init()  # registers the `esc` template filter and the netlist artifacts
    flow.artifacts.utilization_report = "reports/utilization.json"
    flow.artifacts.timing_report = "reports/timing.rpt"
    stem = "yosys_fpga_synth" if flow_cls is YosysFpga else "yosys_synth"
    extra = {}
    if flow_cls is YosysFpga:
        extra["synth_command"] = flow.settings.synth_command(NEWEST_CHECKED_YOSYS)
    script = flow.copy_from_template(
        f"{stem}{flow.script_ext}",
        lstrip_blocks=True,
        trim_blocks=False,
        ghdl_args=[],
        parameters=process_parameters(design.rtl.parameters),
        defines=[],
        abc_constr_file=None,
        **extra,
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


NETLIST_WRITERS = ("write_json", "write_verilog", "write_blif")


def _command_lines(script: str, command: str) -> list[int]:
    """The line numbers of `command` in a `.ys` or `.tcl` script (`yosys `-prefixed in TCL)."""
    pattern = re.compile(rf"^\s*(?:yosys\s+)?{re.escape(command)}(?:\s|$)")
    return [n for n, line in enumerate(script.splitlines()) if pattern.match(line)]


BOTH_FLOWS = pytest.mark.parametrize("flow_cls", [Yosys, YosysFpga], ids=["yosys", "yosys_fpga"])
BOTH_FORMATS = pytest.mark.parametrize("script_format", ["ys", "tcl"])


def _settings_for(flow_cls, **kw: Any) -> dict[str, Any]:
    return (_fpga_settings if flow_cls is YosysFpga else _asic_settings)(**kw)


@BOTH_FLOWS
@BOTH_FORMATS
def test_template_unsets_attributes_before_each_netlist(
    flow_cls, script_format, tmp_path: Path
) -> None:
    """Every attribute is removed, from the objects of every module and from the modules
    themselves -- library boxes (`=*`) included -- before *any* netlist is written."""
    settings = _settings_for(
        flow_cls,
        script_format=script_format,
        write_blif="netlist.blif",
        netlist_unset_attributes=["keep"],
    )
    script = _render(flow_cls, settings, tmp_path)
    writers = {writer: _command_lines(script, writer) for writer in NETLIST_WRITERS}
    assert all(writers.values()), f"a netlist writer is missing: {writers}"
    first_write = min(min(lines) for lines in writers.values())
    for attr in ("keep", "src"):
        for unset in (f"setattr -unset {attr} =*", f"setattr -mod -unset {attr} =*"):
            lines = _command_lines(script, unset)
            assert len(lines) == 1, f"{unset!r} must appear exactly once, at {lines}"
            assert lines[0] < first_write, f"{unset!r} comes after a netlist write"


@BOTH_FLOWS
@BOTH_FORMATS
def test_template_writes_json_without_verilog(flow_cls, script_format, tmp_path: Path) -> None:
    settings = _settings_for(flow_cls, script_format=script_format, netlist_verilog=None)
    script = _render(flow_cls, settings, tmp_path)
    (json_write,) = _command_lines(script, "write_json")
    for unset in ("setattr -unset src =*", "setattr -mod -unset src =*"):
        lines = _command_lines(script, unset)
        assert lines and lines[0] < json_write, f"{unset!r} must come before write_json"
    assert not _command_lines(script, "write_verilog")


@BOTH_FLOWS
@pytest.mark.parametrize("netlist_attrs", [True, False, None])
def test_src_is_unset_whenever_it_is_not_kept(flow_cls, netlist_attrs, tmp_path: Path) -> None:
    """`netlist_attrs` governs only `write_verilog -noattr`; the JSON netlist carries attributes
    either way, so `netlist_src_attrs = false` must strip `src` whatever `netlist_attrs` is."""
    settings = _settings_for(flow_cls, netlist_attrs=netlist_attrs)
    assert "src" in flow_cls.Settings(**settings).attributes_to_unset()
    kept = flow_cls.Settings(**settings, netlist_src_attrs=True)
    assert "src" not in kept.attributes_to_unset()
    script = _render(flow_cls, settings, tmp_path)
    assert _command_lines(script, "setattr -mod -unset src =*")


def test_ys_leaves_plugin_and_flag_args_unquoted(tmp_path: Path) -> None:
    """Quoting rules differ per command in a `.ys` script.

    TCL strips quotes before yosys ever sees a word, so the `.tcl` templates can
    quote everything. Yosys' own tokenizer keeps them and leaves stripping to
    each command: its built-in frontends strip, but plugins (`ghdl`, `read_slang`,
    `read_systemverilog`) do not, and `-I<dir>` is one token parsed as a flag.
    """
    design = Design.from_file(RESOURCES_DIR / "design0/design0.toml")
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
    design = Design.from_file(RESOURCES_DIR / "design0/design0.toml")
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
