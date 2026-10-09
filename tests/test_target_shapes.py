"""A target overlay and its design may write one setting in different forms.

Many design keys accept more than one form: a clock as a port name or a table, `parameters` as a
table or a list of `{name, value}` objects, `vhdl` as a standard or a table, a flow's `fpga` as a
part number or a table. The loader merges the selected target over the design before any of
those forms is expanded, so two things can go wrong when the two sides use different forms:

* the merge replaces one form by the other, and what the replaced value said is lost, or the two
  forms meet in one table and the model refuses it;
* a malformed value of the design is replaced by the target's table, so the error that every
  other selection (or no target at all) reports is hidden.

The oracle is the one of `test_targets.py`: the selected design equals the same design written
flat by hand. Each case below names the design's fragment, the target's fragment and the flat
fragment a person would write for "the design, then the target's changes".
"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.dataclass import shape_problems
from xeda.design import DesignValidationError
from xeda.flow import FlowSettingsError
from xeda.flow_runner.settings_layers import compose_flow_settings
from xeda.flows import GhdlSim, VivadoSynth
from xeda.utils import settings_to_dict
from xeda.xedaproject import XedaProject

from .settings_samples import PROBES
from .test_targets import BASE, RESOURCES, flows_as_read


def write_design(tmp_path: Path, data: dict, name: str = "d.json") -> Path:
    """The design as a JSON file beside the sources it names (YAML would read a text such as
    `08` as a number)."""
    for source in RESOURCES.iterdir():
        if source.suffix in {".v", ".xdc", ".lpf"} and not (tmp_path / source.name).exists():
            shutil.copy(source, tmp_path / source.name)
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def overlay(base: dict, fragment: dict) -> dict:
    """`fragment` written over `base`: tables merge, everything else (a list included) replaces."""
    merged = copy.deepcopy(base)
    for key, value in fragment.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = overlay(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def selected_and_flat(tmp_path: Path, design: dict, target: dict, flat: dict) -> tuple:
    """The design with the target selected, and the design written flat by hand."""
    path = write_design(tmp_path, {**overlay(BASE, design), "targets": {"t": target}})
    flat_path = write_design(tmp_path, overlay(BASE, flat), "flat.json")
    return Design.from_file(path, target="t"), Design.from_file(flat_path)


def rtl(**values: Any) -> dict:
    return {"rtl": values}


def tb(**values: Any) -> dict:
    return {"tb": values}


PARAMETER_LIST = [{"name": "WIDTH", "value": 8}, {"name": "DEPTH", "value": 2}]
KNIGHT_TB = {"top": "knight_tb", "sources": ["knight_tb.v"]}

#: (id, the design's fragment, the target's fragment, the flat fragment).
# fmt: off
SHAPES = [
    # ---- the design's clock: a port name, a table, `clock_port`, or `clocks`
    ("clock: name then table", rtl(clock="CLK"), rtl(clock={"name": "sys"}), rtl(clock={"port": "CLK", "name": "sys"})),
    ("clock: table then name", rtl(clock={"port": "CLK", "name": "sys"}), rtl(clock="CLK2"), rtl(clock={"port": "CLK2", "name": "sys"})),
    ("clock: table then table", rtl(clock={"port": "CLK"}), rtl(clock={"name": "sys"}), rtl(clock={"port": "CLK", "name": "sys"})),
    ("clock: name then name", rtl(clock="CLK"), rtl(clock="CLK2"), rtl(clock="CLK2")),
    ("clock: name then clock_port", rtl(clock="CLK"), rtl(clock_port="CLK2"), rtl(clock="CLK2")),
    ("clock: clock_port then name", rtl(clock_port="CLK"), rtl(clock="CLK2"), rtl(clock="CLK2")),
    ("clock: clock_port then clock_port", rtl(clock_port="CLK"), rtl(clock_port="CLK2"), rtl(clock_port="CLK2")),
    ("clock: clock_port then table", rtl(clock_port="CLK"), rtl(clock={"name": "sys"}), rtl(clock={"port": "CLK", "name": "sys"})),
    ("clock: table then clock_port", rtl(clock={"port": "CLK", "name": "sys"}), rtl(clock_port="CLK2"), rtl(clock={"port": "CLK2", "name": "sys"})),
    ("clock: name then clocks", rtl(clock="CLK"), rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clocks=[{"port": "A"}, {"port": "B"}])),
    ("clock: table then clocks", rtl(clock={"port": "CLK"}), rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clocks=[{"port": "A"}, {"port": "B"}])),
    ("clock: clock_port then clocks", rtl(clock_port="CLK"), rtl(clocks=[{"port": "A"}]), rtl(clocks=[{"port": "A"}])),
    ("clock: clocks then clocks", rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clocks=[{"port": "C"}]), rtl(clocks=[{"port": "C"}])),
    # the target's singular spelling refines the design's first clock, as a flow's `clock` does
    ("clock: clocks then name", rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clock="C"), rtl(clocks=[{"port": "C"}, {"port": "B"}])),
    ("clock: clocks then clock_port", rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clock_port="C"), rtl(clocks=[{"port": "C"}, {"port": "B"}])),
    ("clock: table then null", rtl(clock={"port": "CLK"}), rtl(clock=None), rtl(clock=None)),
    ("clock: table then empty table", rtl(clock={"port": "CLK"}), rtl(clock={}), rtl(clocks=[])),
    ("clock: clocks then empty clocks", rtl(clocks=[{"port": "A"}]), rtl(clocks=[{}]), rtl(clocks=[])),
    ("clock: clocks then null", rtl(clocks=[{"port": "A"}, {"port": "B"}]), rtl(clock=None), rtl(clocks=[])),
    ("clock: name then empty clock_port", rtl(clock="CLK"), rtl(clock_port=""), rtl(clock_port="")),
    ("clock: clocks then empty clock_port", rtl(clocks=[{"port": "A"}]), rtl(clock_port=""), rtl(clocks=[])),
    ("clock: null then name", rtl(clock=None), rtl(clock="CLK"), rtl(clock="CLK")),
    ("clock: one clocks then table", rtl(clocks=[{"port": "A"}]), rtl(clock={"name": "sys"}), rtl(clocks=[{"port": "A", "name": "sys"}])),
    # ---- the testbench: top, cocotb, and the `test`/`tests` spellings
    ("tb.top: text then list", tb(top="tb"), tb(top=["a", "b"]), tb(top=["a", "b"])),
    ("tb.top: list then text", tb(top=["a", "b"]), tb(top="c"), tb(top="c")),
    ("tb.cocotb: true then table", tb(cocotb=True), tb(cocotb={"module": "m"}), tb(cocotb={"module": "m"})),
    ("tb.cocotb: table then true", tb(cocotb={"module": "m", "toplevel": "t"}), tb(cocotb=True), tb(cocotb={"module": "m", "toplevel": "t"})),
    ("tb.cocotb: table then false", tb(cocotb={"module": "m"}), tb(cocotb=False), tb(cocotb=False)),
    ("tb.cocotb: false then table", tb(cocotb=False), tb(cocotb={"module": "m"}), tb(cocotb={"module": "m"})),
    ("tb.cocotb: table then table", tb(cocotb={"module": "m"}), tb(cocotb={"toplevel": "t"}), tb(cocotb={"module": "m", "toplevel": "t"})),
    ("tb: test then tb", {"test": KNIGHT_TB}, tb(top="tb2"), tb(top="tb2", sources=["knight_tb.v"])),
    ("tb: tb then tests", tb(**KNIGHT_TB), {"tests": [{"top": "tb2"}]}, tb(top="tb2", sources=["knight_tb.v"])),
    # ---- the languages
    ("vhdl: standard then table", {"language": {"vhdl": "08"}}, {"language": {"vhdl": {"synopsys": True}}}, {"language": {"vhdl": {"version": "08", "synopsys": True}}}),
    ("vhdl: number then table", {"language": {"vhdl": 2008}}, {"language": {"vhdl": {"synopsys": True}}}, {"language": {"vhdl": {"version": "2008", "synopsys": True}}}),
    ("vhdl: table then standard", {"language": {"vhdl": {"version": "93", "synopsys": True}}}, {"language": {"vhdl": "08"}}, {"language": {"vhdl": {"version": "08", "synopsys": True}}}),
    ("vhdl: standard then version", {"language": {"vhdl": {"standard": "93"}}}, {"language": {"vhdl": {"version": "08"}}}, {"language": {"vhdl": {"version": "08"}}}),
    ("verilog: standard then version", {"language": {"verilog": {"standard": "2001"}}}, {"language": {"verilog": {"version": "2005"}}}, {"language": {"verilog": {"version": "2005"}}}),
    ("language: hdl then language", {"hdl": {"vhdl": "93"}}, {"language": {"vhdl": "08"}}, {"language": {"vhdl": "08"}}),
    # ---- parameters (a table or a list of `{name, value}`), generics, defines
    ("parameters: list then table", rtl(parameters=PARAMETER_LIST), rtl(parameters={"WIDTH": 4}), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: table then list", rtl(parameters={"WIDTH": 8, "DEPTH": 2}), rtl(parameters=[{"name": "WIDTH", "value": 4}]), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: list then list", rtl(parameters=PARAMETER_LIST), rtl(parameters=[{"name": "WIDTH", "value": 4}]), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: generics list then table", rtl(generics=PARAMETER_LIST), rtl(parameters={"WIDTH": 4}), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: table then generics list", rtl(parameters={"WIDTH": 8, "DEPTH": 2}), rtl(generics=[{"name": "WIDTH", "value": 4}]), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: table then table", rtl(parameters={"WIDTH": 8, "DEPTH": 2}), rtl(parameters={"WIDTH": 4}), rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("tb.parameters: list then table", tb(parameters=PARAMETER_LIST), tb(parameters={"WIDTH": 4}), tb(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("defines: table then table", rtl(defines={"A": 1, "B": 2}), rtl(defines={"B": 3}), rtl(defines={"A": 1, "B": 3})),
    # ---- sources: one path, one table, a list
    ("sources: path then list", rtl(sources="knight.v"), rtl(sources=["por_sync.v"]), rtl(sources=["knight.v", "por_sync.v"])),
    ("sources: table then table", rtl(sources={"file": "knight.v"}), rtl(sources={"file": "por_sync.v"}), rtl(sources=["knight.v", "por_sync.v"])),
    ("sources: list then path", rtl(sources=["knight.v"]), rtl(sources="por_sync.v"), rtl(sources=["knight.v", "por_sync.v"])),
    ("tb.sources: path then list", tb(sources="knight_tb.v"), tb(sources=["por_sync.v"]), tb(sources=["knight_tb.v", "por_sync.v"])),
    # ---- the other spellings of a design key
    ("author: author then authors", {"author": "A <a@example.com>"}, {"authors": ["B <b@example.com>"]}, {"authors": ["B <b@example.com>"]}),
]
# fmt: on


def cases(rows):
    return [pytest.param(*row[1:], id=row[0]) for row in rows]


#: Forms the loader takes and the schema does not describe yet, from before targets, with why:
#: a single source on its own where the schema says a list, and an empty table where it says a
#: whole clock.
SCHEMA_GAPS = {
    "sources: path then list": "the schema lists `sources` only as a list",
    "sources: table then table": "the schema lists `sources` only as a list",
    "sources: list then path": "the schema lists `sources` only as a list",
    "tb.sources: path then list": "the schema lists `sources` only as a list",
    "clock: clocks then empty clocks": "the schema lists `clocks` as whole clocks",
}


def schema_cases(rows):
    return [
        pytest.param(
            *row[1:],
            id=row[0],
            marks=(
                [pytest.mark.xfail(reason=SCHEMA_GAPS[row[0]], strict=True)]
                if row[0] in SCHEMA_GAPS
                else []
            ),
        )
        for row in rows
    ]


@pytest.mark.parametrize("design, target, flat", schema_cases(SHAPES))
def test_the_schema_accepts_every_form_the_loader_accepts(tmp_path, design, target, flat):
    """The published schema describes the design file as the loader reads it: a form the loader
    takes, in a design or in a target, is valid against it."""
    from .test_design_schema import validator

    data = {**overlay(BASE, design), "targets": {"t": target}}
    Design.from_file(write_design(tmp_path, data), target="t")
    assert [e.message for e in validator().iter_errors(data)] == []


@pytest.mark.parametrize("design, target, flat", cases(SHAPES))
def test_a_target_in_another_form_gives_the_design_written_flat_by_hand(
    tmp_path, design, target, flat
):
    selected, expected = selected_and_flat(tmp_path, design, target, flat)
    assert selected.model_dump(mode="json", exclude={"target"}) == expected.model_dump(
        mode="json", exclude={"target"}
    )
    assert selected.rtl_hash == expected.rtl_hash and selected.tb_hash == expected.tb_hash


# ------------------------------------------------------------------------------ a flow's section


def composed(flow_class, design: Design, *layers: Any) -> dict:
    """The settings of `flow_class` that the design's `flows` section (then `layers`) give."""
    given = compose_flow_settings(flow_class, [design.flow], *layers)
    settings = flow_class.Settings.from_input(given, design_root=design.root_path)
    return settings.model_dump(mode="json")


