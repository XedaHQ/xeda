"""What a setting accepts: exactly its declared type, plus two documented flow conveniences.

There is no implicit conversion between types -- a number is not text, text is not a list, `True`
is not a name -- so the type a setting declares is the whole truth about what it takes, and a
mismatch is reported as a validation error naming the setting. Where a setting's values are
genuinely of several kinds (a Vivado run property is text, a number or a boolean), its type says
so and its consumer renders each kind explicitly.

On top of that, every *flow* setting gets the conveniences of `Flow.Settings._normalize_flow_setting`:
a list setting may be written as a comma-separated string (`-s xdc_files=a.xdc,b.xdc`), and
`$DESIGN_ROOT`/`$DESIGN_DIR`/`$PWD` are expanded at each path.
"""

import copy
from collections import namedtuple
from pathlib import Path

import pytest

from xeda.dataclass import ValidationError
from xeda.design import Design, DesignValidationError
from xeda.flow import Flow, FlowSettingsError

# ---------------------------------------------------------------------------------------------
# No implicit conversion
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "setting"),
    [
        (lambda: Design(name=2024, rtl={"sources": [], "top": "t"}), "name"),
        (lambda: Design(name=True, rtl={"sources": [], "top": "t"}), "name"),
        (lambda: _flow("verilator").Settings(compile_args=["-j", 8]), "compile_args"),
        (
            lambda: _flow("vivado_synth").Settings(fpga="xc7a100tftg256-2L", suppress_msgs=[8]),
            "suppress_msgs",
        ),
    ],
    ids=[
        "design-name-number",
        "design-name-bool",
        "tool-arg-number",
        "msg-id-number",
    ],
)
def test_a_value_of_another_type_is_a_validation_error_naming_the_setting(build, setting):
    """`speed = 2` must be written `speed = "2"`: a speed grade is text (`"-2L"`), and quietly
    turning numbers into text would have to decide what `True` or `2.5` means as well."""
    with pytest.raises((ValidationError, DesignValidationError, FlowSettingsError)) as error:
        build()
    assert setting in str(error.value)


def test_a_code_may_be_written_as_a_number():
    """A speed grade or device generation is text (`"-2L"`), but naturally written as a number
    (`speed = -1`); the FPGA declares those fields as `Code`, which accepts both. No other
    setting turns a number into text."""
    from xeda.flow.fpga import FPGA

    fpga = FPGA(part="xc7a100t", speed=-1, grade=2, generation=7)
    assert (fpga.speed, fpga.grade, fpga.generation) == ("-1", "2", "7")
    with pytest.raises(ValidationError):
        FPGA(part="xc7a100t", speed=True)


def test_a_code_advertises_every_accepted_input_type():
    from xeda.flow.fpga import FPGA

    speed = FPGA.model_json_schema()["properties"]["speed"]
    types = {
        branch.get("type")
        for branch in speed["anyOf"]
        if isinstance(branch, dict) and branch.get("type") != "null"
    }
    assert types == {"string", "integer", "number"}


def test_a_union_keeps_the_type_it_was_given():
    """`stop_time` takes a number or text with a unit; neither is turned into the other."""
    from xeda.flow.sim import SimFlow

    assert SimFlow.Settings(stop_time=5).stop_time == 5
    assert SimFlow.Settings(stop_time="5us").stop_time == "5us"


def test_fractional_platform_values_are_not_truncated():
    """Physical quantities from PDK data are `float`: an `int` type would truncate them."""
    from xeda.platforms.asics import AsicsPlatform

    platform = AsicsPlatform.from_resource("nangate45")
    assert platform.macro_place_halo == [22.4, 15.12]


def test_vivado_run_properties_take_text_numbers_and_booleans():
    """A run property's type says what Vivado accepts, and each kind is rendered for Tcl."""
    from xeda.flows.vivado.vivado_synth import VivadoSynth, tcl_property_value

    properties = {"STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM": 0, "A": 1.5, "B": True, "C": "none"}
    settings = VivadoSynth.Settings(fpga="xc7a100tftg256-2L", set_synth_properties=properties)

    assert settings.set_synth_properties == properties
    assert [tcl_property_value(v) for v in properties.values()] == ["0", "1.5", "true", "none"]


