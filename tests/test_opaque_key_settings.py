"""A setting whose keys are names of the tool takes a name with dots as one key.

Vivado's run properties are named `STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`. Every origin expands a
dotted key into nested tables before it knows which model the table is for
(`utils.set_hierarchy`), so such a name used to reach `set_synth_properties` as the table
`STEPS -> SYNTH_DESIGN -> ARGS -> MAX_BRAM` and fail as "not a valid string". A flow declares
the settings of that kind in `Flow.Settings.opaque_key_fields`, and the settings fold the nested
tables of such a setting back into one key (`dataclass.fold_dotted_keys`). The tests give a
property from every origin, check that the Tcl gets it as one word, and classify every
dictionary setting of every flow so that a new one has to be decided.
"""

import inspect
import json
import typing
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.dataclass import (
    AtomicName,
    BaseModel,
    annotation_kind,
    fold_dotted_keys,
    name_inside_another,
    opaque_key_fields_of,
)
from xeda.flow import Flow, FlowSettingsException
from xeda.flow.flow import registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import _atomic_table, merge_layers
from xeda.flows import VivadoAltSynth, VivadoProject, VivadoSynth
from xeda.utils import ConflictingKeys, settings_to_dict

from .tool_utils import fake_calls, use_fake_tools

PART = "xc7a12tcsg325-1"
SYNTH_NAME = "STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM"
IMPL_NAME = "STEPS.OPT_DESIGN.ARGS.DIRECTIVE"
SPACED_NAME = "STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS"
BASE = {"fpga": PART, "clock": {"period": "5.5 ns"}}

OPAQUE = ("set_synth_properties", "set_impl_properties")
ISE_STEPS = ("synthesis", "map", "pnr", "translate", "trace")

TOP = "module top(input clk); endmodule\n"


@pytest.fixture(autouse=True)
def start_in_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text(TOP)


def design_data(flows=None, **more) -> dict:
    data: dict = {"name": "d", "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"}, **more}
    if flows is not None:
        data["flows"] = flows
    return data