PART = "xc7a35tcpg236-1"
# fmt: off
FLOW_SHAPES = [
    # (id, flow, the design's section, the target's section, the flat section)
    ("fpga: part then table", VivadoSynth, {"fpga": PART}, {"fpga": {"speed": -2}}, {"fpga": {"part": PART, "speed": -2}}),
    ("fpga: part then part", VivadoSynth, {"fpga": PART}, {"fpga": "xc7a100tcsg324-1"}, {"fpga": "xc7a100tcsg324-1"}),
    ("fpga: table then table", VivadoSynth, {"fpga": {"part": PART}}, {"fpga": {"speed": -2}}, {"fpga": {"part": PART, "speed": -2}}),
    ("threads: ncpus then nthreads", VivadoSynth, {"fpga": PART, "ncpus": 2}, {"nthreads": 4}, {"fpga": PART, "nthreads": 4}),
    ("threads: nthreads then ncpus", VivadoSynth, {"fpga": PART, "nthreads": 2}, {"ncpus": 4}, {"fpga": PART, "nthreads": 4}),
    ("warnings: warn_error then werror", GhdlSim, {"warn_error": True}, {"werror": False}, {"werror": False}),
    ("clock: period then table", VivadoSynth, {"fpga": PART, "clock_period": 10}, {"clock": {"freq": "200MHz"}}, {"fpga": PART, "clock": {"freq": "200MHz"}}),
    ("clock: table then period", VivadoSynth, {"fpga": PART, "clock": {"freq": "200MHz"}}, {"clock_period": 5}, {"fpga": PART, "clock_period": 5}),
    ("clock: clock then clocks", VivadoSynth, {"fpga": PART, "clock": {"period": 10}}, {"clocks": {"main_clock": {"uncertainty": "100ps"}}}, {"fpga": PART, "clock": {"period": 10, "uncertainty": "100ps"}}),
    ("clock: clocks then clock", VivadoSynth, {"fpga": PART, "clocks": {"sys": {"port": "clk", "period": 10}}}, {"clock": {"freq": "200MHz"}}, {"fpga": PART, "clocks": {"sys": {"port": "clk", "freq": "200MHz"}}}),
    ("clock: period then period", VivadoSynth, {"fpga": PART, "clock_period": 10}, {"clock_period": 5}, {"fpga": PART, "clock_period": 5}),
]
# fmt: on