def _flow(name):
    from xeda.flow_runner import get_flow_class

    return get_flow_class(name)


# ---------------------------------------------------------------------------------------------
# Comma-separated list settings
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flow", "extra", "setting", "given", "expected"),
    [
        ("verilator", {}, "compile_args", "-j,8", ["-j", "8"]),
        ("verilator", {}, "compile_args", "", []),
        # an *optional* list splits the same way
        ("yosys", {}, "liberty", "a.lib,b.lib", [Path("a.lib"), Path("b.lib")]),
        # spaces around items and empty items are dropped
        ("yosys", {}, "gates", " AND, OR,,", ["AND", "OR"]),
        # a setting that also accepts plain text keeps the text whole
        ("ghdl_sim", {}, "vpi", "a,b", "a,b"),
    ],
)
def test_a_list_setting_may_be_given_as_comma_separated_text(flow, extra, setting, given, expected):
    settings = _flow(flow).Settings(**extra, **{setting: given})
    assert getattr(settings, setting) == expected

    assigned = _flow(flow).Settings(**extra)
    setattr(assigned, setting, given)
    assert getattr(assigned, setting) == expected, "assignment must behave like construction"


@pytest.mark.parametrize("given", ["[]", "[a,b]", "[ x ]", " [] "], ids=repr)
def test_a_list_setting_written_as_a_list_literal_is_refused_naming_the_right_spellings(given):
    """`-s compile_args=[]` is text, and the text `[]` is not a list: refused, instead of becoming
    the one-item list `["[]"]`. The message names the spelling of the empty list and of a list."""
    with pytest.raises(FlowSettingsError) as refused:
        _flow("verilator").Settings.from_input({"compile_args": given})
    message = str(refused.value)
    assert "compile_args" in message and "is text, not a list" in message
    assert "`compile_args=` for the empty list" in message
    assert "`compile_args=a,b`" in message

    assigned = _flow("verilator").Settings()
    with pytest.raises(ValidationError, match="is text, not a list"):
        assigned.compile_args = given  # type: ignore[assignment]
    assert assigned.compile_args == [], "a refused assignment changes nothing"


def test_every_list_setting_written_as_a_list_literal_is_refused_alike_everywhere():
    """The rule is the settings' own, so every list setting of every flow has it, wherever it is
    written: construction and assignment agree (see also `test_model_invariants`)."""
    from xeda.flow_runner import get_flow_class
    from xeda.flow.flow import _is_comma_separated_list

    from .settings_samples import flow_classes, minimal_settings

    unrefused = []
    for cls, _name in flow_classes():
        for name, info in cls.Settings.model_fields.items():
            if name.endswith("_") or not _is_comma_separated_list(info.annotation):
                continue
            try:
                cls.Settings.from_input({**minimal_settings(cls), name: "[]"})
            except FlowSettingsError:
                continue
            unrefused.append(f"{cls.name}.{name}")
    assert not unrefused, unrefused
    assert get_flow_class("verilator")  # the sweep found list settings to judge