def write_design(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "d.yaml"
    path.write_text(text)
    return path


def planned(tmp_path: Path, design, **kwargs) -> Flow.Settings:
    plan = DefaultRunner(tmp_path / "planned", display_results=False).plan(
        VivadoSynth, design, **kwargs
    )
    return plan.node("vivado_synth").settings


def properties(settings) -> tuple[dict, dict]:
    """The two dictionaries, with every value as text (the command line writes text)."""
    return tuple(  # type: ignore[return-value]
        {name: str(value) for name, value in table.items()}
        for table in (settings.set_synth_properties, settings.set_impl_properties)
    )


WANTED = ({SYNTH_NAME: "0"}, {IMPL_NAME: "Explore"})

# ------------------------------------------------------------------------------------ origins


BASE_ITEMS = [("fpga", PART), ("clock.period", "5.5 ns")]


def design_from(tmp_path: Path, data: dict) -> Design:
    return Design.from_file(write_design(tmp_path, yaml.safe_dump(data, sort_keys=False)))


def flows_section(synth, impl) -> dict:
    return {"vivado_synth": {**BASE, "set_synth_properties": synth, "set_impl_properties": impl}}


def origin_command_line(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[
            *(f"{k}={v}" for k, v in BASE_ITEMS),
            f"set_synth_properties.{SYNTH_NAME}=0",
            f"set_impl_properties.{IMPL_NAME}=Explore",
        ],
    )


def origin_command_line_flows_section(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[
            *(f"{k}={v}" for k, v in BASE_ITEMS),
            f"flows.vivado_synth.set_synth_properties.{SYNTH_NAME}=0",
            f"flows.vivado_synth.set_impl_properties.{IMPL_NAME}=Explore",
        ],
    )


def origin_api_settings(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings={
            **BASE,
            "set_synth_properties": {SYNTH_NAME: 0},
            "set_impl_properties": {IMPL_NAME: "Explore"},
        },
    )


def origin_api_overrides(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[f"{k}={v}" for k, v in BASE_ITEMS],
        flow_overrides={
            "set_synth_properties": {SYNTH_NAME: 0},
            f"set_impl_properties.{IMPL_NAME}": "Explore",
        },
    )


def origin_design_file(tmp_path):
    return planned(
        tmp_path,
        design_from(
            tmp_path,
            design_data(flows_section({SYNTH_NAME: 0}, {IMPL_NAME: "Explore"})),
        ),
    )


def origin_design_file_as_tables(tmp_path):
    """The nested tables that a dotted key stands for are the same setting."""
    return planned(
        tmp_path,
        design_from(
            tmp_path,
            design_data(
                flows_section(
                    {"STEPS": {"SYNTH_DESIGN": {"ARGS": {"MAX_BRAM": 0}}}},
                    {"STEPS": {"OPT_DESIGN": {"ARGS": {"DIRECTIVE": "Explore"}}}},
                )
            ),
        ),
    )


def origin_design_file_dotted_section(tmp_path):
    text = (
        "name: d\nrtl: {sources: [top.v], top: top, clock: clk}\nflows:\n  vivado_synth:\n"
        f"    fpga: {PART}\n    clock.period: 5.5 ns\n"
        f"    set_synth_properties.{SYNTH_NAME}: 0\n"
        f"    set_impl_properties:\n      {IMPL_NAME}: Explore\n"
    )
    return planned(tmp_path, Design.from_file(write_design(tmp_path, text)))


def origin_design_file_dotted_flows_key(tmp_path):
    text = (
        "name: d\nrtl: {sources: [top.v], top: top, clock: clk}\n"
        f"flows.vivado_synth.fpga: {PART}\nflows.vivado_synth.clock.period: 5.5 ns\n"
        f"flows.vivado_synth.set_synth_properties.{SYNTH_NAME}: 0\n"
        f"flows.vivado_synth.set_impl_properties.{IMPL_NAME}: Explore\n"
    )
    return planned(tmp_path, Design.from_file(write_design(tmp_path, text)))


def origin_target(tmp_path):
    data = design_data(
        targets={"board": {"flows": flows_section({SYNTH_NAME: 0}, {IMPL_NAME: "Explore"})}}
    )
    path = write_design(tmp_path, yaml.safe_dump(data, sort_keys=False))
    return planned(tmp_path, path, target="board")


def origin_target_overrides_design(tmp_path):
    data = design_data(
        flows_section({SYNTH_NAME: 1}, {IMPL_NAME: "Default"}),
        targets={"board": {"flows": flows_section({SYNTH_NAME: 0}, {IMPL_NAME: "Explore"})}},
    )
    path = write_design(tmp_path, yaml.safe_dump(data, sort_keys=False))
    return planned(tmp_path, path, target="board")


def origin_project_file(tmp_path):
    project = tmp_path / "xedaproject.yaml"
    project.write_text(
        yaml.safe_dump({"flows": flows_section({SYNTH_NAME: 0}, {IMPL_NAME: "Explore"})})
    )
    # the project's section is the lowest origin; the design adds the device
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        xedaproject=str(project),
    )


def origin_project_file_text(tmp_path):
    project = tmp_path / "xedaproject.yaml"
    project.write_text(
        "flows:\n  vivado_synth:\n"
        f"    fpga: {PART}\n    clock.period: 5.5 ns\n"
        f"    set_synth_properties.{SYNTH_NAME}: 0\n"
        f"    set_impl_properties.{IMPL_NAME}: Explore\n"
    )
    return planned(tmp_path, design_from(tmp_path, design_data()), xedaproject=str(project))


def origin_design_overrides(tmp_path):
    path = write_design(
        tmp_path, yaml.safe_dump(design_data(flows_section({}, {})), sort_keys=False)
    )
    return planned(
        tmp_path,
        path,
        design_overrides=[
            f"flows.vivado_synth.set_synth_properties.{SYNTH_NAME}=0",
            f"flows.vivado_synth.set_impl_properties.{IMPL_NAME}=Explore",
        ],
    )


def origin_design_object(tmp_path):
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.v"], "top": "top", "clock": "clk"},
        flow=flows_section({SYNTH_NAME: 0}, {IMPL_NAME: "Explore"}),
    )
    return planned(tmp_path, design)


ORIGINS = [
    origin_command_line,
    origin_command_line_flows_section,
    origin_api_settings,
    origin_api_overrides,
    origin_design_file,
    origin_design_file_as_tables,
    origin_design_file_dotted_section,
    origin_design_file_dotted_flows_key,
    origin_target,
    origin_target_overrides_design,
    origin_project_file,
    origin_project_file_text,
    origin_design_overrides,
    origin_design_object,
]


@pytest.mark.parametrize("origin", ORIGINS, ids=lambda f: f.__name__.removeprefix("origin_"))
def test_a_property_name_with_dots_is_one_key_from_every_origin(tmp_path, origin):
    assert properties(origin(tmp_path)) == WANTED


