"""The ABC options of `yosys` that measure something take quantities with units.

`abc.delay_target` is a time, `abc.max_slew` is a time and `abc.load` is a capacitance. Each is
text with a unit, such as `500 ps`. The flow converts it to the unit that ABC reads: picoseconds
for `-D` and `buffer -S`, femtofarads for `set_load`. A bare number is refused. Every other
number of the settings of the yosys flows says whether it is a count or a ratio.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from xeda.dataclass import ValidationError
from xeda.flow import FlowSettingsError, flowrun_hash
from xeda.flows import Yosys, YosysFpga, YosysSim
from xeda.flows.yosys.abc_strategies import Composition, composed_abc_script
from xeda.flows.yosys.asic_groups import AbcGroup
from xeda.units import Capacitance, Time, measure_of
from xeda.utils import semantic_hash

from .test_yosys_asic import SCRIPT_FORMATS, _commands, _scripted_launch, _tiny, _words
from .test_yosys_asic_groups import _abc_script, _run

REFUSED = (FlowSettingsError, ValidationError, ValueError)


def _abc(**leaves: Any) -> Yosys.Settings:
    return Yosys.Settings.from_input({"abc": leaves})


def _constraints(commands: list[list[str]]) -> str:
    """The text of the ABC constraints file (`-constr`) that the script names."""
    (abc,) = _words(commands, "abc")
    return abc[abc.index("-constr") + 1]


# ------------------------------------------------------------------------------ the model


def test_the_abc_measures_are_quantities():
    abc = _abc(delay_target="0.5 ns", max_slew="100 ps", load="3.898 fF")
    assert abc.abc.delay_target == Time("500 ps")
    assert abc.abc.max_slew == Time("100 ps")
    assert abc.abc.load == Capacitance("3.898 fF")
    assert isinstance(abc.abc.load, Capacitance)


def test_the_abc_measures_default_to_unset_and_auto_is_a_delay_target():
    abc = Yosys.Settings().abc
    assert (abc.delay_target, abc.max_slew, abc.load) == (None, None, None)
    assert _abc(delay_target="auto").abc.delay_target == "auto"


@pytest.mark.parametrize(
    "leaf,value,message",
    [
        ("delay_target", 500, "needs a unit"),
        ("delay_target", "500", "needs a unit"),
        ("delay_target", -1, "needs a unit"),
        ("delay_target", "-1 ps", "must be at least 0 ns"),
        ("delay_target", "5 MHz", "is a frequency, not a time"),
        ("delay_target", "Auto", "cannot read `Auto` as a time: did you mean `auto`"),
        ("delay_target", " AUTO ", "did you mean `auto`"),
        ("delay_target", "fast", "such as `10 ns`; or write `auto`"),
        ("max_slew", 100, "needs a unit"),
        ("max_slew", "100.5 ps", "got 100.5 ps: ABC's `buffer -S` takes an integer"),
        ("max_slew", "0 ps", "at least 1 ps and a whole number of picoseconds"),
        ("max_slew", "0.4 ps", "at least 1 ps and a whole number of picoseconds"),
        ("max_slew", "-5 ps", "at least 1 ps and a whole number of picoseconds"),
        ("max_slew", "5 fF", "is a capacitance, not a time"),
        ("load", 3.898, "needs a unit"),
        ("load", "3.898", "needs a unit"),
        ("load", "-1 fF", "at least 0 fF"),
        ("load", "1 ps", "is a time, not a capacitance"),
    ],
)
def test_an_abc_measure_without_a_valid_unit_is_refused(leaf, value, message):
    with pytest.raises(REFUSED, match=message):
        _abc(**{leaf: value})


def test_a_whole_number_of_picoseconds_is_accepted_in_any_unit():
    for text in ("100 ps", "0.1 ns", "1e-4 us", "100000 fs"):
        assert _abc(max_slew=text).abc.max_slew == Time("100 ps")
    assert _abc(max_slew="1 ps").abc.max_slew == Time("1 ps")
    assert _abc(max_slew="0.0001 us").abc.max_slew == Time("0.1 ns")


def test_the_check_of_the_slew_reads_only_the_value_of_the_field():
    """Assignment re-runs the validators, and they must not read another field."""
    settings = Yosys.Settings()
    settings.abc.max_slew = "150 ps"  # type: ignore[assignment]
    assert settings.abc.max_slew == Time("150 ps")
    with pytest.raises(REFUSED, match="whole number of picoseconds"):
        settings.abc.max_slew = "1.5 ps"  # type: ignore[assignment]


@pytest.mark.parametrize("value", [500, "500", "-1 ps", "5 MHz"])
def test_only_text_that_is_no_time_at_all_suggests_auto(value):
    with pytest.raises(REFUSED) as raised:
        _abc(delay_target=value)

    assert "auto" not in str(raised.value)


def test_a_bad_delay_target_names_the_quantity_problem():
    """The union of `auto` and a time must not hide the message about the unit."""
    with pytest.raises(REFUSED) as raised:
        _abc(delay_target=500)
    text = str(raised.value)
    assert "needs a unit, such as `10 ns`" in text
    assert "Input should be 'auto'" not in text


def test_the_schema_of_the_delay_target_is_auto_or_a_time():
    properties = Yosys.Settings.model_json_schema()["$defs"]["AbcGroup"]["properties"]
    options = properties["delay_target"]["anyOf"]
    assert {"const": "auto", "type": "string"} in options
    (time,) = [option for option in options if option.get("x-xeda-quantity") == "time"]
    assert time["type"] == "string"
    assert {"type": "null"} in options
    assert len(options) == 3
    assert properties["max_slew"]["anyOf"][0]["x-xeda-quantity"] == "time"
    assert properties["load"]["anyOf"][0]["x-xeda-quantity"] == "capacitance"


def test_the_descriptions_of_the_measures_say_what_they_are():
    fields = Yosys.Settings.model_fields["abc"].annotation.model_fields  # type: ignore[union-attr]
    assert "flip-flop" not in (fields["load"].description or "")
    assert "capacitance" in (fields["load"].description or "")
    assert "`500 ps`" in (fields["delay_target"].description or "")


def test_the_settings_of_the_abc_measures_round_trip_through_json():
    settings = _abc(delay_target="0.5 ns", max_slew="100 ps", load="0.003898 pF").model_dump(
        mode="json"
    )
    assert settings["abc"]["delay_target"] == "0.5 ns"
    assert settings["abc"]["max_slew"] == "0.1 ns"
    assert settings["abc"]["load"] == "3.898 fF"
    again = Yosys.Settings.model_validate(json.loads(json.dumps(settings)))
    assert again.abc.delay_target == Time("500 ps")


def test_the_checks_that_compare_with_defaults_work_with_quantity_values():
    def keys(**leaves):
        return [key for key, _ in AbcGroup(**leaves).problems("abc")]  # type: ignore[arg-type]

    assert keys(max_slew="100 ps") == ["abc.max_slew"]
    assert keys(strategy="area_0", max_slew="100 ps") == ["abc.max_slew"]
    assert keys(strategy="area_0", buffering=True, max_slew="100 ps") == []
    assert keys(clock_derating=0.1, delay_target="500 ps") == ["abc.clock_derating"]
    assert keys(clock_derating=0.1, delay_target="auto") == []
    assert keys(clock_derating=0.1) == ["abc.clock_derating"]


def test_a_quantity_changes_the_identity_only_by_its_canonical_value():
    def identity(**leaves):
        return flowrun_hash("yosys", Yosys.Settings.from_input({"abc": leaves}), "d")

    assert identity(delay_target="500 ps") == identity(delay_target="0.5 ns")
    assert identity(load="3.898 fF") == identity(load="0.003898 pF")
    assert identity(delay_target="500 ps") != identity(delay_target="600 ps")
    assert identity() == identity(delay_target=None)


def test_the_load_keeps_the_hash_of_the_number_of_femtofarads_it_was():
    """Femtofarads were the unit of `abc.load` already. Times were in picoseconds, and a time is
    now hashed in nanoseconds."""
    assert semantic_hash(Capacitance("2.5 fF")) == semantic_hash(2.5)
    assert semantic_hash(Time("500 ps")) != semantic_hash(500.0)


# ------------------------------------------------------------------------------ the scripts


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "target,expected",
    [("500 ps", "500.000"), ("0.5 ns", "500.000"), ("1.2345 ns", "1234.500"), (None, None)],
)
def test_the_delay_target_is_the_d_flag_in_picoseconds(
    tmp_path, monkeypatch, script_format, target, expected
):
    commands = _run(tmp_path, monkeypatch, script_format, abc={"delay_target": target})
    (abc,) = _words(commands, "abc")
    if expected is None:
        assert "-D" not in abc
    else:
        assert abc[abc.index("-D") + 1] == expected


@SCRIPT_FORMATS
def test_the_auto_delay_target_takes_the_derated_clock_period(tmp_path, monkeypatch, script_format):
    given = _tiny(
        tmp_path,
        abc={"delay_target": "auto", "clock_derating": 0.1},
    )
    given["clocks"] = {"main_clock": {"period": "2.5 ns"}}
    commands = _commands(
        _scripted_launch(tmp_path, monkeypatch, Yosys, given), script_format, tmp_path
    )
    (abc,) = _words(commands, "abc")
    assert abc[abc.index("-D") + 1] == "2250.000"


@SCRIPT_FORMATS
@pytest.mark.parametrize("slew", ["100 ps", "0.1 ns"])
def test_the_slew_is_the_s_option_of_buffer_in_picoseconds(
    tmp_path, monkeypatch, script_format, slew
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        abc={"strategy": "area_0", "buffering": True, "max_slew": slew},
    )
    script = _abc_script(commands)
    assert script is not None and ";buffer,-S,100;" in script


@SCRIPT_FORMATS
@pytest.mark.parametrize("load", ["3.898 fF", "0.003898 pF"])
def test_the_load_is_the_set_load_of_the_constraints_in_femtofarads(
    tmp_path, monkeypatch, script_format, load
):
    run_dir_commands = _run(tmp_path, monkeypatch, script_format, abc={"load": load})
    constr = _constraints(run_dir_commands)
    assert constr
    text = _read_constraints(tmp_path, constr)
    assert "set_load 3.898\n" in text + "\n"


def _read_constraints(root, name):
    for path in Path(root).rglob(Path(name).name):
        if path.is_file():
            return path.read_text()
    raise AssertionError(f"no constraints file {name}")


@SCRIPT_FORMATS
def test_an_unset_load_is_the_one_of_the_platform_unchanged(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    text = _read_constraints(tmp_path, _constraints(commands))
    assert "set_load 3.5" in text


def test_the_composition_takes_a_whole_number_of_picoseconds():
    script = composed_abc_script("area_0", Composition(buffering=True, max_slew=100))
    assert "buffer,-N" not in script and ";buffer,-S,100;" in script


# ------------------------------------------------------------------------------ the markers

#: Every plain number of the settings of the yosys flows, with what it measures.
MEASURES = {
    (Yosys, "hierarchy.keep_min_cost"): "count",
    (Yosys, "arithmetic.extract_fa.max_depth"): "count",
    (Yosys, "arithmetic.extract_fa.max_breadth"): "count",
    (Yosys, "memory.max_bits"): "count",
    (Yosys, "sequential.clock_gating.min_width"): "count",
    (Yosys, "sequential.min_enable_fanout"): "count",
    (Yosys, "sequential.min_sync_reset_fanout"): "count",
    (Yosys, "abc.clock_derating"): "ratio",
    (Yosys, "abc.max_fanout"): "count",
    (YosysFpga, "widemux"): "count",
    (YosysSim, "cxxrtl.debug"): "count",
    (YosysSim, "cxxrtl.opt"): "count",
}


def _model_and_field(flow, path):
    model = flow.Settings
    *groups, leaf = path.split(".")
    for name in groups:
        annotation = model.model_fields[name].annotation
        model = next(
            arg
            for arg in (getattr(annotation, "__args__", None) or (annotation,))
            if isinstance(arg, type) and hasattr(arg, "model_fields")
        )
    return model, leaf


@pytest.mark.parametrize(
    "flow,path", list(MEASURES), ids=[f"{f.__name__}.{p}" for f, p in MEASURES]
)
def test_a_plain_number_of_a_yosys_flow_says_what_it_measures(flow, path):
    model, leaf = _model_and_field(flow, path)
    assert measure_of(model, leaf) == MEASURES[(flow, path)]


def test_the_markers_keep_the_tiers():
    from xeda.dataclass import tier_of

    model, leaf = _model_and_field(Yosys, "abc.clock_derating")
    assert tier_of(model, leaf) == "expert"
    model, leaf = _model_and_field(Yosys, "abc.max_fanout")
    assert tier_of(model, leaf) == "advanced"