def _nested_list_fields() -> "list[tuple[type, str]]":
    """Every list field of every model nested in a flow's settings (`cocotb`, `platform`,
    `cxxrtl`, `ghdl`, ...), as `(model, field)`, each once, found by walking the annotations."""
    import types
    from typing import Annotated, Union, get_args, get_origin

    from xeda.dataclass import XedaBaseModel

    from .settings_samples import flow_classes

    def models_in(annotation):
        if get_origin(annotation) is Annotated:
            yield from models_in(get_args(annotation)[0])
        elif isinstance(annotation, type) and issubclass(annotation, XedaBaseModel):
            yield annotation
        else:
            for arg in get_args(annotation):
                yield from models_in(arg)

    def is_list(annotation):
        origin = get_origin(annotation)
        if origin is Annotated:
            return is_list(get_args(annotation)[0])
        if origin in (Union, types.UnionType):
            return any(is_list(arg) for arg in get_args(annotation))
        return origin is list

    found: "dict[tuple[type, str], None]" = {}
    visited: "set[type]" = set()

    def walk(model, nested):
        if model in visited:
            return
        visited.add(model)
        for name, info in model.model_fields.items():
            if name.endswith("_"):
                continue
            if nested and is_list(info.annotation):
                found[(model, name)] = None
            for sub in models_in(info.annotation):
                walk(sub, True)

    for cls, _name in flow_classes():
        walk(cls.Settings, False)
    return sorted(found, key=lambda pair: (pair[0].__qualname__, pair[1]))


def _nested_base(model) -> dict:
    """What `model` needs besides the field under test. A model that cannot be built from its
    defaults gets its base here: a new one fails the sweep below until it has one."""
    from xeda.platforms.asics import AsicsPlatform

    if model is AsicsPlatform:
        return AsicsPlatform.from_setting("asap7").model_dump()
    return {}


NESTED_LIST_FIELDS = _nested_list_fields()


def test_the_sweep_finds_the_nested_models_with_list_fields():
    models = {model.__name__ for model, _ in NESTED_LIST_FIELDS}

    assert {"CocotbSettings", "AsicsPlatform", "CxxRtl"} <= models


@pytest.mark.parametrize(
    ("model", "field"),
    NESTED_LIST_FIELDS,
    ids=[f"{model.__qualname__}.{field}" for model, field in NESTED_LIST_FIELDS],
)
def test_a_nested_model_takes_text_for_a_list_by_the_one_rule_or_not_at_all(model, field):
    """A list field of a model nested in a flow's settings either refuses text (a list is written
    as a list) or takes it as the flow's own settings do: comma separated, empty items dropped,
    and text spelled `[...]` refused. A second rule for text, kept beside the first, gave
    `cocotb.testcase=[]` the one item `[]`."""
    base = _nested_base(model)

    def built(text):
        try:
            return getattr(model(**{**base, field: text}), field)
        except ValidationError as e:
            assert any(
                error["loc"][:1] == (field,) for error in e.errors()
            ), f"{model.__qualname__}({field}={text!r}) fails for another reason: {e}"
            return REFUSED

    REFUSED = object()
    taken = built("a,b")
    if taken is REFUSED or isinstance(taken, str) or [str(item) for item in taken] == ["a,b"]:
        return  # no convenience: text is refused, or it is one item of the field's own
    assert taken == ["a", "b"], taken
    assert built(" a , b ,,") == ["a", "b"]
    assert built("") == []
    assert built("[]") is REFUSED
    assert built("[x, y]") is REFUSED


def test_cocotb_takes_its_lists_as_text_by_the_one_rule():
    from xeda.cocotb import CocotbSettings

    assert CocotbSettings(testcase="a, b,,").testcase == ["a", "b"]
    assert CocotbSettings(testcase="").testcase == []
    assert CocotbSettings(gpi_extra=" lib.so ").gpi_extra == ["lib.so"]
    with pytest.raises(ValidationError, match="`testcase=` for the empty list") as refused:
        CocotbSettings(testcase="[]")
    assert [error["loc"] for error in refused.value.errors()] == [("testcase",)]
    with pytest.raises(ValidationError, match="`gpi_extra=a,b`"):
        CocotbSettings(gpi_extra="[a,b]")


def test_cocotb_text_for_a_list_is_refused_the_same_through_a_flow():
    with pytest.raises(FlowSettingsError, match="is text, not a list"):
        _flow("ghdl_sim").Settings.from_input({"cocotb": {"testcase": "[]"}})