def test_a_layer_over_a_layer_replaces_the_property_it_names(tmp_path):
    """The origins stack by key, as for any setting: the command line replaces the design's
    value of a property and leaves the design's other properties."""
    design = design_from(
        tmp_path,
        design_data(
            flows_section({SYNTH_NAME: 1, "STEPS.SYNTH_DESIGN.ARGS.FSM_EXTRACTION": "off"}, {})
        ),
    )
    settings = planned(tmp_path, design, flow_settings=[f"set_synth_properties.{SYNTH_NAME}=0"])
    assert properties(settings)[0] == {
        SYNTH_NAME: "0",
        "STEPS.SYNTH_DESIGN.ARGS.FSM_EXTRACTION": "off",
    }


@pytest.mark.parametrize("flow_class", [VivadoSynth, VivadoAltSynth, VivadoProject])
def test_every_vivado_flow_with_the_dictionaries_declares_them_opaque(flow_class):
    assert opaque_key_fields_of(flow_class.Settings) == OPAQUE
    if flow_class is VivadoAltSynth:  # non-project mode has no run to set a property on
        with pytest.raises(ValueError, match="non-project mode"):
            flow_class.Settings(**BASE, set_synth_properties={SYNTH_NAME: 0})
        return
    settings = flow_class.Settings(**BASE, set_synth_properties={SYNTH_NAME: 0})
    assert settings.set_synth_properties == {SYNTH_NAME: 0}


def test_the_declaration_is_what_makes_the_difference(monkeypatch):
    """The mutation check: without it a dotted key stays the nested table it was expanded to."""
    settings = settings_to_dict([f"set_synth_properties.{SYNTH_NAME}=0", f"fpga={PART}"])
    assert VivadoSynth.Settings.from_input(settings).set_synth_properties == {SYNTH_NAME: "0"}
    monkeypatch.setattr(VivadoSynth.Settings, "opaque_key_fields", ())
    with pytest.raises(FlowSettingsException, match="set_synth_properties"):
        VivadoSynth.Settings.from_input(settings)


def test_a_name_that_is_the_start_of_another_is_refused_within_one_origin():
    """One origin writes a key as a value or as a table, never both: the expansion of dotted
    keys, which does not know the setting, refuses `A` and `A.B` there, as for any setting."""
    with pytest.raises(ConflictingKeys, match="`set_synth_properties.A`"):
        settings_to_dict(["set_synth_properties.A=1", "set_synth_properties.A.B=2"])


QUALIFIED = "flows.vivado_synth.set_synth_properties"

SPELLINGS = {
    "unqualified first": ["set_synth_properties.A=1", f"{QUALIFIED}.A.B=2"],
    "qualified first": [f"{QUALIFIED}.A.B=2", "set_synth_properties.A=1"],
    "a name inside the other, unqualified": ["set_synth_properties.A.B=2", f"{QUALIFIED}.A=1"],
}


@pytest.mark.parametrize("items", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_the_two_spellings_of_the_command_line_are_one_origin_for_a_name_inside_a_name(
    tmp_path, items
):
    """`-s key` and `-s flows.<requested>.key` are one setting, so `A` in one and `A.B` in the
    other are refused as `A` and `A.B` in one spelling are."""
    design = design_from(tmp_path, design_data())
    with pytest.raises(FlowSettingsException, match=r"value, and .* inside it") as caught:
        planned(tmp_path, design, flow_settings=[*(f"{k}={v}" for k, v in BASE_ITEMS), *items])
    assert "set_synth_properties.A" in str(caught.value)


def test_the_api_takes_the_two_spellings_as_one_origin_too(tmp_path):
    design = design_from(tmp_path, design_data())
    with pytest.raises(FlowSettingsException, match=r"value, and .* inside it"):
        planned(
            tmp_path,
            design,
            flow_settings={
                **BASE,
                "set_synth_properties": {"A": 1},
                "flows": {"vivado_synth": {"set_synth_properties": {"A.B": 2}}},
            },
        )


def test_the_command_line_refuses_the_two_spellings_with_a_name_inside_a_name(tmp_path):
    design = tmp_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"},
                "flows": {"vivado_synth": {"fpga": PART, "clock": {"period": "5.5 ns"}}},
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        ["run", "--dry-run", "--json", "vivado_synth", str(design), "-s", *SPELLINGS_CLI],
    )
    assert result.exit_code == 2, result.output
    document = json.loads(result.stdout)
    assert document["outcome"] is None
    assert "inside it" in document["error"]["message"], document


