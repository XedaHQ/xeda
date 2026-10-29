"""Command-line text becomes the value of the type that its setting declares.

A setting takes exactly its declared type from Python and from files (`test_settings_input.py`).
The command line has only text, so text goes through one parser, `parse_setting_text`, chosen by
the annotation of the setting. A layer of `KEY=VALUE` text is the only input that is parsed.
"""

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import pytest
from pydantic import Field

from xeda.dataclass import AtomicName, Code, SettingText, XedaBaseModel
from xeda.design import Design, DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flow_runner.error_codes import settings_codes
from xeda.flow_runner.settings_layers import merge_layers
from xeda.setting_text import SettingTextError, parse_setting_text
from xeda.units import Time
from xeda.utils import settings_to_dict
from .scoping import node_settings

pytestmark = pytest.mark.python_compat


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


def parsed(annotation, text):
    return parse_setting_text(annotation, text, address="x")


def refusal(annotation, text) -> list[tuple[str, str, str]]:
    with pytest.raises(SettingTextError) as error:
        parsed(annotation, text)
    return error.value.problems


# ---------------------------------------------------------------------------------------------
# One leaf of text
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"), [("true", True), ("false", False), ("TRUE", True), ("False", False)]
)
@pytest.mark.parametrize("annotation", [bool, Optional[bool]])
def test_a_boolean_is_true_or_false_in_any_case(annotation, text, expected):
    assert parsed(annotation, text) is expected


@pytest.mark.parametrize("text", ["yes", "on", "1", "0", "y", "", "tru"])
def test_any_other_text_is_no_boolean(text):
    (problem,) = refusal(bool, text)
    assert problem[0] == "x"
    assert problem[2] == "boolean_text"


@pytest.mark.parametrize(("text", "expected"), [("4", 4), ("-3", -3), ("0", 0)])
def test_an_int_is_read_as_a_whole_number(text, expected):
    result = parsed(int, text)
    assert result == expected and type(result) is int


@pytest.mark.parametrize("text", ["4.5", "four", "", "0x10", "[1]"])
def test_text_that_is_no_whole_number_is_an_invalid_value(text):
    (problem,) = refusal(int, text)
    assert problem[0] == "x" and problem[2] == "invalid_value"
    assert text in problem[1]


@pytest.mark.parametrize(("text", "expected"), [("4", 4.0), ("4.5", 4.5), ("-1e3", -1000.0)])
def test_a_float_is_read_as_a_number(text, expected):
    result = parsed(float, text)
    assert result == expected and type(result) is float


def test_text_that_is_no_number_is_an_invalid_value_for_a_float():
    assert [problem[2] for problem in refusal(float, "fast")] == ["invalid_value"]


@pytest.mark.parametrize(
    "annotation",
    [str, Path, Code, Time, Color, Optional[str], Annotated[str, Field(min_length=1)]],
    ids=["str", "Path", "Code", "quantity", "enum", "optional-str", "annotated-str"],
)
def test_text_stays_text_for_a_setting_that_validates_it_itself(annotation):
    result = parsed(annotation, "10 ns")
    assert result == "10 ns" and type(result) is str


def test_text_that_looks_like_a_number_stays_text_for_a_text_setting():
    assert parsed(str, "4") == "4"
    assert parsed(Code, "-2") == "-2"


def test_a_model_or_unknown_type_gets_the_text_to_judge():
    class Model(XedaBaseModel):
        a: int = 1

    assert parsed(Model, "08") == "08"
    assert parsed(Optional[Model], "08") == "08"


def test_a_leaf_that_is_text_is_a_plain_str():
    result = parsed(str, SettingText("abc"))
    assert type(result) is str