@pytest.mark.parametrize("flow_class, design, target, flat", cases(FLOW_SHAPES))
def test_a_target_s_flow_section_in_another_form_gives_the_section_written_flat_by_hand(
    tmp_path, flow_class, design, target, flat
):
    name = flow_class.name
    selected, expected = selected_and_flat(
        tmp_path, {"flows": {name: design}}, {"flows": {name: target}}, {"flows": {name: flat}}
    )
    assert composed(flow_class, selected) == composed(flow_class, expected)


def test_a_part_given_as_text_survives_a_higher_layer_that_refines_the_device(tmp_path):
    """The same class between origins, with no target: the design writes the part as text, and
    `-s fpga.speed=-2` refines it."""
    design = Design.from_file(
        write_design(tmp_path, {**BASE, "flows": {"vivado_synth": {"fpga": PART}}})
    )
    flat = Design.from_file(
        write_design(
            tmp_path, {**BASE, "flows": {"vivado_synth": {"fpga": {"part": PART}}}}, "flat.json"
        )
    )
    refined = composed(VivadoSynth, design, {"fpga": {"speed": -2}})
    assert refined == composed(VivadoSynth, flat, {"fpga": {"speed": -2}})
    assert refined["fpga"]["part"] == PART


def test_a_part_given_as_text_refines_a_lower_table_as_the_key_does(tmp_path):
    """`fpga: P` is exactly `fpga.part: P` wherever layers meet: it keeps what a lower layer pins
    (the speed grade), as `-s fpga.part=P` always did."""
    design = Design.from_file(
        write_design(
            tmp_path, {**BASE, "flows": {"vivado_synth": {"fpga": {"part": PART, "speed": -2}}}}
        )
    )
    other = "xc7a100tcsg324-1"
    as_text = composed(VivadoSynth, design, {"fpga": other})
    as_key = composed(VivadoSynth, design, {"fpga": {"part": other}})
    assert as_text == as_key
    assert as_text["fpga"]["part"] == other and as_text["fpga"]["speed"] == "-2"