SPELLINGS_CLI = ["set_synth_properties.A=1", f"{QUALIFIED}.A.B=2"]


def test_the_same_name_in_both_spellings_is_one_property(tmp_path):
    """The rule for one setting stays: the same value twice is accepted, two values are not."""
    design = design_from(tmp_path, design_data())
    base = [*(f"{k}={v}" for k, v in BASE_ITEMS)]
    settings = planned(
        tmp_path,
        design,
        flow_settings=[*base, "set_synth_properties.A.B=2", f"{QUALIFIED}.A.B=2"],
    )
    assert prefix_names(settings) == {"A.B": "2"}
    with pytest.raises(FlowSettingsException, match="set one setting to two values"):
        planned(
            tmp_path,
            design,
            flow_settings=[*base, "set_synth_properties.A.B=2", f"{QUALIFIED}.A.B=3"],
        )


def prefix_names(settings) -> dict:
    return {k: str(v) for k, v in settings.set_synth_properties.items()}


def test_names_one_of_which_starts_the_other_are_both_kept_across_origins(tmp_path):
    """Each origin gives its names whole and the origins merge name by name, so a higher
    origin replaces only the names it gives: `A` below and `A.B` above are two properties."""
    design = design_from(tmp_path, design_data(flows_section({"A": 1, SYNTH_NAME: 0}, {})))
    both = {"A": "1", "A.B": "2", SYNTH_NAME: "0"}
    from_command_line = planned(tmp_path, design, flow_settings=["set_synth_properties.A.B=2"])
    assert prefix_names(from_command_line) == both
    from_api = planned(tmp_path, design, flow_overrides={"set_synth_properties": {"A.B": 2}})
    assert prefix_names(from_api) == both
    replaced = planned(tmp_path, design, flow_settings=[f"set_synth_properties.{SYNTH_NAME}=7"])
    assert prefix_names(replaced) == {"A": "1", SYNTH_NAME: "7"}


def test_a_project_a_design_and_a_target_merge_their_names(tmp_path):
    project = tmp_path / "xedaproject.yaml"
    project.write_text(yaml.safe_dump({"flows": flows_section({"A.B": 2}, {})}))
    data = design_data(
        {"vivado_synth": {"set_synth_properties": {"A": 1}}},
        targets={"board": {"flows": {"vivado_synth": {"set_synth_properties": {"A.C": 3}}}}},
    )
    path = write_design(tmp_path, yaml.safe_dump(data, sort_keys=False))
    settings = planned(tmp_path, path, xedaproject=str(project), target="board")
    assert prefix_names(settings) == {"A": "1", "A.B": "2", "A.C": "3"}


def test_a_merged_table_is_stable_when_it_is_merged_again():
    first = merge_layers(
        {"set_synth_properties": {"A": 1}},
        {"set_synth_properties": {"A.B": 2}},
        settings_cls=VivadoSynth.Settings,
    )
    again = merge_layers(
        first, {"set_synth_properties": {"C.D": 3}}, settings_cls=VivadoSynth.Settings
    )
    settings = VivadoSynth.Settings.from_input({**again, **BASE})
    assert settings.set_synth_properties == {"A": 1, "A.B": 2, "C.D": 3}


# ------------------------------------------------- text of a user is never the merged form

ESCAPED = "A\\.B"


def escaped_from_command_line(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[
            *(f"{k}={v}" for k, v in BASE_ITEMS),
            "set_synth_properties.A=1",
            f"set_synth_properties.{ESCAPED}=2",
        ],
    )


def escaped_from_flows_section(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[
            *(f"{k}={v}" for k, v in BASE_ITEMS),
            "set_synth_properties.A=1",
            f"{QUALIFIED}.{ESCAPED}=2",
        ],
    )


def escaped_from_api_settings(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings={**BASE, "set_synth_properties": {"A": 1, ESCAPED: 2}},
    )


def escaped_from_api_overrides(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data()),
        flow_settings=[*(f"{k}={v}" for k, v in BASE_ITEMS)],
        flow_overrides={"set_synth_properties": {"A": 1, ESCAPED: 2}},
    )


def escaped_from_design_file(tmp_path):
    return planned(
        tmp_path,
        design_from(tmp_path, design_data(flows_section({"A": 1, ESCAPED: 2}, {}))),
    )


