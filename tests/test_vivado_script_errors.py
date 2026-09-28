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
from pathlib import Path

import click
import pytest

from xeda import Design
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner

from .tool_utils import fake_calls, use_fake_tools

TEMPLATES = Path(__file__).parent.parent / "src" / "xeda" / "flows" / "vivado" / "templates"
INCLUDE_UTIL = re.compile(r"\{%-?\s*include\s+['\"]util\.tcl['\"]\s*-?%\}")
PROC = re.compile(r"^\s*proc\s+(\w+)", re.M)


def _code(template: Path) -> str:
    """The template's TCL, without Jinja or TCL comments."""
    text = re.sub(r"\{#.*?#\}", "", template.read_text(), flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_every_vivado_template_includes_the_procs_it_calls() -> None:
    """A proc a template calls is its own or `util.tcl`'s, and then the template includes
    `util.tcl`. The procs of every template are the candidates, so a proc one template defines
    for itself cannot be called from another."""
    templates = sorted(TEMPLATES.glob("*.tcl"))
    defined = {template.name: set(PROC.findall(_code(template))) for template in templates}
    shared = defined["util.tcl"]
    every_proc = set().union(*defined.values())
    assert "errorExit" in shared
    for template in templates:
        code = _code(template)
        called = {p for p in every_proc - defined[template.name] if re.search(rf"\b{p}\b", code)}
        assert called <= shared, (template.name, called - shared)
        if called and template.name != "util.tcl":
            assert INCLUDE_UTIL.search(template.read_text()), (template.name, called)


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
