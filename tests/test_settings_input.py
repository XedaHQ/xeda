"""What a setting accepts: exactly its declared type, plus three documented flow conveniences.

There is no implicit conversion between types -- a number is not text, text is not a list, `True`
is not a name -- so the type a setting declares is the whole truth about what it takes, and a
mismatch is reported as a validation error naming the setting. Where a setting's values are
genuinely of several kinds (a Vivado run property is text, a number or a boolean), its type says
so and its consumer renders each kind explicitly.

On top of that, every *flow* setting gets the conveniences of `Flow.Settings._normalize_flow_setting`:
a list setting may be written as a comma-separated string (`-s xdc_files=a.xdc,b.xdc`), and
`$DESIGN_ROOT`/`$DESIGN_DIR`/`$PWD` are expanded at each path. Dependency settings are covered by
`test_dependency_settings.py`.
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
        (
            lambda: __import__("xeda.flow.fpga", fromlist=["FPGA"]).FPGA(part="xc7a100t", speed=2),
            "speed",
        ),
        (lambda: Design(name=2024, rtl={"sources": [], "top": "t"}), "name"),
        (lambda: Design(name=True, rtl={"sources": [], "top": "t"}), "name"),
        (lambda: _flow("verilator").Settings(compile_args=["-j", 8]), "compile_args"),
        (
            lambda: _flow("vivado_synth").Settings(fpga="xc7a100tftg256-2L", suppress_msgs=[8]),
            "suppress_msgs",
        ),
    ],
    ids=[
        "fpga-speed-number",
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


def test_a_union_keeps_the_type_it_was_given():
    """`stop_time` takes a number or text with a unit; neither is turned into the other."""
    from xeda.flow.sim import SimFlow

    assert SimFlow.Settings(stop_time=5).stop_time == 5
    assert SimFlow.Settings(stop_time="5us").stop_time == "5us"


def test_fractional_platform_values_are_not_truncated():
    """Physical quantities from PDK data are `float`; an `int` type used to truncate them."""
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


# ---------------------------------------------------------------------------------------------
# Path variables
# ---------------------------------------------------------------------------------------------


def test_path_placeholders_expand_recursively_without_rewriting_non_path_strings(tmp_path):
    """Path expansion follows the annotation through lists, mappings, tuples, and unions."""

    class PathSettings(Flow.Settings):
        scalar: Path
        paths: list[Path]
        hooks: dict[str, Path | None]
        libraries: list[tuple[str, str | Path]]

    payload = {
        "scalar": "$DESIGN_ROOT/scalar.sdc",
        "paths": ["$DESIGN_ROOT/a.sdc", "$DESIGN_DIR/b.sdc"],
        "hooks": {"pre": "$DESIGN_ROOT/pre.tcl", "post": None},
        "libraries": [("$LIBRARY_NAME", "$DESIGN_ROOT/lib/cells.lib")],
    }
    before = copy.deepcopy(payload)

    settings = PathSettings(design_root_=tmp_path, **payload)

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

    settings = TupleSettings(
        design_root_=tmp_path, pair=Pair(1, 2), lib=Lib("$NAME", "$DESIGN_ROOT/cells.lib")
    )

    assert settings.pair == (1, 2)
    assert settings.lib == ("$NAME", tmp_path / "cells.lib")


def test_real_path_list_setting_expands_design_root(tmp_path):
    from xeda.flows.dc import Dc

    settings = Dc.Settings(
        design_root_=tmp_path,
        platform="asap7",
        target_libraries=["$DESIGN_ROOT/lib/cells.lib"],
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

    settings = get_flow_class(flow).Settings(design_root_=tmp_path, **extra, **{field: value})

    assert getattr(settings, field) == at_root(expected)


def test_comma_separated_path_list_expands_each_design_root(tmp_path):
    from xeda.flows.dc import Dc

    settings = Dc.Settings(
        design_root_=tmp_path,
        platform="asap7",
        target_libraries="$DESIGN_ROOT/lib/a.lib,$DESIGN_ROOT/lib/b.lib",
    )

    assert settings.target_libraries == [
        tmp_path / "lib/a.lib",
        tmp_path / "lib/b.lib",
    ]