def escaped_from_target(tmp_path):
    data = design_data(targets={"t": {"flows": flows_section({"A": 1, ESCAPED: 2}, {})}})
    return planned(
        tmp_path, Design.from_file(write_design(tmp_path, yaml.safe_dump(data)), target="t")
    )


ESCAPED_ORIGINS = [
    escaped_from_command_line,
    escaped_from_flows_section,
    escaped_from_api_settings,
    escaped_from_api_overrides,
    escaped_from_design_file,
    escaped_from_target,
]


@pytest.mark.parametrize("origin", ESCAPED_ORIGINS, ids=lambda f: f.__name__)
def test_a_name_written_with_an_escaped_dot_is_refused_from_every_origin(tmp_path, origin):
    """Merged tables keep a dot of a name as `ESCAPED_DOT`, a form `set_hierarchy` does not
    split. The same text in a key a user wrote would be a name `A.B` that no check of `A` and
    `A.B` in one origin saw, so it is refused, naming the key."""
    with pytest.raises(ValueError, match=r"`A\\.B` has a backslash before a dot"):
        origin(tmp_path)


def test_a_name_with_an_escaped_dot_is_refused_by_the_model():
    with pytest.raises(FlowSettingsException, match="backslash before a dot"):
        VivadoSynth.Settings.from_input({**BASE, "set_synth_properties": {ESCAPED: 1}})
    with pytest.raises(ValueError, match="backslash before a dot"):
        VivadoSynth.Settings(**BASE).set_synth_properties = {ESCAPED: 1}


def test_the_command_line_reports_a_name_with_an_escaped_dot(tmp_path):
    design = tmp_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"},
                "flows": {"vivado_synth": {"fpga": PART, "clock": {"period": "5.5 ns"}}},
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--dry-run",
            "--json",
            "vivado_synth",
            str(design),
            "-s",
            "set_synth_properties.A=1",
            f"set_synth_properties.{ESCAPED}=2",
        ],
    )
    assert result.exit_code == 2, result.output
    document = json.loads(result.stdout)
    assert document["outcome"] is None
    assert "backslash before a dot" in document["error"]["message"], document


def test_the_merged_form_is_what_text_cannot_be():
    """A merged table's keys are `AtomicName`s. The same keys as plain text, as a table that was
    written out and read back would hold them, are the refused text."""
    merged = merge_layers(
        {"set_synth_properties": {"A": 1}},
        {"set_synth_properties": {"A.B": 2}},
        settings_cls=VivadoSynth.Settings,
    )
    names = list(merged["set_synth_properties"])
    assert all(isinstance(name, AtomicName) for name in names)
    assert sorted(str(name) for name in names) == ["A", ESCAPED]
    assert VivadoSynth.Settings.from_input({**BASE, **merged}).set_synth_properties == {
        "A": 1,
        "A.B": 2,
    }
    as_text = {"set_synth_properties": {str(name): 0 for name in names}}
    with pytest.raises(ValueError, match="backslash before a dot"):
        merge_layers(as_text, settings_cls=VivadoSynth.Settings)


def test_a_name_inside_another_is_found_in_a_table_the_user_gave_and_not_in_a_merge():
    assert name_inside_another(["A", "B", "A.B"]) == ("A", "A.B")
    assert name_inside_another(["A.B", "A.C", "AB"]) is None
    with pytest.raises(ConflictingKeys, match="`set_synth_properties.A`"):
        _atomic_table("set_synth_properties", {"A": 1, "A.B": 2})
    merged = merge_layers(
        {"set_synth_properties": {"A": 1}},
        {"set_synth_properties": {"A.B": 2}},
        settings_cls=VivadoSynth.Settings,
    )
    assert len(_atomic_table("set_synth_properties", merged["set_synth_properties"])) == 2


# --------------------------------------------------------------------- the setting, as a model


def test_construction_assignment_and_reload_agree():
    constructed = VivadoSynth.Settings(**BASE, set_synth_properties={SYNTH_NAME: 0})
    nested = VivadoSynth.Settings(
        **BASE, set_synth_properties={"STEPS": {"SYNTH_DESIGN": {"ARGS": {"MAX_BRAM": 0}}}}
    )
    assigned = VivadoSynth.Settings(**BASE)
    assigned.set_synth_properties = {"STEPS": {"SYNTH_DESIGN": {"ARGS": {"MAX_BRAM": 0}}}}
    reloaded = VivadoSynth.Settings.model_validate(json.loads(constructed.model_dump_json()))

    assert constructed.set_synth_properties == {SYNTH_NAME: 0}
    assert nested == assigned == reloaded == constructed


