"""A Vivado script that fails says why: every proc it calls is defined where it runs.

The Vivado templates share their procs through `util.tcl`, which a template includes. A template
that called one without including it failed on a read error with Vivado's `invalid command name
"errorExit"` instead of the error itself: `vivado_synth.tcl` never included `util.tcl`, and no
template but `vivado_sim.tcl` defined `errorExit`, so `vivado_alt_synth.tcl`, which does include
`util.tcl`, failed the same way. (The fake Vivado records an unknown command as a tool command,
so there the read error was not even a failure.)
"""

import re
import shutil
import subprocess
from pathlib import Path

import click
import pytest

from xeda import Design
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner

from .tool_utils import FAKE_TOOLS_DIR, fake_calls, use_fake_tools

TEMPLATES = Path(__file__).parent.parent / "src" / "xeda" / "flows" / "vivado" / "templates"
INCLUDE = re.compile(r"\{%-?\s*include\s+['\"]([\w.]+)['\"]\s*-?%\}")
PROC = re.compile(r"^\s*proc\s+(\w+)", re.M)


def _code(template: Path) -> str:
    """The template's TCL, without Jinja or TCL comments."""
    text = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _script(template: Path) -> str:
    """The template's TCL with each template it includes in its place: the code of the script as
    it is rendered."""
    return INCLUDE.sub(lambda found: _script(TEMPLATES / found[1]), _code(template))


def test_every_vivado_script_defines_the_procs_it_calls() -> None:
    """A proc a script calls is its own or `util.tcl`'s, and then the script includes `util.tcl`,
    itself or in a template it includes (`implementation.tcl` is part of two scripts). The procs
    of every template are the candidates, so a proc one script defines for itself cannot be
    called from another."""
    templates = sorted(TEMPLATES.glob("*.tcl"))
    fragments = {name for template in templates for name in INCLUDE.findall(template.read_text())}
    every_proc = set().union(*(set(PROC.findall(_code(template))) for template in templates))
    assert "errorExit" in set(PROC.findall(_code(TEMPLATES / "util.tcl")))
    for template in templates:
        if template.name in fragments:  # judged in the scripts that include it
            continue
        code = _script(template)
        called = {p for p in every_proc - set(PROC.findall(code)) if re.search(rf"\b{p}\b", code)}
        assert not called, (template.name, called)


def _design(root: Path) -> Design:
    """A VHDL design with a VHDL testbench."""
    root.mkdir(parents=True)
    (root / "top.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity top is port(clk, a: in std_logic; y: out std_logic); end;\n"
        "architecture rtl of top is begin y <= not a; end;\n"
    )
    (root / "tb.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity tb is end;\n"
        "architecture sim of tb is signal clk, a, y: std_logic := '0';\n"
        "begin uut: entity work.top port map(clk, a, y); end;\n"
    )
    return Design(
        name="rd",
        design_root=root,
        rtl={"sources": ["top.vhd"], "top": "top", "clock_port": "clk"},
        tb={"sources": ["tb.vhd"], "top": "tb", "uut": "uut"},
    )


FPGA = {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0}


@pytest.mark.skipif(not shutil.which("tclsh"), reason="the fake Vivado runs its TCL under tclsh")
@pytest.mark.parametrize(
    ("flow", "settings", "failing"),
    [
        ("vivado_synth", FPGA, "read_vhdl"),
        ("vivado_alt_synth", FPGA, "read_vhdl"),
        ("vivado_sim", {}, "xvhdl"),
    ],
)
def test_a_read_error_fails_the_script_with_its_own_message(
    flow, settings, failing, tmp_path, monkeypatch, capfd
) -> None:
    """Each script that reads the design, rendered as its flow renders it and run under the
    fake Vivado's tclsh, with the command reading the VHDL source failing as it does on a
    syntax error: the script ends with that error, through `errorExit`."""
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", failing)
    run = DefaultRunner(tmp_path / "run").run_flow(
        registered_flows[flow][1], _design(tmp_path / "design"), settings
    )
    assert run is not None and not run.succeeded
    output = click.unstyle(capfd.readouterr().out)
    assert f"ERROR: {failing} failed" in output
    assert "invalid command name" not in output
    calls = fake_calls(run.run_path)
    assert failing in {call[0] if call[0] != "exec" else call[1] for call in calls}
    assert "errorExit" not in {call[0] for call in calls}, "errorExit ran as a tool command"


#: the commands of the templates that write a file, each with the file in `{file}`
WRITING_COMMANDS = [
    "report_timing_summary -no_header -delay_type max -file {file}",
    "report_utilization -hierarchical -force -file {file}",
    "report_power_opt -file {file}",
    "report_drc -file {file}",
    "report_power -hier all -format xml -verbose -file {file}",
    "write_checkpoint -force {file}",
    "write_bitstream -force {file}",
    "write_verilog -mode funcsim -force {file}",
    "write_verilog -mode timesim -sdf_anno false -force -file {file}",
    "write_sdf -mode timesim -process_corner slow -force -file {file}",
    "write_xdc -no_fixed_only -force {file}",
]


@pytest.mark.skipif(not shutil.which("tclsh"), reason="the fake Vivado runs its TCL under tclsh")
@pytest.mark.parametrize("command", WRITING_COMMANDS)
def test_the_fake_vivado_makes_no_directory_for_a_file_it_writes(command, tmp_path) -> None:
    """Vivado does not make the directory of a report or a file: `ERROR: [Common 17-37]
    Directory in which file ... is to be written does not exist`. A stand-in that made it hid
    a script that relied on it: `vivado_impl` never made `reports/post_place`, which the power
    optimization of the shared implementation steps reports into."""
    script = tmp_path / "script.tcl"
    script.write_text(command.format(file="out/dir/file.rpt") + "\n")
    run = ["vivado", "-mode", "batch", "-source", str(script)]
    missing = subprocess.run(
        [str(FAKE_TOOLS_DIR / run[0]), *run[1:]], cwd=tmp_path, capture_output=True, text=True
    )
    assert missing.returncode != 0, command
    assert (
        "[Common 17-37] Directory in which file file.rpt is to be written does not exist [out/dir]"
    ) in missing.stderr
    assert not (tmp_path / "out").exists(), "the stand-in made the directory"
    (tmp_path / "out" / "dir").mkdir(parents=True)
    made = subprocess.run(
        [str(FAKE_TOOLS_DIR / run[0]), *run[1:]], cwd=tmp_path, capture_output=True, text=True
    )
    assert made.returncode == 0, made.stderr