@pytest.mark.parametrize(
    ("flow", "setting", "section", "key"),
    [
        ("ghdl_sim", {"cocotb": {"testcase": "[]"}}, "cocotb", "testcase"),
        ("yosys", {"ghdl": {"compiler_flags": "[a]"}}, "ghdl", "compiler_flags"),
    ],
    ids=["cocotb", "nested flow settings"],
)
def test_the_message_for_a_nested_list_names_the_key_as_the_command_line_writes_it(
    flow, setting, section, key
):
    """The spellings a message gives are the ones to write after `-s`: with the section."""
    with pytest.raises(FlowSettingsError) as refused:
        _flow(flow).Settings.from_input(setting)

    ((location, message, _, kind),) = refused.value.errors
    assert location == f"{section} -> {key}" and kind == "list_text"
    assert f"write `{section}.{key}=` for the empty list and `{section}.{key}=a,b`" in message


def test_a_setting_that_also_accepts_text_keeps_a_list_literal_as_text():
    """`ghdl_sim.vpi` takes text or a list: the text is its own, whatever it looks like."""
    assert _flow("ghdl_sim").Settings(vpi="[a]").vpi == "[a]"


@pytest.mark.parametrize("given", ["-DX=[1],-y", "a[0],b[1]", "[a", "a]", "x,[]"], ids=repr)
def test_brackets_inside_a_list_item_are_just_text(given):
    """Only a text spelled as a whole list literal is refused."""
    settings = _flow("verilator").Settings(compile_args=given)
    assert settings.compile_args == [item for item in given.split(",") if item]


# ---------------------------------------------------------------------------------------------
# Path variables
# ---------------------------------------------------------------------------------------------


def test_path_placeholders_expand_recursively_without_rewriting_non_path_strings(tmp_path):
    """Path expansion follows the annotation through lists, mappings, tuples, and unions."""

    class PathSettings(Flow.Settings):
        scalar: Path = Path()
        paths: list[Path] = []
        hooks: dict[str, Path | None] = {}
        libraries: list[tuple[str, str | Path]] = []

    payload = {
        "scalar": "$DESIGN_ROOT/scalar.sdc",
        "paths": ["$DESIGN_ROOT/a.sdc", "$DESIGN_DIR/b.sdc"],
        "hooks": {"pre": "$DESIGN_ROOT/pre.tcl", "post": None},
        "libraries": [("$LIBRARY_NAME", "$DESIGN_ROOT/lib/cells.lib")],
    }
    before = copy.deepcopy(payload)

    settings = PathSettings.from_input(payload, design_root=tmp_path)

    assert payload == before
    assert settings.scalar == tmp_path / "scalar.sdc"
    assert settings.paths == [tmp_path / "a.sdc", tmp_path / "b.sdc"]
    assert settings.hooks == {"pre": tmp_path / "pre.tcl", "post": None}
    assert settings.libraries == [("$LIBRARY_NAME", tmp_path / "lib/cells.lib")]


def test_path_expansion_accepts_namedtuples_for_tuple_fields(tmp_path):
    """Rebuilding a tuple as `type(value)(items)` breaks on a namedtuple, whose constructor takes
    its fields positionally; the resulting TypeError surfaced as a validation error."""
    Pair = namedtuple("Pair", "a b")
    Lib = namedtuple("Lib", "name path")

    class TupleSettings(Flow.Settings):
        pair: tuple[int, int] = (0, 0)
        lib: tuple[str, Path] = ("", Path())

    settings = TupleSettings.from_input(
        {"pair": Pair(1, 2), "lib": Lib("$NAME", "$DESIGN_ROOT/cells.lib")}, design_root=tmp_path
    )

    assert settings.pair == (1, 2)
    assert settings.lib == ("$NAME", tmp_path / "cells.lib")


def test_real_path_list_setting_expands_design_root(tmp_path):
    from xeda.flows.dc import Dc

    settings = Dc.Settings.from_input(
        {"platform": "asap7", "target_libraries": ["$DESIGN_ROOT/lib/cells.lib"]},
        design_root=tmp_path,
    )

    assert settings.target_libraries == [tmp_path / "lib/cells.lib"]


