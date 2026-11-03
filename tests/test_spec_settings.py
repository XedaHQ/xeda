"""Settings are addressed by node: `Spec.settings` and `-s` give the contents of a `flows` table."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda._internal.cli import cli
from xeda._internal.flow import FlowSettingsError, registered_flows
from xeda._internal.flow_runner import DefaultRunner
from xeda._internal.flow_runner.error_codes import public_error
from xeda._internal.spec import Spec, spec_sections

ECP5 = "LFE5U-25F-6BG381C"
ICE40 = "iCE40HX1K-TQ144"


def _design(tmp_path: Path) -> Design:
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    return Design(name="d", root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})


def _spec(tmp_path: Path, settings, flow: str = "nextpnr") -> Spec:
    return Spec(flow, _design(tmp_path), settings=settings)


def _refusal(tmp_path: Path, settings, flow: str = "nextpnr"):
    with pytest.raises(Exception) as raised:
        spec_sections(_spec(tmp_path, settings, flow))
    return public_error(raised.value)


@pytest.mark.parametrize(
    "settings, code, address",
    [
        ({"seed": 7}, "unscoped_setting", "nextpnr.seed"),
        ({"flows": {"nextpnr": {"seed": 7}}}, "unscoped_setting", "nextpnr.seed"),
        ({"flows.nextpnr.seed": 7}, "unscoped_setting", "nextpnr.seed"),
        ({"clocks": {"main_clock": {"period": "5 ns"}}}, "unscoped_setting", "nextpnr.clocks."),
        ({"inputs": {"netlist": "yosys_fpga"}}, "unscoped_setting", "nextpnr.inputs.netlist"),
        ({"nextpnrx": {"seed": 7}}, "unknown_node", "nextpnr"),
        ({"vivado_synth": {"x": 1}}, "unknown_node", None),
        ({"nextpnr": 7}, "invalid_value", None),
    ],
)
def test_a_key_that_is_no_node_table_is_refused_with_the_address_to_write(
    tmp_path, settings, code, address
):
    error = _refusal(tmp_path, settings)
    assert error.code == code
    if address is not None:
        assert error.suggestions and error.suggestions[0].startswith(address), error.suggestions
        assert f"`{error.suggestions[0]}" in str(error)


def test_an_unscoped_key_names_the_flows_of_the_chain_that_have_the_setting(tmp_path):
    error = _refusal(tmp_path, {"fpga": {"part": ECP5}}, "yosys_fpga+nextpnr")
    assert error.code == "unscoped_setting"
    assert set(error.suggestions) == {"nextpnr.fpga.part", "yosys_fpga.fpga.part"}


def test_a_node_outside_the_run_is_refused_with_the_nodes_of_the_run(tmp_path):
    error = _refusal(tmp_path, {"vivado_synth": {"x": 1}})
    assert "names no node of this run" in str(error) and "nextpnr" in str(error)


def test_dotted_and_nested_forms_are_one_table(tmp_path):
    dotted, _ = spec_sections(_spec(tmp_path, {"nextpnr.seed": 7, "nextpnr.placer": "heap"}))
    nested, _ = spec_sections(_spec(tmp_path, {"nextpnr": {"seed": 7, "placer": "heap"}}))
    assert dotted == nested == {"nextpnr": {"seed": 7, "placer": "heap"}}


def test_one_leaf_given_twice_is_refused_even_with_equal_values(tmp_path):
    error = _refusal(tmp_path, {"nextpnr": {"seed": 7}, "nextpnr.seed": 7})
    assert error.code == "conflicting_keys"
    assert "nextpnr.seed" in str(error) and "nextpnr: {...}" in str(error)


def test_binding_keys_of_a_node_become_the_binding_layer(tmp_path):
    sections, layer = spec_sections(
        _spec(tmp_path, {"nextpnr.inputs.netlist": "yosys_fpga", "nextpnr.seed": 3}, "nextpnr")
    )
    assert sections == {"nextpnr": {"seed": 3}}
    assert [(entry.node.flow, entry.name) for entry in layer.entries] == [("nextpnr", "netlist")]


def test_items_that_set_one_setting_to_two_values_are_refused_and_an_exact_repeat_is_not(tmp_path):
    run = _runner(tmp_path).plan
    design = _design(tmp_path)
    plan = run("nextpnr", design, flow_settings=_items("seed=1", "seed=1"))
    assert plan.node("nextpnr").settings.seed == 1
    with pytest.raises(FlowSettingsError) as raised:
        run("nextpnr", design, flow_settings=_items("seed=1", "seed=2"))
    message = str(public_error(raised.value))
    assert "`-s nextpnr.seed=1`" in message and "`-s nextpnr.seed=2`" in message


def _items(*items: str) -> list[str]:
    return [f"nextpnr.{item}" for item in items] + [f"nextpnr.fpga.part={ECP5}"]


def _runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "run", display_results=False)


def test_appending_a_node_does_not_move_a_setting(tmp_path):
    design = _design(tmp_path)
    settings = {"nextpnr": {"seed": 7, "fpga": {"part": ECP5}}}
    alone = _runner(tmp_path).plan("nextpnr", design, flow_settings=settings)
    chained = _runner(tmp_path).plan("yosys_fpga+nextpnr", design, flow_settings=settings)
    assert alone.node("nextpnr").settings.seed == chained.node("nextpnr").settings.seed == 7
    assert alone.node("nextpnr").settings_hash == chained.node("nextpnr").settings_hash


def test_a_shared_setting_on_one_node_propagates_and_a_conflict_is_refused(tmp_path):
    design = _design(tmp_path)
    plan = _runner(tmp_path).plan(
        "yosys_fpga+nextpnr",
        design,
        flow_settings=["yosys_fpga.fpga.part=" + ECP5],
    )
    assert plan.node("nextpnr").settings.fpga.part == ECP5
    with pytest.raises(Exception) as raised:
        _runner(tmp_path).plan(
            "yosys_fpga+nextpnr",
            design,
            flow_settings={
                "yosys_fpga": {"fpga": {"part": ECP5}},
                "nextpnr": {"fpga": {"part": ICE40}},
            },
        )
    assert public_error(raised.value).code == "shared_setting_conflict"


FPGA_CHAIN = "yosys_fpga+nextpnr"
ASIC_CHAIN = "yosys+openroad_place"
BAD_SHARED_VALUES = [
    (FPGA_CHAIN, "nextpnr", {"fpga": {"part": 5}}),
    (FPGA_CHAIN, "nextpnr", {"fpga": {"vendor": 5}}),
    (FPGA_CHAIN, "nextpnr", {"board": 5}),
    (FPGA_CHAIN, "nextpnr", {"out_of_context": "maybe"}),
    (FPGA_CHAIN, "nextpnr", {"clocks": {"main_clock": {"period": 10}}}),
    (FPGA_CHAIN, "nextpnr", {"clocks": {"main_clock": {"period": "10 MHz"}}}),
    (FPGA_CHAIN, "nextpnr", {"clocks": {"main_clock": {"freq": 10}}}),
    (FPGA_CHAIN, "nextpnr", {"clocks": {"main_clock": {"period": "1 ns", "uncertainty": 3}}}),
    (FPGA_CHAIN, "nextpnr", {"clocks": {"main_clock": 5}}),
    (ASIC_CHAIN, "openroad_place", {"hierarchical": "maybe"}),
    (ASIC_CHAIN, "openroad_place", {"wire_rc": 5}),
    (ASIC_CHAIN, "openroad_place", {"wire_rc": {"signal": 5}}),
]


def _plan_code(tmp_path, flow, settings):
    with pytest.raises(Exception) as raised:
        _runner(tmp_path).plan(flow, _design(tmp_path), flow_settings=settings)
    return public_error(raised.value).code


@pytest.mark.parametrize("chain, node, value", BAD_SHARED_VALUES, ids=str)
def test_a_malformed_shared_value_gets_the_code_of_the_same_value_of_an_ordinary_setting(
    tmp_path, chain, node, value
):
    flow = registered_flows[node][1]
    with pytest.raises(FlowSettingsError) as raised:
        flow.Settings.from_input(value, design_root=tmp_path, runner_cwd=tmp_path)
    code = public_error(raised.value).code
    assert code != "shared_setting_conflict"
    assert _plan_code(tmp_path, chain, {node: value}) == code
    assert _plan_code(tmp_path, node, {node: value}) == code


@pytest.mark.parametrize(
    "setting", [{"platform": "no_such_platform"}, {"platform_file": "missing.yaml"}]
)
def test_a_platform_that_cannot_be_read_is_unsupported_not_a_conflict(tmp_path, setting):
    assert _plan_code(tmp_path, ASIC_CHAIN, {"openroad_place": setting}) == "unsupported_setting"


def test_a_shared_path_that_cannot_be_resolved_is_an_invalid_value(tmp_path):
    settings = {"openroad_place": {"platform": "nangate45", "liberty": ["a\x00b.lib"]}}
    assert _plan_code(tmp_path, ASIC_CHAIN, settings) == "invalid_value"


def test_two_clock_periods_that_differ_are_a_shared_setting_conflict(tmp_path):
    settings = {
        "yosys_fpga": {"clocks": {"main_clock": {"period": "10 ns"}}},
        "nextpnr": {"clocks": {"main_clock": {"period": "20 ns"}}},
    }
    assert _plan_code(tmp_path, FPGA_CHAIN, settings) == "shared_setting_conflict"


def test_a_dollar_pwd_expands_when_the_spec_is_made(tmp_path):
    spec = _spec(tmp_path, {"nextpnr": {"placed_svg": "$PWD/out.svg", "a": "${PWD}/x"}})
    assert spec.settings["nextpnr"]["placed_svg"] == f"{os.getcwd()}/out.svg"
    assert spec.settings["nextpnr"]["a"] == f"{os.getcwd()}/x"
    assert _spec(tmp_path, {"nextpnr": {"a": "$PWDX"}}).settings["nextpnr"]["a"] == "$PWDX"


def _dry_run(tmp_path: Path, flow: str, *items: str):
    design = tmp_path / "design.yaml"
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    design.write_text("name: d\nrtl:\n  sources: [top.v]\n  top: top\n")
    result = CliRunner().invoke(
        cli,
        ["run", "--dry-run", "--json", "--run-root", str(tmp_path / "run"), flow, str(design)]
        + (["-s", *items] if items else []),
    )
    return result, json.loads(result.stdout)


def test_the_command_line_addresses_a_setting_by_its_node(tmp_path):
    result, document = _dry_run(tmp_path, "nextpnr", "nextpnr.seed=3", f"nextpnr.fpga.part={ECP5}")
    assert result.exit_code == 0, result.output
    assert document["nodes"][-1]["name"] == "nextpnr"


@pytest.mark.parametrize(
    "item, code, hint",
    [
        ("seed=3", "unscoped_setting", "`nextpnr.seed`"),
        ("flows.nextpnr.seed=3", "unscoped_setting", "write `nextpnr.seed`"),
        ("nextpnrx.seed=3", "unknown_node", "did you mean `nextpnr`"),
    ],
)
def test_the_command_line_refuses_a_key_without_a_node(tmp_path, item, code, hint):
    result, document = _dry_run(tmp_path, "nextpnr", item, f"nextpnr.fpga.part={ECP5}")
    assert result.exit_code == 2, result.output
    assert document["outcome"] is None
    assert document["error"]["code"] == code
    assert hint in document["error"]["message"]


def test_the_command_line_names_both_items_that_set_one_setting_to_two_values(tmp_path):
    result, document = _dry_run(
        tmp_path, "nextpnr", "nextpnr.seed=1", "nextpnr.seed=2", f"nextpnr.fpga.part={ECP5}"
    )
    assert result.exit_code == 2, result.output
    assert document["error"]["code"] == "conflicting_keys"
    assert "-s nextpnr.seed=1" in document["error"]["message"]
    assert "-s nextpnr.seed=2" in document["error"]["message"]
    result, _ = _dry_run(
        tmp_path, "nextpnr", "nextpnr.seed=1", "nextpnr.seed=1", f"nextpnr.fpga.part={ECP5}"
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "settings, address",
    [({"nthreads": 2}, "nextpnr.nthreads"), ({"flows.nextpnr.seed": 7}, "nextpnr.seed")],
)
def test_a_python_layer_refuses_a_key_without_a_node(tmp_path, settings, address):
    with pytest.raises(FlowSettingsError) as raised:
        _runner(tmp_path).plan("nextpnr", _design(tmp_path), flow_settings=settings)
    error = public_error(raised.value)
    assert error.code == "unscoped_setting"
    assert error.suggestions[0] == address


def test_a_python_layer_takes_a_table_of_nodes_without_a_wrapper(tmp_path):
    settings = {"nextpnr": {"seed": 7, "fpga": {"part": ECP5}}}
    plan = _runner(tmp_path).plan("nextpnr", _design(tmp_path), flow_settings=settings)
    assert plan.node("nextpnr").settings.seed == 7


def test_a_binding_makes_its_producer_a_node_of_the_spec(tmp_path):
    names = ("netlist", "netlist_timing", "sdf")
    bound = {f"vivado_postsynth_sim.inputs.{name}": f"vivado_alt_synth.{name}" for name in names}
    spec = _spec(tmp_path, {**bound, "vivado_alt_synth.nthreads": 4}, "vivado_postsynth_sim")
    sections, layer = spec_sections(spec)
    assert sections["vivado_alt_synth"] == {"nthreads": 4}
    assert len(layer.entries) == 3
    with pytest.raises(FlowSettingsError):  # without the bindings it is no node of the run
        spec_sections(_spec(tmp_path, {"vivado_alt_synth.nthreads": 4}, "vivado_postsynth_sim"))


def test_a_table_of_a_mapping_is_the_command_line_table():
    from xeda._internal.flow_runner.settings_layers import command_line_table

    assert command_line_table({"nextpnr": {"seed": 7}}) == {"nextpnr": {"seed": 7}}
    assert command_line_table(["nextpnr.seed=7"]) == {"nextpnr": {"seed": "7"}}
    with pytest.raises(ValueError):
        command_line_table(["nextpnr"])


def test_a_binding_of_the_project_makes_its_producer_a_node_of_the_spec(tmp_path):
    from xeda._internal.project import Project

    _design(tmp_path)
    names = ("netlist", "netlist_timing", "sdf")
    bound = {name: f"vivado_alt_synth.{name}" for name in names}
    (tmp_path / "xedaproject.yaml").write_text(
        json.dumps(
            {
                "designs": [{"name": "d", "rtl": {"sources": ["top.v"], "top": "top"}}],
                "flows": {"vivado_postsynth_sim": {"inputs": bound}},
            }
        )
    )
    design = Project.load(tmp_path / "xedaproject.yaml").design("d")
    spec = Spec("vivado_postsynth_sim", design, settings={"vivado_alt_synth.nthreads": 4})
    sections, _layer = spec_sections(spec)
    assert sections["vivado_alt_synth"] == {"nthreads": 4}