@pytest.mark.parametrize("key", ["fpga", "synth", "impl"])
@pytest.mark.parametrize("probe", [3, [1], True])
def test_a_malformed_lower_layer_is_reported_whatever_the_layer_above_writes(tmp_path, key, probe):
    """Between origins, not only in one design: the design writes no table where a table is
    expected, and `-s` writes one."""
    higher = {"fpga": {"part": PART}, "synth": {"strategy": "x"}, "impl": {"strategy": "y"}}[key]
    design = Design.from_file(
        write_design(tmp_path, {**BASE, "flows": {"vivado_synth": {"fpga": PART, key: probe}}})
    )
    with pytest.raises(FlowSettingsError, match=key):
        composed(VivadoSynth, design, {key: higher})


@pytest.mark.parametrize("clock", [0, False, [], 3, ["x"], True])
@pytest.mark.parametrize("spelling", ["clock", "clock_port"])
def test_a_clock_that_is_neither_text_nor_a_table_is_refused_at_the_key_written(
    tmp_path, spelling, clock
):
    """A number silently meant no clock, because falsy clocks were dropped. `clock_port` takes a
    port name alone, but an empty one (`""`, `null`) is no clock."""
    if spelling == "clock_port" and not clock:
        pytest.skip("an empty `clock_port` is no clock")
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(write_design(tmp_path, overlay(BASE, rtl(**{spelling: clock}))))
    assert f"rtl.{spelling}:" in str(raised.value), raised.value


@pytest.mark.parametrize("clocks", [[False], [0], [[]], [{"port": "A"}, False], 3, False])
def test_a_clock_in_clocks_that_is_no_clock_is_refused_at_clocks(tmp_path, clocks):
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(write_design(tmp_path, overlay(BASE, rtl(clocks=clocks))))
    assert "rtl.clocks" in str(raised.value), raised.value


@pytest.mark.parametrize(
    "fragment",
    [rtl(clock={}), rtl(clocks={}), rtl(clocks=[{}]), rtl(clocks=[{}, None, ""]), rtl(clocks=[])],
    ids=["clock {}", "clocks {}", "clocks [{}]", "clocks [{}, null, '']", "clocks []"],
)
def test_an_empty_table_is_no_clock(tmp_path, fragment):
    """An empty table says nothing, as `null` and `""` do (the loader has always read it so)."""
    design = Design.from_file(write_design(tmp_path, overlay(BASE, fragment)))
    assert design.rtl.clocks == [] and design.rtl.clock is None


@pytest.mark.parametrize("spelling", ["clock", "clock_port", "clocks"])
@pytest.mark.parametrize("selected", [False, True], ids=["unselected", "selected"])
def test_a_target_clock_that_is_no_clock_is_reported_at_the_key_written_in_the_target(
    tmp_path, spelling, selected
):
    """`clock` and `clock_port` are no fields, but a target's overlay is judged as the design is:
    where it is written, whether or not the target is selected."""
    value = [False] if spelling == "clocks" else 3
    data = {**BASE, "targets": {"good": {"defines": {"A": 1}}, "bad": rtl(**{spelling: value})}}
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(write_design(tmp_path, data), target="bad" if selected else "good")
    assert f"targets.bad.rtl.{spelling}:" in str(raised.value), raised.value


