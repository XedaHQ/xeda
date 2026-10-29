"""The flows that `-s <name>.<key>` may name follow the design and the plan.

A run may address its nodes and the default producers that a source or a binding displaced. A
converter belongs to the run only when the design has a source that it converts.
"""

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.design import SourceType
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import suggest_dependency_node, transitive_dependencies
from xeda.flows import Nextpnr, Yosys, YosysFpga

PART = "xc7a100tfgg676-3"
ECP5 = "LFE5U-25F-6BG381C"
GCD = Path(__file__).parent.parent / "examples" / "bluespec" / "gcd" / "gcd.yaml"

SOURCES = {
    SourceType.Verilog: ("top.sv", "module top(input clk); endmodule\n"),
    SourceType.Bluespec: ("Top.bsv", "package Top; endpackage\n"),
    SourceType.JsonNetlist: ("net.json", "{}\n"),
}


def _design(tmp_path: Path, *kinds: SourceType) -> Design:
    root = tmp_path / "d"
    root.mkdir(exist_ok=True)
    entries = []
    for kind in kinds:
        name, text = SOURCES[kind]
        (root / name).write_text(text)
        entries.append({"file": name, "type": kind.name})
    return Design(name="d", root=root, rtl={"sources": entries, "top": "top"})


def _plan(tmp_path: Path, request, design, **kwargs):
    return DefaultRunner(tmp_path / "run", display_results=False).plan(request, design, **kwargs)


