"""A dictionary setting inside a settings group takes a name with dots as one key.

A flattened hierarchy names its instances and nets with dots (`u_core.reg_1`). Every origin
expands a dotted key into nested tables before it knows the model (`utils.set_hierarchy`), so a
group declares the settings of that kind in `opaque_key_fields`, and the group folds the nested
tables back into one key. These tests give such a name to `ir_drop.instance_power`,
`ir_drop.nets`, `ir_drop.vsrc_files` and `ndr.assignments` from the command line, a design file
and a project file, and check that the rules for ordinary settings stay.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from xeda.cli_types import _is_key_value, starts_opaque_key
from xeda.dataclass import opaque_key_paths
from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import merge_layers
from xeda.flows.openroad import Openroad
from xeda.units import Power
from xeda.utils import ConflictingKeys

from .openroad_utils import openroad_design, openroad_settings, register_openroad_flows
from .tiny_platform import tiny_store
from .project_files import project_listing
from .setting_texts import text_items

NAME = "u_core.reg_1"
DEEP_NAME = "u_top.u_core.u_alu.reg_1"
MILLIWATT = Power("1 mW")


def plan_openroad(
    tmp_path: Path,
    *texts: str,
    design_flows: dict[str, Any] | None = None,
    project_flows: dict[str, Any] | None = None,
):
    """Plan `openroad` on the tiny platform and return the settings of the last node. `texts` are
    command-line settings; the sections are the `flows.openroad` tables of the design file and of
    a project file."""
    store = tiny_store(tmp_path)
    design = openroad_design(tmp_path / "design")
    if design_flows is not None:
        design.write_text(
            design.read_text() + yaml.safe_dump({"flows": {"openroad": design_flows}})
        )
    kwargs: dict[str, Any] = {}
    selected: Path | str = design
    if project_flows is not None:
        kwargs["xedaproject"] = project_listing(design, {"openroad": project_flows})
        selected = "counter"
    given = text_items([*openroad_settings(store), *texts])
    with register_openroad_flows():
        plan = DefaultRunner(tmp_path / "run", display_results=False).plan(
            "openroad", selected, flow_settings=given, **kwargs
        )
    return plan.nodes[-1].settings


def test_the_groups_declare_the_dictionaries_of_names_from_a_netlist():
    assert opaque_key_paths(Openroad.Settings) == (
        "ndr.assignments",
        "ir_drop.nets",
        "ir_drop.vsrc_files",
        "ir_drop.instance_power",
    )


@pytest.mark.parametrize("name", [NAME, DEEP_NAME])
def test_the_command_line_gives_an_instance_with_dots_one_key(tmp_path, name):
    settings = plan_openroad(tmp_path, f"ir_drop.instance_power.{name}=1mW")
    assert dict(settings.ir_drop.instance_power) == {name: MILLIWATT}


def test_the_command_line_gives_a_net_with_dots_one_key(tmp_path):
    settings = plan_openroad(
        tmp_path,
        "ndr.rules.wide.width.met1=0.28um",
        "ndr.assignments.u_core.clk_div=wide",
        "ir_drop.nets.u_core.VDD=1.1V",
    )
    assert dict(settings.ndr.assignments) == {"u_core.clk_div": "wide"}
    assert list(settings.ir_drop.nets) == ["u_core.VDD"]


def test_the_command_line_qualified_by_the_flow_gives_the_same_key(tmp_path):
    settings = plan_openroad(tmp_path, f"flows.openroad.ir_drop.instance_power.{NAME}=1mW")
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT}


def test_a_name_with_signs_is_a_key_of_the_command_line(tmp_path):
    """A name from a netlist holds characters that no setting name has (`\\u_core/reg[1]`)."""
    name = "u_core.reg[1]$x"
    settings = plan_openroad(tmp_path, f"ir_drop.instance_power.{name}=1mW")
    assert dict(settings.ir_drop.instance_power) == {name: MILLIWATT}


def test_a_design_file_gives_an_instance_with_dots_one_key(tmp_path):
    settings = plan_openroad(
        tmp_path,
        design_flows={
            f"ir_drop.instance_power.{NAME}": "1 mW",
            f"ir_drop.instance_power.{DEEP_NAME}": "2 mW",
            "ndr.assignments.u_core.clk_div": "wide",
        },
    )
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT, DEEP_NAME: Power("2 mW")}
    assert dict(settings.ndr.assignments) == {"u_core.clk_div": "wide"}


def test_the_mapping_of_a_design_file_keeps_the_name(tmp_path):
    settings = plan_openroad(
        tmp_path,
        design_flows={
            "ir_drop": {"instance_power": {NAME: "1 mW"}},
            "ndr": {"assignments": {"u_core.clk_div": "wide"}},
        },
    )
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT}
    assert dict(settings.ndr.assignments) == {"u_core.clk_div": "wide"}


def test_the_nested_tables_of_a_dotted_key_are_the_same_setting(tmp_path):
    settings = plan_openroad(
        tmp_path,
        design_flows={"ir_drop": {"instance_power": {"u_core": {"reg_1": "1 mW"}}}},
    )
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT}


def test_a_project_file_gives_an_instance_with_dots_one_key(tmp_path):
    settings = plan_openroad(tmp_path, project_flows={f"ir_drop.instance_power.{NAME}": "1 mW"})
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT}


def test_the_origins_stack_name_by_name(tmp_path):
    """The command line replaces the instance it names and keeps the others of the design."""
    settings = plan_openroad(
        tmp_path,
        f"ir_drop.instance_power.{NAME}=3mW",
        design_flows={
            f"ir_drop.instance_power.{NAME}": "1 mW",
            "ir_drop.instance_power.u_core.reg_2": "2 mW",
        },
    )
    assert dict(settings.ir_drop.instance_power) == {
        NAME: Power("3 mW"),
        "u_core.reg_2": Power("2 mW"),
    }


def test_an_instance_and_an_instance_inside_it_are_both_kept_across_origins(tmp_path):
    """A hierarchical database has the instance `u_core` and the instance `u_core.reg_1`. Each
    origin gives its names whole, so the origins merge name by name."""
    settings = plan_openroad(
        tmp_path,
        f"ir_drop.instance_power.{NAME}=1mW",
        design_flows={"ir_drop.instance_power.u_core": "2 mW"},
    )
    assert dict(settings.ir_drop.instance_power) == {"u_core": Power("2 mW"), NAME: MILLIWATT}


def test_the_names_of_one_origin_are_not_a_value_and_a_key_inside_it():
    """As for any setting, one origin writes a key either as a value or as a table."""
    with pytest.raises(ConflictingKeys, match="ir_drop.instance_power.u_core"):
        merge_layers(
            {"ir_drop.instance_power.u_core": "2 mW", f"ir_drop.instance_power.{NAME}": "1 mW"},
            settings_cls=Openroad.Settings,
        )


def test_the_names_merge_as_names_in_a_merge_of_layers():
    merged = merge_layers(
        {"ir_drop.instance_power.u_core": "2 mW"},
        {f"ir_drop.instance_power.{NAME}": "1 mW"},
        settings_cls=Openroad.Settings,
    )
    settings = Openroad.Settings.from_input(merged)
    assert dict(settings.ir_drop.instance_power) == {"u_core": Power("2 mW"), NAME: MILLIWATT}


def test_a_name_with_a_backslash_before_a_dot_is_refused():
    with pytest.raises(FlowSettingsException, match="backslash before a dot"):
        Openroad.Settings.from_input({"ir_drop": {"instance_power": {"a\\.b": "1 mW"}}})


def test_an_assignment_to_the_group_folds_the_names_too():
    settings = Openroad.Settings()
    settings.ir_drop.instance_power = {"u_core": {"reg_1": "1 mW"}}
    assert dict(settings.ir_drop.instance_power) == {NAME: MILLIWATT}


def test_the_settings_reload_with_the_names_kept():
    settings = Openroad.Settings.from_input({"ir_drop": {"instance_power": {NAME: "1 mW"}}})
    again = Openroad.Settings.from_input(settings.model_dump(mode="json"))
    assert dict(again.ir_drop.instance_power) == dict(settings.ir_drop.instance_power)


def test_a_path_that_goes_on_past_a_value_is_still_an_error(tmp_path):
    """The rest of the path is the name of a key. A key holds a power, and not a table."""
    with register_openroad_flows(), pytest.raises(FlowSettingsException) as error:
        plan_openroad(tmp_path, "ir_drop.enabled.u_core=1mW")
    assert "ir_drop" in str(error.value) and "enabled" in str(error.value)


def test_a_dictionary_of_tables_still_goes_on_into_the_table(tmp_path):
    """`ndr.rules` holds tables, so `ndr.rules.wide.width` is a path into the rule."""
    settings = plan_openroad(tmp_path, "ndr.rules.wide.width.met1=0.28um")
    assert list(settings.ndr.rules) == ["wide"]
    assert list(settings.ndr.rules["wide"].width) == ["met1"]


@pytest.mark.parametrize(
    "token",
    [
        "ir_drop.instance_power.u_core/reg 1=1mW",
        "ir_drop.instance_power.\\u_core.reg[1]$x=1mW",
        "openroad.ndr.assignments.u_core.n$1=wide",
        "ir_drop.nets.u_core.VDD=1.1V",
    ],
)
def test_the_command_line_takes_a_name_with_signs_after_the_path_of_the_setting(token):
    assert _is_key_value(token)


@pytest.mark.parametrize(
    "token",
    [
        "ir_drop.enabled.u_core/reg 1=true",
        "ir_drop.u_core/reg 1=1mW",
        "dir/ir_drop.instance_power.x=1mW",
    ],
)
def test_the_command_line_takes_no_other_key_with_signs(token):
    assert not _is_key_value(token)


def test_a_key_starts_a_name_of_a_tool_after_a_path_of_identifiers_only():
    paths = {"ir_drop.instance_power"}
    assert starts_opaque_key("ir_drop.instance_power.a b", paths)
    assert starts_opaque_key("openroad.ir_drop.instance_power.a b", paths)
    assert not starts_opaque_key("ir_drop.instance_power", paths)
    assert not starts_opaque_key("x/ir_drop.instance_power.a b", paths)


def test_a_path_variable_in_a_nested_name_expands_as_in_the_folded_name(tmp_path):
    """The group folds `{"u_core": {"VDD": ...}}` to the one name `u_core.VDD`. The path is
    expanded as the value of that name, so a reload of the settings gives the same path."""
    root = tmp_path / "design"
    given = {"ir_drop": {"vsrc_files": {"u_core": {"VDD": "$DESIGN_ROOT/vsrc.csv"}}}}
    settings = Openroad.Settings.from_input(given, design_root=root)
    assert dict(settings.ir_drop.vsrc_files) == {"u_core.VDD": root / "vsrc.csv"}
    again = Openroad.Settings.from_input(settings.model_dump(mode="json"), design_root=root)
    assert again.ir_drop.vsrc_files == settings.ir_drop.vsrc_files