@pytest.mark.parametrize(
    ("annotation", "text", "expected"),
    [
        (Literal["random"], "random", "random"),
        (Literal[1, 2], "2", 2),
        (Literal["a", "b"], "c", "c"),  # not a member: the model reports it
        (Literal["random"] | Annotated[int, Field(ge=1)], "random", "random"),
        (Literal["random"] | Annotated[int, Field(ge=1)], "7", 7),
        (Union[bool, int, str], "true", True),
        (Union[bool, int, str], "3", 3),
        (Union[bool, int, str], "three", "three"),
        (Union[str, int, float, bool], "3", 3),
        (Union[str, int, float, bool], "3.5", 3.5),
        (Union[str, int, float, bool], "none", "none"),
        (Union[int, float], "3.5", 3.5),
        (Union[str, Path], "a/b", "a/b"),
        (Optional[int], "5", 5),
    ],
)
def test_a_union_takes_the_first_branch_that_reads_the_text(annotation, text, expected):
    result = parsed(annotation, text)
    assert result == expected and type(result) is type(expected)


def test_a_union_without_a_text_branch_refuses_text_no_branch_reads():
    assert [problem[2] for problem in refusal(Literal["random"] | int, "often")] == [
        "invalid_value"
    ]


@pytest.mark.parametrize(
    ("annotation", "text", "expected"),
    [
        (list[str], "a.xdc,b.xdc", ["a.xdc", "b.xdc"]),
        (list[str], "a.xdc, b.xdc ,", ["a.xdc", "b.xdc"]),
        (list[str], "", []),
        (list[str], "single", ["single"]),
        (list[Path | str], "a,b", ["a", "b"]),
        (list[Annotated[int, Field(ge=1)]], "10,20", [10, 20]),
        (Optional[list[int]], "1", [1]),
        (tuple[int, ...], "1,2", (1, 2)),
        (tuple[str, ...], "a,b", ("a", "b")),
        (list[bool], "true,false", [True, False]),
    ],
)
def test_a_list_is_split_at_commas_and_each_item_is_read_as_its_own_type(
    annotation, text, expected
):
    result = parsed(annotation, text)
    assert result == expected and type(result) is type(expected)


def test_a_tuple_of_fixed_length_takes_that_many_items_of_their_own_types():
    pair = tuple[float, float]
    assert parsed(pair, "0.5, 2") == (0.5, 2.0)
    assert [problem[2] for problem in refusal(pair, "0.5")] == ["invalid_value"]
    assert [problem[2] for problem in refusal(pair, "0.5,1,2")] == ["invalid_value"]
    assert [problem[2] for problem in refusal(pair, "0.5,x")] == ["invalid_value"]


@pytest.mark.parametrize("text", ["[]", "[a,b]", " [x] "])
def test_text_spelled_as_a_list_is_refused(text):
    (problem,) = refusal(list[str], text)
    assert problem[2] == "list_text"
    assert "x=" in problem[1]


def test_an_item_that_is_no_value_of_its_type_is_refused():
    assert [problem[2] for problem in refusal(list[int], "1,two")] == ["invalid_value"]


# ---------------------------------------------------------------------------------------------
# A layer of text, read against the model of the setting
# ---------------------------------------------------------------------------------------------


def settings_of(flow: str):
    return get_flow_class(flow).Settings


def merged(flow: str, text: str):
    return merge_layers([text], settings_cls=settings_of(flow))


@pytest.mark.parametrize(
    ("flow", "text", "expected"),
    [
        ("nextpnr", "seed=4", {"seed": 4}),
        ("nextpnr", "timing_allow_fail=false", {"timing_allow_fail": False}),
        ("vivado_synth", "fpga.part=xc7a35tcpg236-1", {"fpga": {"part": "xc7a35tcpg236-1"}}),
        (
            "vivado_synth",
            "clocks.main_clock.period=10 ns",
            {"clocks": {"main_clock": {"period": "10 ns"}}},
        ),
        ("vivado_synth", "xdc_files=a.xdc,b.xdc", {"xdc_files": ["a.xdc", "b.xdc"]}),
        ("verilator", "random_seed=random", {"random_seed": "random"}),
        ("verilator", "random_seed=7", {"random_seed": 7}),
        ("vivado_synth", "set_synth_properties.A=0", {"set_synth_properties": {"A": 0}}),
        ("vivado_synth", "set_synth_properties.A=none", {"set_synth_properties": {"A": "none"}}),
        (
            "vivado_synth",
            "set_synth_properties.A.B=true",
            {"set_synth_properties": {AtomicName.of("A.B"): True}},
        ),
    ],
)
def test_cli_text_becomes_its_declared_type(flow, text, expected):
    result = merged(flow, text)
    assert result == expected
    leaf = result
    while isinstance(leaf, dict):
        (leaf,) = leaf.values()
    assert type(leaf) is not SettingText