def test_a_target_s_parameters_given_as_generics_are_reported_as_written(tmp_path):
    data = {**BASE, "targets": {"good": {}, "bad": rtl(generics=None)}}
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(write_design(tmp_path, data), target="good")
    assert "targets.bad.rtl.generics:" in str(raised.value), raised.value


@pytest.mark.parametrize("spelling", ["clock", "clock_port"])
@pytest.mark.parametrize("none", [None, ""])
def test_an_absent_clock_is_no_clock(tmp_path, spelling, none):
    design = Design.from_file(write_design(tmp_path, overlay(BASE, rtl(**{spelling: none}))))
    assert design.rtl.clocks == [] and design.rtl.clock is None


@pytest.mark.parametrize("standard", [True, False])
def test_a_bool_is_no_language_standard(tmp_path, standard):
    with pytest.raises(DesignValidationError, match="vhdl"):
        Design.from_file(write_design(tmp_path, overlay(BASE, {"language": {"vhdl": standard}})))


# ------------------------------------------------------------------------------ a malformed value


def plain(value: Any) -> Any:
    """`value` as a file holds it: tuples and named tuples are lists."""
    return json.loads(json.dumps(value))


def _distinct(values: list) -> list:
    """`values` without a repeat; `0` and `False` are two values."""
    seen: set = set()
    kept = []
    for value in values:
        key = (type(value).__name__, repr(value))
        if key not in seen:
            seen.add(key)
            kept.append(value)
    return kept


#: Every kind of value that is not a table, as a design file holds it.
PROBE_VALUES = _distinct([probe for probe in plain(PROBES) if not isinstance(probe, dict)])

#: (the path of a mapping-shaped key of the design, a table the target can write there).
MAPPING_KEYS = [
    (("rtl",), {"top": "knight"}),
    (("tb",), {"top": "knight_tb"}),
    (("language",), {"vhdl": {"synopsys": True}}),
    (("language", "vhdl"), {"synopsys": True}),
    (("language", "verilog"), {"version": "2005"}),
    (("rtl", "parameters"), {"WIDTH": 4}),
    (("rtl", "defines"), {"A": 1}),
    (("rtl", "attributes"), {"keep": {"knight": True}}),
    (("rtl", "clock"), {"port": "CLK"}),
    (("tb", "parameters"), {"WIDTH": 4}),
    (("tb", "defines"), {"A": 1}),
    (("tb", "cocotb"), {"module": "m"}),
]


def written_at(path: tuple, value: Any) -> dict:
    fragment: Any = value
    for key in reversed(path):
        fragment = {key: fragment}
    return fragment


def error_of_design(tmp_path: Path, data: dict, **kwargs: Any) -> str | None:
    try:
        Design.from_file(write_design(tmp_path, data), **kwargs)
    except DesignValidationError as e:
        return str(e)
    return None


@pytest.mark.parametrize("probe", PROBE_VALUES, ids=repr)
@pytest.mark.parametrize(
    "path, table",
    MAPPING_KEYS,
    ids=lambda value: ".".join(value) if isinstance(value, tuple) else "",
)
def test_a_malformed_design_value_is_reported_whether_or_not_the_target_writes_a_table_there(
    tmp_path, path, table, probe
):
    """A design is as valid as it is, whichever target is selected: the error of a value that no
    target is selected over is the error a target that writes a table there reports."""
    design = overlay(BASE, written_at(path, probe))
    alone = error_of_design(tmp_path, design)
    if alone is None:
        pytest.skip("a form the key accepts")
    with_target = error_of_design(
        tmp_path, {**design, "targets": {"t": written_at(path, table)}}, target="t"
    )
    assert with_target is not None, f"the target's table hid: {alone}"
    assert with_target == alone


@pytest.mark.parametrize(
    "key, value, where",
    [
        ("tb", 3, "targets.bad.tb"),
        ("language", {"vhdl": True}, "targets.bad.language.vhdl"),
        ("tb", {"cocotb": "x"}, "targets.bad.tb.cocotb"),
        ("flows", {"vivado_synth": {"fpga": 5}}, "targets.bad.flows.vivado_synth.fpga"),
    ],
    ids=["tb", "vhdl", "cocotb", "flow fpga"],
)
def test_a_target_s_own_mistake_is_reported_where_it_is_written_selected_or_not(
    tmp_path, key, value, where
):
    """The target that is not selected is checked as well: the mistake is not left for the day
    someone selects it."""
    data = {**BASE, "targets": {"good": {"defines": {"A": 1}}, "bad": {key: value}}}
    path = write_design(tmp_path, data)
    with pytest.raises(DesignValidationError) as raised:
        Design.from_file(path, target="good")
    assert where in str(raised.value) and "takes a table" in str(raised.value), raised.value


FLOW_MAPPING_KEYS = [
    (VivadoSynth, "fpga", {"part": PART}),
    (VivadoSynth, "synth", {"strategy": "Flow_PerfOptimized_high"}),
    (VivadoSynth, "impl", {"strategy": "Performance_Explore"}),
]