def test_a_spaced_name_is_one_key():
    settings = VivadoSynth.Settings(
        **BASE, set_synth_properties={SPACED_NAME: "-flatten_hierarchy none"}
    )
    assert list(settings.set_synth_properties) == [SPACED_NAME]


@pytest.mark.parametrize(
    "given, folded",
    [
        ({"a": {"b": {"c": 1}}}, {"a.b.c": 1}),
        ({"a.b": 1, "c": {"d": 2}}, {"a.b": 1, "c.d": 2}),
        ({"a": {}}, {"a": {}}),  # an empty table is a value, for the type to judge
        ({"a": 1, "b": [1]}, {"a": 1, "b": [1]}),
        ({}, {}),
        ("text", "text"),
        (None, None),
        ([("a", 1)], [("a", 1)]),
    ],
)
def test_folding_joins_the_path_to_each_value(given, folded):
    assert fold_dotted_keys(given) == folded


def test_what_cannot_be_folded_stays_as_given_for_the_type_to_judge():
    twice = {"a.b": 1, "a": {"b": 2}}
    not_text = {"a": {1: 2}}
    assert fold_dotted_keys(twice) is twice
    assert fold_dotted_keys(not_text) is not_text
    for value in (twice, not_text, {"a": {}}, {"a": [1]}):
        with pytest.raises(Exception, match="set_synth_properties"):
            VivadoSynth.Settings(**BASE, set_synth_properties=value)


def test_folding_does_not_change_the_input():
    given = {"a": {"b": 1}}
    VivadoSynth.Settings(**BASE, set_synth_properties=given)
    assert given == {"a": {"b": 1}}


# ------------------------------------------------------------ the declaration is checked early


def test_a_declaration_names_a_setting_that_holds_a_dictionary_of_values():
    typing_ = typing

    with pytest.raises(TypeError, match="`missing`.*opaque_key_fields"):

        class _NoSuchSetting(Flow.Settings):
            opaque_key_fields = ("missing",)

    with pytest.raises(TypeError, match="`count`.*opaque_key_fields"):

        class _NotADictionary(Flow.Settings):
            count: int = 0
            opaque_key_fields = ("count",)

    with pytest.raises(TypeError, match="`groups`.*opaque_key_fields"):

        class _DictionariesOfDictionaries(Flow.Settings):
            groups: typing_.Dict[str, typing_.Dict[str, int]] = {}
            opaque_key_fields = ("groups",)


def test_a_declaration_is_inherited_and_added_to():
    typing_ = typing

    class _Base(Flow.Settings):
        first: typing_.Dict[str, int] = {}
        opaque_key_fields = ("first",)

    class _Derived(_Base):
        second: typing_.Dict[str, int] = {}
        opaque_key_fields = ("second",)

    assert opaque_key_fields_of(_Derived) == ("first", "second")
    assert _Derived(first={"a": {"b": 1}}, second={"c": {"d": 2}}).first == {"a.b": 1}


# ---------------------------------------------- every dictionary setting is decided, in code

#: Each dictionary-typed setting of a flow's settings and of the models nested in them, as
#: `<module>.<class>.<field>`, that does not take dotted names as keys: why a dotted key of it
#: stays a path into nested tables.
ORDINARY_DICTIONARIES = {
    "xeda.flow.synth.SynthFlow.Settings.clocks": "clock names, with tables as values: "
    "`clocks.clk.period` is a path into the clock's table",
    "xeda.flows.dc.Dc.Settings.compile": "suffixes of DC `compile_*` variables, which have no dots",
    "xeda.flows.dc.Dc.Settings.hdlin": "suffixes of DC `hdlin_*` variables, which have no dots",
    "xeda.flows.dc.Dc.Settings.hooks": "stage names, a fixed set without dots",
    "xeda.flows.yosys.common.YosysBase.Settings.defines": "Verilog macro names, which have no dots",
    "xeda.flows.yosys.common.YosysBase.Settings.set_attribute": "attribute name, then an object "
    "path of yosys's selection syntax (separated by `/`): nested by design",
    "xeda.flows.yosys.common.YosysBase.Settings.set_mod_attribute": "attribute name, then a "
    "module name: nested by design",
    "xeda.flows.vivado.vivado_synth.RunOptions.steps": "step names, then `ARGS` and `TCL` tables: "
    "`synth.steps.SYNTH_DESIGN.ARGS.MAX_BRAM` is a path into them by design",
    "xeda.flows.openroad.base.WireRcSettings.layers": "routing layer names, which have no dots",
    "xeda.flows.openroad.base.WireRcSettings.vias": "via names of the technology LEF file, which "
    "have no dots",
    "xeda.flows.openroad.final_groups.IrDropDecap.cells": "decap cell names, which have no dots",
    "xeda.flows.openroad.final_groups.NdrGroup.rules": "names of the rules, which the user picks",
    "xeda.flows.openroad.final_groups.NdrRule.spacing": "routing layer names, which have no dots",
    "xeda.flows.openroad.final_groups.NdrRule.width": "routing layer names, which have no dots",
    "xeda.flows.openroad.place_groups.RoutingGroup.layer_adjustments": "routing layer names or "
    "ranges such as `metal2-metal3`, which have no dots",
    "xeda.flows.openroad.place_pdn.PdnConnect.split_cuts": "routing layer names, which have no "
    "dots",
}