class Group(XedaBaseModel):
    count: int = 0
    label: str = ""


class Table(XedaBaseModel):
    env: dict[str, str] = {}
    counts: dict[str, int] = {}
    groups: dict[str, Group] = {}
    group: Group = Group()


def test_the_value_of_a_dictionary_is_read_as_the_type_of_its_entries():
    layer = merge_layers(
        ["env.NUM_TV=8", "counts.a=3", "groups.x.count=2", "groups.x.label=1", "group.count=5"],
        settings_cls=Table,
    )
    assert layer == {
        "env": {"NUM_TV": "8"},
        "counts": {"a": 3},
        "groups": {"x": {"count": 2, "label": "1"}},
        "group": {"count": 5},
    }


def test_a_model_that_is_no_flow_raises_the_text_error_itself():
    with pytest.raises(SettingTextError) as error:
        merge_layers(["counts.a=x"], settings_cls=Table)
    assert error.value.problems[0][0] == "counts.a"


def test_a_key_that_names_no_setting_keeps_its_text_for_validation_to_refuse():
    result = merged("nextpnr", "no_such_setting=4")
    assert result == {"no_such_setting": "4"}
    with pytest.raises(FlowSettingsError) as error:
        settings_of("nextpnr").from_input(result)
    assert settings_codes(error.value) == ["unknown_setting"]


def test_a_problem_names_the_address_the_user_wrote():
    with pytest.raises(FlowSettingsError) as error:
        merged("nextpnr", "seed=four")
    assert settings_codes(error.value) == ["invalid_value"]
    assert error.value.errors[0][0] == "seed"
    with pytest.raises(FlowSettingsError) as error:
        merged("nextpnr", "timing_allow_fail=yes")
    assert settings_codes(error.value) == ["boolean_text"]


def test_every_problem_of_a_layer_is_reported_at_once():
    with pytest.raises(FlowSettingsError) as error:
        merge_layers(["seed=four", "timing_allow_fail=yes"], settings_cls=settings_of("nextpnr"))
    assert sorted(location for location, *_ in error.value.errors) == [
        "seed",
        "timing_allow_fail",
    ]


def test_list_text_in_brackets_is_refused_from_the_cli():
    with pytest.raises(FlowSettingsError) as error:
        merged("vivado_synth", "xdc_files=[a.xdc]")
    assert settings_codes(error.value) == ["list_text"]
    assert error.value.errors[0][0] == "xdc_files"


def test_only_text_is_read_by_the_annotation_and_a_value_is_left_alone():
    flow = settings_of("nextpnr")
    assert merge_layers({"seed": 4}, settings_cls=flow) == {"seed": 4}
    assert merge_layers({"seed": "4"}, settings_cls=flow) == {"seed": "4"}
    again = merge_layers(["seed=4"], settings_cls=flow)
    assert merge_layers(again, ["timing_allow_fail=true"], settings_cls=flow) == {
        "seed": 4,
        "timing_allow_fail": True,
    }


def test_text_without_a_model_stays_text_until_a_merge_has_one():
    below = merge_layers(["seed=4"])
    assert below == {"seed": "4"}
    assert merge_layers(below, settings_cls=settings_of("nextpnr")) == {"seed": 4}


