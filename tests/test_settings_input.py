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

from xeda.dataclass import ValidationError, waveform_format_of
from xeda.design import Design, DesignValidationError
from xeda.flow import Flow, FlowSettingsError

pytestmark = pytest.mark.python_compat


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


@pytest.mark.parametrize("given", ["lots", "", "2.5", 2.5, ["2"], {"level": 2}], ids=repr)
def test_verbose_takes_a_whole_number_and_refuses_everything_else(given):
    """`verbose: lots` used to become 0, and `verbose: 2.5` became 2, without a word."""
    with pytest.raises(FlowSettingsError, match="verbose"):
        Flow.Settings.from_input({"verbose": given})
    with pytest.raises(ValidationError) as refused:
        Flow.Settings(verbose=given)
    assert [error["loc"] for error in refused.value.errors()] == [("verbose",)]

    settings = Flow.Settings(verbose=2)
    with pytest.raises(ValidationError, match="verbose"):
        settings.verbose = given
    assert settings.verbose == 2, "a refused assignment changes nothing"


@pytest.mark.parametrize("given", [2, "2"], ids=repr)
def test_verbose_takes_a_number_as_it_is_written_in_a_file_or_on_the_command_line(given):
    assert Flow.Settings.from_input({"verbose": given}).verbose == 2


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
    """Every list field of every model nested in a flow's settings (`cocotb`,
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
    return {}


NESTED_LIST_FIELDS = _nested_list_fields()


def test_the_sweep_finds_the_nested_models_with_list_fields():
    models = {model.__name__ for model, _ in NESTED_LIST_FIELDS}

    assert {"CocotbSettings", "CxxRtl"} <= models


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
    assert [str(item) for item in taken] == ["a", "b"], taken
    assert [str(item) for item in built(" a , b ,,")] == ["a", "b"]
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
        ("yosys", {"ghdl": {"analysis_flags": "[a]"}}, "ghdl", "analysis_flags"),
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
        {"target_libraries": ["$DESIGN_ROOT/lib/cells.lib"]},
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
            {"target_libraries": []},
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
        {"target_libraries": "$DESIGN_ROOT/lib/a.lib,$DESIGN_ROOT/lib/b.lib"},
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
    from xeda.flow_runner.launch_input import input_settings
    from xeda.flows import VivadoSynth

    (tmp_path / "top.v").write_text("module top; endmodule\n")  # `vivado_synth` reads a source
    design = Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    settings = VivadoSynth.Settings(fpga="xc7a100t")
    settings.xdc_files = ["$DESIGN_ROOT/pins.xdc"]

    validated = input_settings(VivadoSynth, settings, design, tmp_path / "start")

    assert validated.xdc_files == [tmp_path / "pins.xdc"]


# ---------------------------------------------------------------------------------------------
# A scalar setting takes a value of its type or fails naming the setting
# ---------------------------------------------------------------------------------------------


def _scalar_kinds(annotation) -> "tuple[set[str], set[object]] | None":
    """Return what a field of a plain type holds, as its kinds (`int`, `float`, `bool`, `str`,
    `path`, `literal`) and the values of its `Literal`s. Return `None` for any other field: a
    list, a table, a model, a quantity, a choice among names, or a type with a converter of
    its own (`Code`, which takes a number for text)."""
    import types
    from pathlib import PurePath
    from typing import Annotated, Literal, Union, get_args, get_origin

    from pydantic import BeforeValidator

    origin = get_origin(annotation)
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        if any(isinstance(item, BeforeValidator) for item in metadata):
            return None
        return _scalar_kinds(base)
    if origin is Literal:
        return {"literal"}, set(get_args(annotation))
    if origin is list:
        (item,) = get_args(annotation)
        found = _scalar_kinds(item)
        if found is None or "list" in found[0]:
            return None  # a list of lists is not judged
        return {"list", *found[0]}, found[1]
    if origin in (Union, types.UnionType):
        kinds: "set[str]" = set()
        literals: "set[object]" = set()
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found = _scalar_kinds(arg)
            if found is None:
                return None
            kinds |= found[0]
            literals |= found[1]
        return kinds, literals
    for name, kind in (("bool", bool), ("int", int), ("float", float), ("str", str)):
        if annotation is kind:
            return {name}, set()
    if isinstance(annotation, type) and issubclass(annotation, PurePath):
        return {"path"}, set()
    return None


def _values_of_another_type(kinds: "set[str]", literals: "set[object]") -> list:
    """Return values that no field of `kinds` takes, whatever the settings convenience. A list
    and a table are never a scalar, text is no number or switch, and a number is no text. A list
    takes a list of items of its kind, never a table, a number or a switch; text is a
    comma-separated list, so it is not judged."""
    if "list" in kinds:
        items = _values_of_another_type(kinds - {"list"}, literals)
        return [{"lots": 1}, 2.5, True, False, 0] + [
            [item] for item in items if item not in ([], {})
        ]
    wrong: list = [["lots"], {"lots": 1}, [], {}]
    text = bool(kinds & {"str", "path"})
    if not text and "lots" not in literals:
        wrong.append("lots")
    if "float" not in kinds:
        wrong.append(2.5)
    if not kinds & {"int", "float", "bool", "literal"}:
        wrong += [True, False]
    if not kinds & {"int", "float"}:
        # A switch takes `true` and `false` only, never the numbers 0 and 1.
        wrong += [number for number in (0, 1, 2) if number not in literals]
    return wrong


#: What a wrong value may raise (see `test_malformed_input.VALIDATION_ERRORS`). Anything else is a
#: leak and fails the sweep.
VALIDATION_ERRORS = (ValidationError, DesignValidationError, FlowSettingsError, ValueError)

#: A scalar field that takes a value of another type on purpose, as `(class that declares the
#: field, field): (types of the values it takes, reason)`. Add a row only for a conversion that a
#: text for users documents. A waveform setting takes `true` and `false` for "write the default
#: file" and "write none" (`dataclass.waveform`), which the sweep knows without a row here.
DOCUMENTED_CONVERSIONS: "dict[tuple[str, str], tuple[tuple[type, ...], str]]" = {
    ("LanguageSettings", "standard"): (
        (int,),
        "a standard is naturally written as a number (docs/design-file.rst: `standard: 2008`)",
    ),
}

#: The input that a model needs and that no required field names.
SWEEP_BASES: "dict[str, dict]" = {"FPGA": {"part": "xc7a100t"}}


def _sweep_models() -> "list[type]":
    from xeda.design import Design

    from .settings_fields import reachable_models
    from .settings_samples import flow_classes

    return reachable_models([cls.Settings for cls, _ in flow_classes()] + [Design])


def _sweep_base(model) -> dict:
    """Return the least that `model` needs to be built alone. A model with a required field that
    `settings_fields.REQUIRED_VALUES` does not list fails the sweep, so that it never goes
    unjudged."""
    from xeda.design import Design

    from .settings_fields import REQUIRED_VALUES, key_of
    from .settings_samples import flow_classes, minimal_settings

    if model is Design:
        return {"name": "d", "rtl": {"sources": [], "top": "t"}}
    for cls, _ in flow_classes():
        if cls.Settings is model:
            return minimal_settings(cls)
    base = dict(SWEEP_BASES.get(model.__qualname__, {}))
    for name, info in model.model_fields.items():
        if info.is_required():
            key = key_of(model, name)
            base[name] = REQUIRED_VALUES[key]
    return base


SWEEP_MODELS = _sweep_models()


def test_the_sweep_judges_fields_of_every_plain_kind():
    judged = [
        (model.__qualname__, name, _scalar_kinds(info.annotation))
        for model in SWEEP_MODELS
        for name, info in model.model_fields.items()
        if not name.endswith("_")
    ]
    kinds = set().union(*(found[0] for _, _, found in judged if found))
    assert {"int", "float", "bool", "str", "path", "literal", "list"} <= kinds
    assert ("Settings", "verbose", ({"int"}, set())) in [
        (model.rpartition(".")[2], name, found) for model, name, found in judged
    ]
    assert sum(found is not None for _, _, found in judged) > 1200


@pytest.mark.parametrize("model", SWEEP_MODELS, ids=[m.__qualname__ for m in SWEEP_MODELS])
def test_a_scalar_setting_refuses_a_value_of_another_type_naming_the_setting(model):
    """The class of `verbose: lots` becoming 0: a validator that converts what it cannot read
    into a default. Every field of a plain type (a number, a switch, text, a path) is given a list,
    a table, and the text or number that its type does not take. Each must be a validation error
    that names the field, on construction and on assignment, and an assignment that fails
    changes nothing."""
    from .settings_fields import key_of

    base = _sweep_base(model)
    built = model(**base)
    silent = []
    unnamed = []
    for name, info in model.model_fields.items():
        if name.endswith("_"):
            continue
        taken_types = DOCUMENTED_CONVERSIONS.get(key_of(model, name), ((), ""))[0]
        kinds = _scalar_kinds(info.annotation)
        if kinds is None:
            continue
        wrong = _values_of_another_type(*kinds)
        wrong = [bad for bad in wrong if type(bad) not in taken_types]
        if waveform_format_of(model, name):
            wrong = [bad for bad in wrong if not isinstance(bad, bool)]
        for bad in wrong:
            try:
                taken = getattr(model(**{**base, name: bad}), name)
                silent.append(f"{name}={bad!r} was taken as {taken!r}")
            except VALIDATION_ERRORS as error:
                if not _names(error, name, info.alias):
                    unnamed.append(f"{name}={bad!r}: {error}")
            before = getattr(built, name)
            try:
                setattr(built, name, bad)
                silent.append(f"{name} = {bad!r} was taken as {getattr(built, name)!r}")
            except VALIDATION_ERRORS as error:
                if not _names(error, name, info.alias):
                    unnamed.append(f"{name} = {bad!r}: {error}")
            finally:
                if getattr(built, name) != before:
                    silent.append(
                        f"{name} = {bad!r} changed {before!r} to {getattr(built, name)!r}"
                    )
                    object.__setattr__(built, name, before)
    assert not silent, "\n".join(silent)
    assert not unnamed, "\n".join(unnamed)


def _names(error: Exception, name: str, alias: "str | None") -> bool:
    """Whether the location of the validation error is the setting, as the error gives it."""
    import re

    names = (name, alias)
    if isinstance(error, ValidationError):
        return any(item["loc"][:1] in ((name,), (alias,)) for item in error.errors())
    if isinstance(getattr(error, "errors", None), list):  # settings and design errors
        return any(re.split(r"\s*->\s*", str(item[0]))[0] in names for item in error.errors)
    return False


@pytest.mark.parametrize("given", [{}, {"a": 1}, 5, 2.5, True, {"a"}], ids=repr)
def test_a_testbench_top_of_another_type_is_refused_not_dropped(given):
    """`tb.top: 5` used to become no top module at all."""
    from xeda.design import TbSettings

    with pytest.raises(ValidationError, match="top"):
        TbSettings(top=given)
    assert TbSettings(top=None).top == TbSettings(top="").top == TbSettings(top=[]).top == ()
    assert TbSettings(top="t").top == ("t",)
    assert TbSettings(top=["a", "b"]).top == ("a", "b")


def test_no_validator_of_a_setting_turns_what_it_cannot_read_into_a_default():
    """`try_convert` returns a default for any failure, and a validator that calls it says
    nothing about a value it cannot read. A validator reports it, or leaves it to pydantic."""
    import ast

    import xeda

    lenient = {"try_convert", "try_convert_to_primitives"}
    # It reads the digits of a part number that a pattern matched: no setting has a wrong type.
    allowed = {("fpga.py", "_fpga_root_validator")}
    validators = {"field_validator", "model_validator", "validator", "root_validator"}
    found = []
    for path in sorted(Path(xeda.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {
                getattr(node, "attr", getattr(node, "id", ""))
                for decorator in function.decorator_list
                for node in ast.walk(decorator)
            }
            if not decorators & validators:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and node.id in lenient:
                    if (path.name, function.name) in allowed:
                        continue
                    found.append(f"{path.name}:{node.lineno} {function.name}")
    assert not found, found


@pytest.mark.parametrize("given", [0, False, [], {}], ids=repr)
def test_a_design_root_that_is_no_path_is_refused_not_replaced_by_the_working_directory(given):
    with pytest.raises(DesignValidationError, match="design_root"):
        Design(name="d", rtl={"sources": [], "top": "t"}, design_root=given)