def flow_error(tmp_path: Path, flow_class, data: dict, **kwargs: Any) -> str | None:
    design = Design.from_file(write_design(tmp_path, data), **kwargs)
    try:
        composed(flow_class, design)
    except FlowSettingsError as e:
        return str(e)
    return None


@pytest.mark.parametrize("probe", PROBE_VALUES, ids=repr)
@pytest.mark.parametrize(
    "flow_class, key, table", FLOW_MAPPING_KEYS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_a_malformed_flow_setting_is_reported_whether_or_not_the_target_writes_a_table_there(
    tmp_path, flow_class, key, table, probe
):
    name = flow_class.name
    if not shape_problems(flow_class.Settings, {key: probe}):
        pytest.skip("a form the setting accepts as a table (a wrong value in one is not a shape)")
    design = overlay(BASE, {"flows": {name: {"fpga": PART, key: probe}}})
    alone = flow_error(tmp_path, flow_class, design)
    assert alone is not None, "a value that is no table is an error where a table is expected"
    target = {"flows": {name: {key: table}}}
    with_target = flow_error(tmp_path, flow_class, {**design, "targets": {"t": target}}, target="t")
    assert with_target is not None, f"the target's table hid: {alone}"
    assert with_target == alone


# ------------------------------------------------------------------------------ design overrides
#
# `--design-overrides` (`overrides=` of `Design.from_file`) is an overlay on the design, as a target
# is, and is merged by the same rule: a shorthand is read as its table, and a mistake of the
# design is not hidden by an override table. The one difference is `sources`: only a target adds
# its sources to the design's, so an override's replace them.

#: (id, the design's fragment, the overrides as `settings_to_dict` gives them, the flat fragment).
# fmt: off
OVERRIDES = [
    ("clock: name over a port", rtl(clock="CLK"), {"rtl": {"clock": {"name": "sys"}}}, rtl(clock={"port": "CLK", "name": "sys"})),
    ("clock: port over a table", rtl(clock={"port": "CLK", "name": "sys"}), {"rtl": {"clock": "CLK2"}}, rtl(clock={"port": "CLK2", "name": "sys"})),
    ("clock: flat name over a port", rtl(clock="CLK"), {"clock": {"name": "sys"}}, rtl(clock={"port": "CLK", "name": "sys"})),
    ("clock: clocks over a port", rtl(clock="CLK"), {"rtl": {"clocks": [{"port": "A"}, {"port": "B"}]}}, rtl(clocks=[{"port": "A"}, {"port": "B"}])),
    ("clock: none over a port", rtl(clock="CLK"), {"rtl": {"clock": None}}, rtl(clocks=[])),
    ("parameters: table over a list", rtl(parameters=PARAMETER_LIST), {"rtl": {"parameters": {"WIDTH": 4}}}, rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: list over a table", rtl(parameters={"WIDTH": 8, "DEPTH": 2}), {"rtl": {"parameters": [{"name": "WIDTH", "value": 4}]}}, rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("parameters: flat generics over a list", rtl(parameters=PARAMETER_LIST), {"generics": {"WIDTH": 4}}, rtl(parameters={"WIDTH": 4, "DEPTH": 2})),
    ("vhdl: synopsys over a standard", {"language": {"vhdl": "08"}}, {"language": {"vhdl": {"synopsys": True}}}, {"language": {"vhdl": {"version": "08", "synopsys": True}}}),
    ("vhdl: standard over a table", {"language": {"vhdl": {"version": "93", "synopsys": True}}}, {"language": {"vhdl": "08"}}, {"language": {"vhdl": {"version": "08", "synopsys": True}}}),
    ("vhdl: hdl over language", {"language": {"vhdl": "93"}}, {"hdl": {"vhdl": {"synopsys": True}}}, {"language": {"vhdl": {"version": "93", "synopsys": True}}}),
    ("cocotb: true over a table", tb(cocotb={"module": "m"}), {"tb": {"cocotb": True}}, tb(cocotb={"module": "m"})),
    ("cocotb: table over true", tb(cocotb=True), {"tb": {"cocotb": {"module": "m"}}}, tb(cocotb={"module": "m"})),
    ("cocotb: false over a table", tb(cocotb={"module": "m"}), {"tb": {"cocotb": False}}, tb(cocotb=False)),
    ("sources: replaced", rtl(sources=["knight.v", "por_sync.v"]), {"rtl": {"sources": ["por_sync.v"]}}, rtl(sources=["por_sync.v"])),
    ("sources: tb replaced", tb(**KNIGHT_TB), {"tb": {"sources": ["por_sync.v"]}}, tb(top="knight_tb", sources=["por_sync.v"])),
]
# fmt: on


@pytest.mark.parametrize("with_target", [False, True], ids=["no target", "a target"])
@pytest.mark.parametrize("design, overrides, flat", cases(OVERRIDES))
def test_design_overrides_give_the_design_written_flat_by_hand(
    tmp_path, design, overrides, flat, with_target
):
    targets = {"targets": {"t": {"defines": {"A": 1}}}} if with_target else {}
    path = write_design(tmp_path, {**overlay(BASE, design), **targets})
    flat_path = write_design(
        tmp_path,
        overlay(overlay(BASE, flat), {"rtl": {"defines": {"A": 1}}} if with_target else {}),
        "flat.json",
    )
    selected = Design.from_file(path, overrides=overrides)
    expected = Design.from_file(flat_path)
    assert selected.model_dump(mode="json", exclude={"target"}) == expected.model_dump(
        mode="json", exclude={"target"}
    )
    assert selected.rtl_hash == expected.rtl_hash


@pytest.mark.parametrize(
    "design, overrides, flat",
    [
        (rtl(clock="CLK"), ["rtl.clock.name=sys"], rtl(clock={"port": "CLK", "name": "sys"})),
        (
            rtl(parameters=PARAMETER_LIST),
            ["rtl.parameters.WIDTH=4"],
            rtl(parameters={"WIDTH": "4", "DEPTH": 2}),
        ),
        (
            {"language": {"vhdl": "08"}},
            ["language.vhdl.synopsys=true"],
            {"language": {"vhdl": {"version": "08", "synopsys": True}}},
        ),
        (tb(cocotb=True), ["tb.cocotb.module=m"], tb(cocotb={"module": "m"})),
    ],
    ids=["clock", "parameters", "vhdl", "cocotb"],
)
def test_design_overrides_given_as_text_are_tables_too(tmp_path, design, overrides, flat):
    """`--design-overrides KEY=VALUE` reaches the loader as `settings_to_dict` makes it."""
    selected = Design.from_file(
        write_design(tmp_path, overlay(BASE, design)), overrides=settings_to_dict(overrides)
    )
    expected = Design.from_file(write_design(tmp_path, overlay(BASE, flat), "flat.json"))
    assert selected.model_dump(mode="json") == expected.model_dump(mode="json")


# fmt: off
FLOW_OVERRIDES = [
    ("fpga: speed over a part", VivadoSynth, {"fpga": PART}, {"fpga": {"speed": -2}}, {"fpga": {"part": PART, "speed": -2}}),
    ("fpga: part over a table", VivadoSynth, {"fpga": {"part": PART, "speed": -2}}, {"fpga": "xc7a100tcsg324-1"}, {"fpga": {"part": "xc7a100tcsg324-1", "speed": -2}}),
    ("threads: nthreads over ncpus", VivadoSynth, {"fpga": PART, "ncpus": 2}, {"nthreads": 4}, {"fpga": PART, "nthreads": 4}),
    ("clock: table over a period", VivadoSynth, {"fpga": PART, "clock_period": 10}, {"clock": {"freq": "200MHz"}}, {"fpga": PART, "clock": {"freq": "200MHz"}}),
]
# fmt: on


@pytest.mark.parametrize("flow_class, design, overrides, flat", cases(FLOW_OVERRIDES))
def test_a_flow_override_in_another_form_gives_the_section_written_flat_by_hand(
    tmp_path, flow_class, design, overrides, flat
):
    name = flow_class.name
    selected = Design.from_file(
        write_design(tmp_path, overlay(BASE, {"flows": {name: design}})),
        overrides={"flows": {name: overrides}},
    )
    expected = Design.from_file(
        write_design(tmp_path, overlay(BASE, {"flows": {name: flat}}), "flat.json")
    )
    assert composed(flow_class, selected) == composed(flow_class, expected)


@pytest.mark.parametrize("probe", PROBE_VALUES, ids=repr)
@pytest.mark.parametrize(
    "path, table",
    MAPPING_KEYS,
    ids=lambda value: ".".join(value) if isinstance(value, tuple) else "",
)
def test_a_malformed_design_value_is_reported_whether_or_not_an_override_writes_a_table_there(
    tmp_path, path, table, probe
):
    design = overlay(BASE, written_at(path, probe))
    alone = error_of_design(tmp_path, design)
    if alone is None:
        pytest.skip("a form the key accepts")
    overridden = error_of_design(tmp_path, design, overrides=written_at(path, table))
    assert overridden is not None, f"the override's table hid: {alone}"
    assert overridden == alone


@pytest.mark.parametrize("probe", PROBE_VALUES, ids=repr)
@pytest.mark.parametrize(
    "flow_class, key, table", FLOW_MAPPING_KEYS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_a_malformed_flow_setting_is_reported_whether_or_not_an_override_writes_a_table_there(
    tmp_path, flow_class, key, table, probe
):
    name = flow_class.name
    if not shape_problems(flow_class.Settings, {key: probe}):
        pytest.skip("a form the setting accepts as a table")
    design = overlay(BASE, {"flows": {name: {"fpga": PART, key: probe}}})
    alone = flow_error(tmp_path, flow_class, design)
    assert alone is not None
    overridden = flow_error(tmp_path, flow_class, design, overrides={"flows": {name: {key: table}}})
    assert overridden == alone


def test_a_flows_table_that_is_no_table_is_a_design_error_with_overrides(tmp_path):
    with pytest.raises(DesignValidationError, match="flows"):
        Design.from_file(
            write_design(tmp_path, {**BASE, "flows": 3}), overrides={"rtl": {"top": "knight"}}
        )


#: A null or empty `flows` table, or flow section, adds nothing, as it does in every settings
#: layer: the design's section stays as it is. A key set in the section changes it.
NULL_FLOWS = [
    ("table null", {"flows": None}),
    ("table empty", {"flows": {}}),
    ("flow spelling null", {"flow": None}),
    ("section null", {"flows": {"vivado_synth": None}}),
    ("section empty", {"flows": {"vivado_synth": {}}}),
    ("section null, flow spelling", {"flow": {"vivado_synth": None}}),
    ("section null by alias", {"flows": {"VivadoSynth": None}}),
]


@pytest.mark.parametrize("merged", ["a target", "overrides"])
@pytest.mark.parametrize(
    "fragment", [row[1] for row in NULL_FLOWS], ids=[row[0] for row in NULL_FLOWS]
)
def test_a_null_or_empty_flows_table_or_section_leaves_the_design_s_section(
    tmp_path, merged, fragment
):
    design = {
        **BASE,
        "flows": {
            "vivado_synth": {"fpga": PART, "clock": {"period": 10.0}},
            "nextpnr": {"seed": 3},
        },
    }
    kept = Design.from_file(write_design(tmp_path, design))
    if merged == "a target":
        data = {**design, "targets": {"t": {"defines": {"A": 1}, **fragment}}}
        selected = Design.from_file(write_design(tmp_path, data, "target.json"))
    else:
        selected = Design.from_file(write_design(tmp_path, design), overrides=fragment)
    assert kept.flow["vivado_synth"]["fpga"] == PART
    assert flows_as_read(selected) == flows_as_read(kept)


@pytest.mark.parametrize("merged", ["a target", "overrides"])
def test_a_null_section_beside_a_written_one_changes_only_the_written_one(tmp_path, merged):
    design = {
        **BASE,
        "flows": {
            "vivado_synth": {"fpga": PART, "clock": {"period": 10.0}},
            "nextpnr": {"seed": 3},
        },
    }
    fragment = {"flows": {"vivado_synth": None, "nextpnr": {"board": "ulx3s_85f"}}}
    if merged == "a target":
        data = {**design, "targets": {"t": fragment}}
        selected = Design.from_file(write_design(tmp_path, data))
    else:
        selected = Design.from_file(write_design(tmp_path, design), overrides=fragment)
    assert selected.flow["vivado_synth"] == {"fpga": PART, "clock": {"period": 10.0}}
    assert selected.flow["nextpnr"] == {"seed": 3, "board": "ulx3s_85f"}


# ------------------------------------------------------------------------------ projects and removed keys


def write_project(tmp_path: Path, design: dict) -> Path:
    """A project file with `design` as its one entry, beside the design's sources."""
    write_design(tmp_path, {})  # the sources
    path = tmp_path / "project.json"
    path.write_text(json.dumps({"designs": [design]}))
    return path


@pytest.mark.parametrize(
    "design, overrides",
    [pytest.param(*row[1:3], id=row[0]) for row in OVERRIDES]
    + [pytest.param(rtl(), {"name": "renamed"}, id="name")],
)
def test_the_same_overrides_give_the_same_design_through_a_project_and_a_design_file(
    tmp_path, design, overrides
):
    """Overrides are applied once, when the design is selected, by one rule: a project's entry
    and a design file are the same design."""
    data = overlay(BASE, design)
    from_file = Design.from_file(write_design(tmp_path, data), overrides=overrides)
    project = XedaProject.from_file(write_project(tmp_path, data), design_overrides=overrides)
    from_project = project.get_design(from_file.name)
    assert from_project is not None, project.design_names
    assert from_project.model_dump(mode="json") == from_file.model_dump(mode="json")
    assert project.design_names == [from_file.name]
    assert project.designs == [data], "the entries stay as written"


@pytest.mark.parametrize("spelling", ["flows", "flow"])
@pytest.mark.parametrize("merged", ["plain", "a target", "overrides"])
def test_remove_extra_removes_a_key_in_any_spelling(tmp_path, spelling, merged):
    """A merge writes the field's name (`flow`), so the key to remove may be written either way."""
    data = {**BASE, "flows": {"vivado_synth": {"fpga": PART}}, "author": "A <a@example.com>"}
    if merged == "a target":
        data["targets"] = {"t": {"defines": {"A": 1}}}
    overrides = {"rtl": {"top": "knight"}} if merged == "overrides" else None
    path = write_design(tmp_path, data)
    kept = Design.from_file(path, overrides=overrides)
    assert kept.flow and kept.authors
    removed = Design.from_file(path, overrides=overrides, remove_extra=[spelling])
    assert removed.flow == {} and removed.authors == kept.authors
    other = "authors" if spelling == "flows" else "author"
    removed = Design.from_file(path, overrides=overrides, remove_extra=[other])
    assert removed.authors == [] and removed.flow == kept.flow


@pytest.mark.parametrize("spelling", ["flows", "flow"])
def test_design_remove_extra_of_a_project_removes_a_key_in_any_spelling(tmp_path, spelling):
    data = {**BASE, "flows": {"vivado_synth": {"fpga": PART}}}
    path = write_project(tmp_path, data)
    project = XedaProject.from_file(path, design_remove_extra=[spelling])
    assert [{"flow", "flows"} & set(entry) for entry in project.designs] == [set()]
    assert project.get_design(0).flow == {}  # type: ignore[union-attr]