def declaring_class(model: type, name: str) -> type:
    """The class along the MRO of `model` that declares the field `name`."""
    for klass in model.__mro__:
        if name in inspect.get_annotations(klass):
            return klass
    return model


def dictionary_fields() -> dict[str, list[type]]:
    """Every dictionary-typed field of a registered flow's settings and of the models nested in
    them: `<module>.<class>.<field>` of the class that declares it -> every model walked that
    has the field (the declaring class and each subclass of it that a flow uses)."""
    found: dict[str, list[type]] = {}
    visited: set[type] = set()

    def walk_annotation(model: type, name: str, annotation) -> None:
        if annotation_kind(annotation)[0] == "dict":
            owner = declaring_class(model, name)
            found.setdefault(f"{owner.__module__}.{owner.__qualname__}.{name}", []).append(model)
        for argument in typing.get_args(annotation):
            walk_annotation(model, name, argument)
            if isinstance(argument, type) and issubclass(argument, BaseModel):
                walk(argument)

    def walk(model: type[BaseModel]) -> None:
        if model in visited:
            return
        visited.add(model)
        for name, info in model.model_fields.items():
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation)
            walk_annotation(model, name, annotation)

    for _, flow_class in registered_flows.values():
        if flow_class.__module__.startswith("xeda."):
            walk(flow_class.Settings)
    return found


def test_every_dictionary_setting_is_opaque_or_reviewed_as_nested():
    """A setting whose keys are names of a tool that may contain dots declares it. A new
    dictionary setting fails here until it is decided one way or the other."""
    found = dictionary_fields()
    opaque = set()
    for key, models in found.items():
        name = key.rsplit(".", 1)[1]
        declared = [name in opaque_key_fields_of(model) for model in models]
        assert all(declared) or not any(declared), (
            f"{key} is declared opaque by some of the settings that have it and not by "
            f"others: {[m.__qualname__ for m, d in zip(models, declared) if d != declared[0]]}"
        )
        if all(declared):
            opaque.add(key)
    undecided = sorted(set(found) - opaque - set(ORDINARY_DICTIONARIES))
    assert (
        not undecided
    ), f"dictionary settings that are neither in `opaque_key_fields` nor reviewed: {undecided}"
    stale = sorted(set(ORDINARY_DICTIONARIES) - set(found))
    assert not stale, f"reviewed dictionary settings that no longer exist: {stale}"
    both = sorted(opaque & set(ORDINARY_DICTIONARIES))
    assert not both, f"settings that are declared opaque and reviewed as nested: {both}"
    assert opaque == {
        "xeda.flows.vivado.vivado_synth.VivadoSynth.Settings.set_synth_properties",
        "xeda.flows.vivado.vivado_synth.VivadoSynth.Settings.set_impl_properties",
        *(f"xeda.flows.ise.IseSynth.Settings.{step}_options" for step in ISE_STEPS),
        "xeda.flows.openroad.final_groups.IrDropGroup.nets",
        "xeda.flows.openroad.final_groups.IrDropGroup.vsrc_files",
        "xeda.flows.openroad.final_groups.IrDropGroup.instance_power",
        "xeda.flows.openroad.final_groups.NdrGroup.assignments",
    }


# ----------------------------------------------------------- the Tcl gets the name as one word