@pytest.mark.parametrize(
    ("flow", "extra", "field", "value", "expected"),
    [
        # `str | Path` unions
        ("ghdl_sim", {}, "vcd", "$DESIGN_ROOT/w.vcd", "{root}/w.vcd"),
        ("verilator", {}, "fst", "$DESIGN_ROOT/w.fst", "{root}/w.fst"),
        # lists of paths, and of `str | Path`
        ("nvc", {}, "vhpi", ["$DESIGN_ROOT/v.so"], ["{root}/v.so"]),
        (
            "vivado_synth",
            {"fpga": {"part": "xc7a100tftg256-2L"}},
            "xdc_files",
            ["$DESIGN_ROOT/a.xdc"],
            ["{root}/a.xdc"],
        ),
        # a mapping's values
        (
            "dc",
            {"platform": "asap7", "target_libraries": []},
            "hooks",
            {"pre_elab": "$DESIGN_ROOT/pre.tcl", "post_elab": None},
            {"pre_elab": "{root}/pre.tcl", "post_elab": None},
        ),
        # only the path half of a `lib_paths` entry; the library name is not a path
        ("ghdl_sim", {}, "lib_paths", [("$NAME", "$DESIGN_ROOT/lib")], [("$NAME", "{root}/lib")]),
    ],
)
def test_real_settings_expand_design_root_at_every_path_leaf(
    tmp_path, flow, extra, field, value, expected
):
    """The fields the changelog names, on the flows that declare them."""
    from xeda.flow_runner import get_flow_class

    def at_root(item):
        if isinstance(item, str):
            return Path(item.format(root=tmp_path)) if "{root}" in item else item
        if isinstance(item, list):
            return [at_root(i) for i in item]
        if isinstance(item, tuple):
            return tuple(at_root(i) for i in item)
        if isinstance(item, dict):
            return {k: at_root(v) for k, v in item.items()}
        return item

    settings = get_flow_class(flow).Settings.from_input(
        {**extra, field: value}, design_root=tmp_path
    )

    assert getattr(settings, field) == at_root(expected)


def test_comma_separated_path_list_expands_each_design_root(tmp_path):
    from xeda.flows.dc import Dc

    settings = Dc.Settings.from_input(
        {"platform": "asap7", "target_libraries": "$DESIGN_ROOT/lib/a.lib,$DESIGN_ROOT/lib/b.lib"},
        design_root=tmp_path,
    )

    assert settings.target_libraries == [
        tmp_path / "lib/a.lib",
        tmp_path / "lib/b.lib",
    ]


def test_direct_flow_construction_attaches_missing_settings_context(tmp_path):
    from xeda.flows import VivadoSynth

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "top"})
    start = tmp_path / "start"
    flow = VivadoSynth(
        VivadoSynth.Settings(
            fpga="xc7a100t",
            xdc_files=["$DESIGN_ROOT/already-present.xdc"],
        ),
        design,
        tmp_path / "run",
        runner_cwd=start,
    )

    assert flow.settings.xdc_files == [tmp_path / "already-present.xdc"]
    flow.settings.xdc_files = ["$DESIGN_ROOT/clock.xdc", "$PWD/hook.xdc"]
    assert flow.settings.xdc_files == [tmp_path / "clock.xdc", start / "hook.xdc"]


def test_runner_preserves_edits_inside_a_producer_settings_object(tmp_path):
    from xeda.flow_runner import DefaultRunner
    from xeda.flows import VivadoSynth

    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "top"})
    settings = VivadoSynth.Settings(fpga="xc7a100t")
    settings.xdc_files = ["$DESIGN_ROOT/pins.xdc"]

    validated = DefaultRunner(tmp_path / "run")._input_settings(
        VivadoSynth, settings, design, tmp_path / "start"
    )

    assert validated.xdc_files == [tmp_path / "pins.xdc"]