def _run(tmp_path: Path, design: Design, *items: str, flow: str = "yosys_fpga"):
    """Run `xeda run --dry-run --json` on a design written to a file."""
    path = tmp_path / "design.yaml"
    path.write_text(
        json.dumps(
            {
                "name": "d",
                "rtl": {
                    "sources": [
                        {"file": str(s.path), "type": s.type.name} for s in design.rtl.sources
                    ],
                    "top": "top",
                },
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        ["run", "--dry-run", "--json", flow, str(path), "-s", f"{flow}.fpga.part={PART}", *items],
    )
    return result, json.loads(result.stdout)


# ------------------------------------------------------------------ a converter needs its source


def test_a_converter_is_no_flow_of_a_run_of_a_design_without_its_source(tmp_path):
    result, document = _run(tmp_path, _design(tmp_path, SourceType.Verilog), "bsc.optimize=true")
    assert result.exit_code == 2, result.output
    assert document["outcome"] is None and document["nodes"] == []
    message = document["error"]["message"]
    assert "bsc: names no node of this run (yosys_fpga)" in message, message
    assert "bsc" not in message.split("(")[-1]
    assert not (tmp_path / "xeda_run").exists()


def test_the_refusal_of_a_flow_outside_the_run_has_the_unknown_flow_kind(tmp_path):
    with pytest.raises(FlowSettingsError) as raised:
        _plan(
            tmp_path,
            "yosys_fpga",
            _design(tmp_path, SourceType.Verilog),
            flow_settings=[f"fpga.part={PART}", "flows.bsc.optimize=true"],
        )
    assert [kind for _location, _message, _context, kind in raised.value.errors] == ["unknown_node"]


def test_the_list_of_flows_of_the_run_holds_the_flows_of_the_plan_only(tmp_path):
    result, document = _run(tmp_path, _design(tmp_path, SourceType.Verilog), "vivado_synth.x=1")
    assert result.exit_code == 2, result.output
    message = document["error"]["message"]
    assert "vivado_synth: names no node of this run (yosys_fpga)" in message, message


def test_a_converter_of_a_source_of_the_design_is_a_flow_of_the_run(tmp_path):
    design = _design(tmp_path, SourceType.Bluespec)
    plan = _plan(
        tmp_path,
        "yosys_fpga",
        design,
        flow_settings=[f"fpga.part={PART}", "flows.bsc.optimize=true"],
    )
    assert [node.name for node in plan.nodes] == ["bsc", "yosys_fpga"]
    assert plan.node("bsc").settings.optimize is True


def test_a_converter_of_a_source_that_a_dependency_brings_is_a_flow_of_the_run(tmp_path):
    """The Bluespec sources of the example design come from its dependency."""
    plan = _plan(
        tmp_path,
        "yosys_fpga",
        str(GCD),
        xedaproject="",
        flow_settings=[f"fpga.part={PART}", "flows.bsc.optimize=true"],
    )
    assert plan.node("bsc").settings.optimize is True


def test_a_displaced_default_producer_is_still_a_flow_of_the_run(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    plan = _plan(
        tmp_path,
        Nextpnr.name,
        _design(tmp_path, SourceType.JsonNetlist),
        flow_settings=[f"fpga.part={ECP5}", "flows.yosys_fpga.flatten=true"],
    )
    assert [node.name for node in plan.nodes] == ["nextpnr"]
    assert "yosys_fpga is not part of this run: its settings are unused" in caplog.text


def test_a_flow_that_a_chain_adds_is_a_flow_of_the_run(tmp_path):
    plan = _plan(
        tmp_path,
        "yosys_fpga+vivado_impl",
        _design(tmp_path, SourceType.Verilog),
        flow_settings=[f"fpga.part={PART}", "flows.vivado_impl.tcl_shell=false"],
    )
    assert plan.node("vivado_impl").settings.tcl_shell is False


def test_a_design_section_of_a_flow_outside_the_run_is_ignored(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    design = _design(tmp_path, SourceType.Verilog)
    design = design.model_copy(update={"flows": {"bsc": {"optimize": True}}})
    plan = _plan(tmp_path, "yosys_fpga", design, flow_settings=[f"fpga.part={PART}"])
    assert [node.name for node in plan.nodes] == ["yosys_fpga"]
    assert "bsc" not in caplog.text


# ------------------------------------------------------------------------------ the hint


def test_an_unknown_setting_gets_no_hint_for_a_converter_the_design_does_not_need(tmp_path):
    result, document = _run(tmp_path, _design(tmp_path, SourceType.Verilog), "optimize=true")
    assert result.exit_code == 2, result.output
    message = document["error"]["message"]
    assert "optimize" in message and "flows.bsc" not in message, message


def test_an_unknown_setting_gets_the_hint_for_a_converter_the_design_needs(tmp_path):
    design = _design(tmp_path, SourceType.Bluespec)
    with pytest.raises(FlowSettingsError, match=r"did you mean -s bsc\.optimize"):
        _plan(tmp_path, "yosys_fpga", design, flow_settings=[f"fpga.part={PART}", "optimize=true"])


def test_the_hint_names_a_producer_of_the_flow_for_a_design_with_the_source(tmp_path):
    error = FlowSettingsError(
        [("optimize", "Extra inputs are not permitted", "", "extra_forbidden")],
        YosysFpga.Settings,
    )
    suggest_dependency_node(YosysFpga, _design(tmp_path, SourceType.Bluespec), error)
    assert "-s bsc.optimize" in error.errors[0][1]
    plain = FlowSettingsError(
        [("optimize", "Extra inputs are not permitted", "", "extra_forbidden")],
        YosysFpga.Settings,
    )
    suggest_dependency_node(YosysFpga, _design(tmp_path, SourceType.Verilog), plain)
    assert "flows.bsc" not in plain.errors[0][1]


# --------------------------------------------------------------------- the one definition


def test_the_flows_of_a_run_depend_on_the_languages_of_the_design(tmp_path):
    verilog = _design(tmp_path, SourceType.Verilog)
    bluespec = _design(tmp_path, SourceType.Bluespec)
    for consumer in (Yosys, YosysFpga, Nextpnr):
        assert "bsc" not in transitive_dependencies(consumer, verilog)
        assert "bsc" in transitive_dependencies(consumer, bluespec)


def test_the_hint_for_a_converter_holds_for_a_source_that_a_dependency_brings(tmp_path):
    with pytest.raises(FlowSettingsError, match=r"did you mean -s bsc\.optimize"):
        _plan(
            tmp_path,
            "yosys_fpga",
            str(GCD),
            xedaproject="",
            flow_settings=[f"fpga.part={PART}", "optimize=true"],
        )