def run_on_fake_vivado(tmp_path, monkeypatch, settings, flow_class=VivadoSynth):
    use_fake_tools(monkeypatch)
    design = Design.from_file(Path(__file__).parent / "resources" / "design0" / "design0.toml")
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        flow_class, design, settings
    )
    assert flow is not None and flow.succeeded
    return flow


def property_calls(run_dir: Path) -> list[list[str]]:
    """The name and the value of each `set_property NAME VALUE OBJECTS` the fake Vivado
    recorded, each a word of its own (the fake lists the command first)."""
    return [call[1:3] for call in fake_calls(run_dir) if call[:1] == ["set_property"]]


def test_vivado_gets_each_property_name_as_one_word(tmp_path, monkeypatch):
    flow = run_on_fake_vivado(
        tmp_path,
        monkeypatch,
        [
            *(f"{k}={v}" for k, v in BASE_ITEMS),
            f"set_synth_properties.{SYNTH_NAME}=0",
            f"set_impl_properties.{IMPL_NAME}=Explore",
        ],
    )
    calls = property_calls(flow.run_path)
    assert [SYNTH_NAME, "0"] in calls and [IMPL_NAME, "Explore"] in calls, calls
    script = (flow.run_path / "vivado_synth.tcl").read_text()
    assert f'set_property "{SYNTH_NAME}" "0" [get_runs synth_1]' in script, script
    assert f'set_property "{IMPL_NAME}" "Explore" [get_runs impl_1]' in script, script


def test_vivado_gets_a_name_with_a_space_as_one_word(tmp_path, monkeypatch):
    flow = run_on_fake_vivado(
        tmp_path,
        monkeypatch,
        {
            **BASE,
            "set_synth_properties": {SPACED_NAME: "-flatten_hierarchy none"},
        },
    )
    calls = property_calls(flow.run_path)
    assert [SPACED_NAME, "-flatten_hierarchy none"] in calls, calls


def test_the_command_line_runs_vivado_with_the_property(tmp_path, monkeypatch):
    use_fake_tools(monkeypatch)
    design = tmp_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"},
                "flows": {"vivado_synth": {"fpga": PART}},
            }
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--run-root",
            str(tmp_path / "xeda_run"),
            "vivado_synth",
            str(design),
            "-s",
            "clock.period=5.5ns",
            f"set_synth_properties.{SYNTH_NAME}=0",
            f"flows.vivado_synth.set_impl_properties.{IMPL_NAME}=Explore",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["outcome"] == "passed"
    (run_dir,) = (tmp_path / "xeda_run").glob("d/vivado_synth")
    calls = property_calls(run_dir)
    assert [SYNTH_NAME, "0"] in calls and [IMPL_NAME, "Explore"] in calls, calls


def test_a_property_given_on_the_command_line_is_judged_like_one_in_a_file(tmp_path):
    """`out_of_context` refuses a property for the options of `synth_design` without the mode
    (`property_mode_conflicts`): the property reaches that check from the command line now."""
    design = design_from(tmp_path, design_data())
    with pytest.raises(FlowSettingsException, match="set_synth_properties"):
        planned(
            tmp_path,
            design,
            flow_settings=[
                *(f"{k}={v}" for k, v in BASE_ITEMS),
                "out_of_context=true",
                f"set_synth_properties.{SPACED_NAME}=-flatten_hierarchy none",
            ],
        )


@pytest.mark.parametrize("design_first", [True, False], ids=["design-first", "settings-first"])
def test_the_command_line_takes_a_name_with_a_space(tmp_path, monkeypatch, design_first):
    """`-s` keeps a name of the tool whole when it has a space, and still ends at the design."""
    use_fake_tools(monkeypatch)
    design = tmp_path / "d.yaml"
    design.write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top", "clock": "clk"},
                "flows": {"vivado_synth": {"fpga": PART, "clock": {"period": "5.5 ns"}}},
            }
        )
    )
    given = [f"set_synth_properties.{SPACED_NAME}=-flatten_hierarchy none", "out_of_context=false"]
    root = ["--run-root", str(tmp_path / "xeda_run"), "--json"]
    if design_first:
        arguments = ["vivado_synth", str(design), "-s", *given]
    else:
        arguments = ["vivado_synth", "-s", *given, *root[:-1], "--", str(design)]
    result = CliRunner().invoke(cli, ["run", *(root if design_first else []), *arguments])
    assert result.exit_code == 0, result.output
    (run_dir,) = (tmp_path / "xeda_run").glob("d/vivado_synth")
    assert [SPACED_NAME, "-flatten_hierarchy none"] in property_calls(run_dir)