# ---------------------------------------------------------------------------------------------
# The launcher: text on the command line, values from Python
# ---------------------------------------------------------------------------------------------


def write_design(directory: Path) -> Path:
    (directory / "top.v").write_text("module top; endmodule\n")
    design = directory / "top.yaml"
    design.write_text(
        "name: top\nrtl:\n  sources: [top.v]\n  top: top\n",
    )
    return design


def plan(tmp_path: Path, **keywords):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    return runner.plan("verilator", write_design(tmp_path), **keywords)


def test_the_command_line_text_for_an_int_setting_is_an_int(tmp_path):
    result = plan(tmp_path, flow_settings=node_settings("verilator", ["random_seed=7"]))
    assert result.nodes[0].settings.random_seed == 7


def test_the_text_of_the_design_updates_is_read_against_the_design(tmp_path):
    design = Design.load(write_design(tmp_path))
    updated = design.with_updates(
        settings_to_dict(["language.vhdl.synopsys=true", "rtl.top=other"])
    )
    assert updated.language["vhdl"]["synopsys"] is True
    assert updated.rtl.top == "other"


def test_a_design_update_that_is_no_boolean_is_refused(tmp_path):
    design = Design.load(write_design(tmp_path))
    with pytest.raises(DesignValidationError) as error:
        design.with_updates(settings_to_dict(["language.vhdl.synopsys=yes"]))
    assert "language.vhdl.synopsys" in str(error.value)


def test_a_design_update_that_is_no_number_names_the_setting(tmp_path):
    design = Design.load(write_design(tmp_path))
    with pytest.raises(DesignValidationError) as error:
        design.with_updates(settings_to_dict(["language.vhdl.synopsys=maybe"]))
    assert [item[0] for item in error.value.errors] == ["language.vhdl.synopsys"]


def test_the_text_of_the_exploration_settings_is_read_as_their_types():
    from xeda.flow_runner.dse.dse_runner import Dse

    optimizer = settings_to_dict(["init_freq_low=50", "init_freq_high=100"])
    explorer = Dse(
        "fmax_optimizer",
        optimizer_settings=optimizer,
        **settings_to_dict(["max_failed_iters=2", "keep_optimal_run_dirs=true"]),
    )
    assert explorer.settings.max_failed_iters == 2
    assert explorer.settings.keep_optimal_run_dirs is True
    with pytest.raises(FlowSettingsError) as error:
        Dse("fmax_optimizer", optimizer_settings=optimizer, **settings_to_dict(["timeout=soon"]))
    assert settings_codes(error.value) == ["invalid_value"]
    assert error.value.errors[0][0] == "timeout"


@pytest.mark.parametrize("text", ["1_0", "+5", "٣", " 4", "4 "])
def test_whole_number_text_is_digits_with_an_optional_minus(text):
    (problem,) = refusal(int, text)
    assert problem[2] == "invalid_value"


@pytest.mark.parametrize("text", ["nan", "inf", "-inf", "1_0.5", "NaN"])
def test_number_text_is_finite_and_plain(text):
    (problem,) = refusal(float, text)
    assert problem[2] == "invalid_value"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10", 10),
        ("-10", -10),
        ("1.5", 1.5),
        ("010", "010"),
        ("1e3", "1e3"),
        ("+5", "+5"),
        ("1.50", "1.50"),
        ("0", 0),
        ("-0", "-0"),
        ("nan", "nan"),
    ],
)
def test_text_beside_numbers_becomes_a_number_only_when_it_reads_back_the_same(text, expected):
    result = parsed(Union[str, int, float, bool], text)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize(("text", "expected"), [("010", "010"), ("10", 10), ("1e3", "1e3")])
def test_a_property_value_keeps_its_text_unless_it_is_a_plain_number(text, expected):
    layer = merge_layers(
        [f"set_synth_properties.A={text}"], settings_cls=settings_of("vivado_synth")
    )
    value = layer["set_synth_properties"]["A"]
    assert value == expected and type(value) is type(expected)
