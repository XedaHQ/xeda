"""Every number in a settings model is a quantity or says what kind of number it is.

A setting that holds a physical measurement (a time, a frequency, a capacitance, ...) is a
quantity type of `xeda.units`, and the user writes its unit. Every other number carries one marker
(`ratio`, `percent`, `count`, `library_units`, `data_size`) that says what the plain number is. A
rule like this holds only while a test finds the next setting that breaks it. The sweep below walks
every settings model, recursively, and fails on a number that is neither.

A setting that a later change still converts is in `NOT_MIGRATED_YET`, and a map of tool
options whose values the flow hands to the tool in the tool's own words is in `PASS_THROUGH`. The
sweep keeps both tables true: an entry names an existing field, and a field that gains a unit or a
marker has to leave `NOT_MIGRATED_YET`. The first table may only shrink.
"""

import json
import typing
from collections.abc import Iterable, Mapping
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, ClassVar, Literal, Optional, Union

import annotated_types
import pytest
from pydantic import BaseModel

from xeda.flow import MetricSpec
from xeda.cli import _run_document
from xeda.dataclass import Field, XedaBaseModel
from xeda.flow import Flow, flowrun_hash
from xeda.flow_runner.dse import Dse, Optimizer
from xeda.flow_runner.trace import as_recorded
from xeda.flows import Quartus
from xeda.introspect import json_safe
from xeda.units import (
    MEASURE_KEY,
    MEASURES,
    Bounds,
    Capacitance,
    Frequency,
    Length,
    Power,
    Resistance,
    Time,
    Voltage,
    count,
    data_size,
    library_units,
    measure_of,
    percent,
    ratio,
)
from xeda.utils import dump_json, semantic_hash

from .flow_registry import preserved_flow_registry
from .settings_fields import (
    FieldKey,
    assign,
    fields_of,
    follow,
    kind_of,
    nest,
    quantity_paths,
    reachable_models,
    shape_of,
)
from .settings_samples import (
    QUANTITY_FIELD_IDS,
    QUANTITY_FIELDS,
    flow_classes,
    minimal_settings,
    with_quantity,
)
from .test_cli_json_contract import json_stdout, run_xeda

pytestmark = pytest.mark.python_compat

FMAX = "FmaxOptimizer.Settings"
DC_UNCHANGED = "dc is not changed in this branch"
SEARCH_UNCHANGED = "the design-space search is not changed in this branch"

#: Settings that a later change converts to quantities or marks: the physical ones, and every
#: number of dc and of the design-space search, which this branch does not change. A field leaves
#: this table when it becomes a quantity or gets a marker; the table never grows. Every entry says
#: why the setting is here.
NOT_MIGRATED_YET: dict[FieldKey, str] = {
    # simulation
    ("SimFlow.Settings", "stop_time"): "a time in nanoseconds, or text with a unit",
    ("SimFlow.Settings", "timeout"): "a time in seconds",
    ("VivadoSim.Settings", "prerun_time"): "a time, as text with a unit",
    ("Vcs.Settings", "time_unit"): "a time, as text such as 1ns",
    ("Vcs.Settings", "time_resolution"): "a time, as text such as 1ps",
    ("Vcs.Settings", "lic_wait"): "a waiting time, in the unit of the tool",
    # Vivado and nextpnr timing
    ("VivadoImplementation.Settings", "default_max_input_delay"): "a time in nanoseconds",
    ("VivadoImplementation.Settings", "default_max_output_delay"): "a time in nanoseconds",
    ("VivadoImplementation.Settings", "default_min_input_delay"): "a time in nanoseconds",
    ("VivadoImplementation.Settings", "default_min_output_delay"): "a time in nanoseconds",
    ("Nextpnr.Settings", "hold_detour_max"): "a time in nanoseconds",
    # programming
    ("Openfpgaloader.Settings", "freq"): "a frequency in hertz",
    # dc and the design-space search are not changed in this branch
    ("Dc.Settings", "default_max_input_delay"): DC_UNCHANGED,
    ("Dc.Settings", "default_max_output_delay"): DC_UNCHANGED,
    ("Dc.Settings", "max_routing_layer"): DC_UNCHANGED,
    ("Dc.Settings", "min_routing_layer"): DC_UNCHANGED,
    ("Dse.Settings", "max_failed_iters"): SEARCH_UNCHANGED,
    ("Dse.Settings", "max_failed_iters_with_best"): SEARCH_UNCHANGED,
    ("Dse.Settings", "max_runtime_minutes"): SEARCH_UNCHANGED,
    ("Dse.Settings", "max_workers"): SEARCH_UNCHANGED,
    ("Dse.Settings", "timeout"): SEARCH_UNCHANGED,
    (FMAX, "init_freq_low"): SEARCH_UNCHANGED,
    (FMAX, "init_freq_high"): SEARCH_UNCHANGED,
    (FMAX, "resolution"): SEARCH_UNCHANGED,
    (FMAX, "delta"): SEARCH_UNCHANGED,
    (FMAX, "min_improve_inc_variations"): SEARCH_UNCHANGED,
    (FMAX, "max_luts"): SEARCH_UNCHANGED,
    (FMAX, "init_num_variations"): SEARCH_UNCHANGED,
    (FMAX, "stop_after_no_improves"): SEARCH_UNCHANGED,
}

#: Maps of tool options. The flow hands each value to the tool in the tool's own words, so xeda
#: neither converts nor checks a unit.
PASS_THROUGH: dict[FieldKey, str] = {
    ("IseSynth.Settings", "synthesis_options"): "XST properties, spelled as ISE spells them",
    ("IseSynth.Settings", "map_options"): "map properties, spelled as ISE spells them",
    ("IseSynth.Settings", "pnr_options"): "par properties, spelled as ISE spells them",
    ("IseSynth.Settings", "translate_options"): "ngdbuild properties, spelled as ISE spells them",
    ("IseSynth.Settings", "trace_options"): "trce properties, spelled as ISE spells them",
    ("VivadoSynth.Settings", "set_synth_properties"): "run properties, set as given",
    ("VivadoSynth.Settings", "set_impl_properties"): "run properties, set as given",
}


def _bounds_metadata(annotation: Any, info: Any) -> list[Any]:
    """Return the `gt`, `ge`, `lt` and `le` limits that a field puts on its value."""
    metadata: list[Any] = list(info.metadata)
    pending = [annotation]
    while pending:
        item = pending.pop()
        if typing.get_origin(item) is Annotated:
            first, *rest = typing.get_args(item)
            pending.append(first)
            for entry in rest:
                metadata.extend(getattr(entry, "metadata", None) or [entry])
        else:
            pending.extend(typing.get_args(item))
    return [
        entry
        for entry in metadata
        if isinstance(
            entry, (annotated_types.Gt, annotated_types.Ge, annotated_types.Lt, annotated_types.Le)
        )
    ]


def _marker_of(info: Any) -> Any:
    extra = info.json_schema_extra
    return extra.get(MEASURE_KEY) if isinstance(extra, dict) else None


def numeric_problems(
    roots: Iterable[type[BaseModel]],
    not_migrated: Mapping[FieldKey, str],
    pass_through: Mapping[FieldKey, str],
) -> list[str]:
    """Return what is wrong with the numbers of the settings models `roots` and with the tables."""
    problems: list[str] = []
    models = reachable_models(roots)
    names: dict[str, type[BaseModel]] = {}
    for model in models:
        if names.setdefault(model.__qualname__, model) is not model:
            problems.append(f"two settings models are named {model.__qualname__}")

    seen = fields_of(models)

    for entries, table in ((not_migrated, "NOT_MIGRATED_YET"), (pass_through, "PASS_THROUGH")):
        for key, reason in entries.items():
            if key not in seen:
                problems.append(f"{table} names {key[0]}.{key[1]}, which is no field")
            if not reason.strip():
                problems.append(f"{table} gives no reason for {key[0]}.{key[1]}")
    for key in not_migrated.keys() & pass_through.keys():
        problems.append(f"{key[0]}.{key[1]} is in both tables")

    for (owner, name), (model, info) in sorted(seen.items()):
        where = f"{owner}.{name}"
        shape = shape_of(info.annotation)
        marker = _marker_of(info)
        for text in shape.unresolved:
            problems.append(f"{where}: the annotation {text!r} is not resolved")
        if marker is not None and marker not in MEASURES:
            problems.append(f"{where}: {marker!r} is no kind of number ({', '.join(MEASURES)})")
        is_quantity = shape.quantities and not shape.numbers
        is_numeric = shape.numbers or shape.quantities
        places = [
            place
            for place, present in (
                ("a quantity", is_quantity),
                ("a marker", marker is not None),
                ("NOT_MIGRATED_YET", (owner, name) in not_migrated),
                ("PASS_THROUGH", (owner, name) in pass_through),
            )
            if present
        ]
        if is_numeric and not places:
            problems.append(
                f"{where} is a number with no unit and no marker: make it a quantity of "
                f"xeda.units, or mark it with ratio(), percent(), count(), library_units() or "
                f"data_size()"
            )
        elif len(places) > 1:
            problems.append(f"{where} is in more than one place: {', '.join(places)}")
        elif places and not is_numeric and places != ["NOT_MIGRATED_YET"]:
            problems.append(f"{where} holds no number, but it is {places[0]}")
        if is_quantity and _bounds_metadata(info.annotation, info):
            problems.append(f"{where}: bound a quantity with Bounds(...), not Field(gt=...)")
        if marker == "library_units" and "in the units of the library" not in (
            info.description or ""
        ):
            problems.append(f'{where}: its description does not say "in the units of the library"')
        if marker == "percent" and not name.endswith("_percent"):
            problems.append(f"{where}: a percentage is named <name>_percent")
    return problems


def settings_models() -> list[type[BaseModel]]:
    """The settings of every registered flow and of the design-space search."""
    roots: list[type[BaseModel]] = [cls.Settings for cls, _ in flow_classes()]
    roots.append(Dse.Settings)
    pending = list(Optimizer.__subclasses__())
    while pending:
        optimizer = pending.pop()
        roots.append(optimizer.Settings)
        pending.extend(optimizer.__subclasses__())
    return roots


def test_every_number_of_every_settings_model_is_a_quantity_or_says_what_it_is():
    problems = numeric_problems(settings_models(), NOT_MIGRATED_YET, PASS_THROUGH)
    assert not problems, "\n" + "\n".join(problems)


def test_the_sweep_reaches_the_fields_of_the_models_nested_in_the_settings_of_the_flows():
    models = reachable_models(settings_models())
    fields = fields_of(models)
    for key in (
        ("Flow.Settings", "verbose"),  # inherited by every flow
        ("SimFlow.Settings", "timeout"),
        ("PhysicalClock", "period"),  # in a dict of models
        ("FPGA", "pins"),  # a model, Optional
        ("CocotbSettings", "random_seed"),
        ("AbcGroup", "delay_target"),  # a group in a group
        ("FmaxOptimizer.Settings", "init_freq_low"),  # the design-space search
        ("Dse.Settings", "timeout"),
    ):
        assert key in fields, key
    clock = fields[("PhysicalClock", "period")]
    assert shape_of(clock[1].annotation).quantities
    duty_cycle = fields[("PhysicalClock", "duty_cycle")]
    assert shape_of(duty_cycle[1].annotation).numbers
    assert measure_of(duty_cycle[0], "duty_cycle") == "ratio"
    assert len(models) > 40 and len(fields) > 500


# ------------------------------------------------------------------------------ the toy cases


@pytest.fixture
def toy_flow():
    """A factory of flows with one setting. Each flow registers itself, and the registry is
    restored after the test."""
    with preserved_flow_registry():

        def make(annotation: Any, default: Any, **extra: Any) -> type[Flow]:
            class ToyFlow(Flow):
                """A flow that exists to carry one setting."""

                results_description: ClassVar[dict[str, MetricSpec]] = {}

                class Settings(Flow.Settings):
                    knob: annotation = Field(  # type: ignore[valid-type]
                        default, description="A toy setting.", json_schema_extra=extra or None
                    )

                def run(self) -> None:
                    pass

            # The classes are local to this function. Name them as a module would.
            ToyFlow.__qualname__ = "ToyFlow"
            ToyFlow.Settings.__qualname__ = "ToyFlow.Settings"
            return ToyFlow

        yield make


def toy_problems(flow: type[Flow], **tables: Mapping[FieldKey, str]) -> list[str]:
    return numeric_problems(
        [flow.Settings], tables.get("not_migrated", {}), tables.get("pass_through", {})
    )


def test_a_registered_flow_is_in_the_sweep(toy_flow):
    flow = toy_flow(float, 1.0)
    assert flow in [cls for cls, _ in flow_classes()]
    assert flow.Settings in settings_models()


def test_a_quantity_in_any_slot_of_a_fixed_tuple_is_probed(toy_flow):
    """Each position of `tuple[A, B]` has its own type: the path to a quantity names its index,
    and the other slots get valid values."""
    second = toy_flow(tuple[str, Time], ("x", Time("1 ns")))
    (path,) = quantity_paths(second.Settings)
    assert [step.key for step in path] == ["knob", 1]
    assert nest(path, "10 ns") == {"knob": ["x", "10 ns"]}
    settings = second.Settings(**nest(path, "10 ns"))
    assert follow(settings, path) == Time("10 ns")
    assign(settings, path, "20 ns")
    assert follow(settings, path) == Time("20 ns")
    assert settings.knob == ("x", Time("20 ns"))

    both = toy_flow(tuple[Time, Time], (Time("1 ns"), Time("2 ns")))
    paths = quantity_paths(both.Settings)
    assert [[step.key for step in path] for path in paths] == [["knob", 0], ["knob", 1]]
    assert both.Settings(**nest(paths[0], "30 ns")).knob == (Time("30 ns"), Time("10 ns"))
    assert both.Settings(**nest(paths[1], "30 ns")).knob == (Time("10 ns"), Time("30 ns"))

    repeated = toy_flow(tuple[Time, ...], (Time("1 ns"),))
    (path,) = quantity_paths(repeated.Settings)
    assert [step.key for step in path] == ["knob", 0]


def test_an_unmarked_float_fails(toy_flow):
    (problem,) = toy_problems(toy_flow(float, 1.0))
    assert problem.startswith("ToyFlow.Settings.knob is a number with no unit and no marker")


@pytest.mark.parametrize(
    "annotation,default",
    [
        (int, 1),
        (Optional[int], None),
        (Optional[float], None),
        (Optional[list[float]], None),
        (list[int], []),
        (tuple[float, ...], ()),
        (tuple[int, str], (1, "a")),
        (dict[str, float], {}),
        (Mapping[str, Union[str, int]], {}),
        (Union[str, int, float], "x"),
        (Annotated[float, Field(gt=0)], 1.0),
        (Literal[0, 1, 2], 0),
        (Literal["auto"] | int, "auto"),
        (Union[int, Time], 1),
        (Optional[Union[Literal["auto"], int, Time]], None),
        (Decimal, Decimal(1)),
        (Optional[Decimal], None),
        (list[Decimal], []),
        (Fraction, Fraction(1, 2)),
        (Literal[Decimal(1)], Decimal(1)),
    ],
    ids=str,
)
def test_an_unmarked_number_fails_whatever_wraps_it(toy_flow, annotation, default):
    (problem,) = toy_problems(toy_flow(annotation, default))
    assert "ToyFlow.Settings.knob is a number with no unit and no marker" in problem


@pytest.mark.parametrize(
    "annotation,default",
    [
        (bool, False),
        (Optional[bool], None),
        (str, "x"),
        (Optional[str], None),
        (list[str], []),
        (dict[str, str], {}),
        (Literal["a", "b"], "a"),
        (Literal[True], True),
        (Literal["auto"] | bool, "auto"),
        (Time, Time("1 ns")),
        (Optional[Time], None),
        (list[Time], []),
        (dict[str, Time], {}),
        (Annotated[Time, Bounds(gt="0 ns")], Time("1 ns")),
        (Union[Literal["auto"], Time], "auto"),
        (Union[Literal["auto"], Annotated[Time, Bounds(ge="0 ns")], None], None),
    ],
    ids=str,
)
def test_a_flag_a_text_and_a_quantity_pass(toy_flow, annotation, default):
    assert toy_problems(toy_flow(annotation, default)) == []


def test_an_int_enum_is_a_choice_and_passes(toy_flow):
    class Level(int, Enum):
        LOW = 1
        HIGH = 2

    assert toy_problems(toy_flow(Level, Level.LOW)) == []


@pytest.mark.parametrize(
    "annotation,default,marker",
    [
        (float, 1.0, ratio()),
        (Optional[float], None, ratio()),
        (int, 1, count()),
        (Union[Literal["random"], int], "random", count()),
        (Optional[list[float]], None, ratio()),
        (Optional[int], None, data_size()),
        (Decimal, Decimal(1), ratio()),
    ],
)
def test_a_marked_number_passes(toy_flow, annotation, default, marker):
    assert toy_problems(toy_flow(annotation, default, **marker)) == []


class _Group(XedaBaseModel):
    leaf: int = 0


class _MarkedGroup(XedaBaseModel):
    leaf: int = Field(0, json_schema_extra=count())


def test_an_unmarked_leaf_of_a_nested_group_fails(toy_flow):
    flow = toy_flow(_Group, _Group())
    (problem,) = toy_problems(flow)
    assert problem.startswith("_Group.leaf is a number with no unit and no marker")


def test_a_marked_leaf_of_a_nested_group_passes(toy_flow):
    assert toy_problems(toy_flow(_MarkedGroup, _MarkedGroup())) == []


def test_an_unmarked_leaf_in_a_collection_of_groups_fails(toy_flow):
    for annotation, default in (
        (list[_Group], []),
        (dict[str, _Group], {}),
        (Optional[_Group], None),
    ):
        (problem,) = toy_problems(toy_flow(annotation, default))
        assert problem.startswith("_Group.leaf is a number")


def test_a_number_that_is_not_a_quantity_has_one_marker_or_one_table_entry(toy_flow):
    flow = toy_flow(float, 1.0)
    key = ("ToyFlow.Settings", "knob")
    assert toy_problems(flow, not_migrated={key: "a time"}) == []
    assert toy_problems(flow, pass_through={key: "a map"}) == []
    both = toy_problems(flow, not_migrated={key: "a time"}, pass_through={key: "a map"})
    assert any("is in both tables" in problem for problem in both)
    marked = toy_flow(float, 1.0, **ratio())
    problems = toy_problems(marked, not_migrated={key: "a time"})
    assert problems == [
        "ToyFlow.Settings.knob is in more than one place: a marker, NOT_MIGRATED_YET"
    ]


def test_an_entry_of_a_table_names_a_field_and_says_why(toy_flow):
    flow = toy_flow(float, 1.0, **ratio())
    assert toy_problems(flow, not_migrated={("ToyFlow.Settings", "knobs"): "a time"}) == [
        "NOT_MIGRATED_YET names ToyFlow.Settings.knobs, which is no field"
    ]
    assert toy_problems(flow, pass_through={("Elsewhere", "knob"): "a map"}) == [
        "PASS_THROUGH names Elsewhere.knob, which is no field"
    ]
    plain = toy_flow(float, 1.0)
    key = ("ToyFlow.Settings", "knob")
    assert toy_problems(plain, not_migrated={key: " "}) == [
        "NOT_MIGRATED_YET gives no reason for ToyFlow.Settings.knob"
    ]


def test_a_converted_setting_must_leave_not_migrated_yet(toy_flow):
    """A quantity, or a marked number, in the table fails: the entry has to go."""
    key = ("ToyFlow.Settings", "knob")
    quantity = toy_flow(Optional[Time], None)
    assert toy_problems(quantity, not_migrated={key: "a time"}) == [
        "ToyFlow.Settings.knob is in more than one place: a quantity, NOT_MIGRATED_YET"
    ]


def test_a_text_that_holds_a_time_may_be_listed_as_not_migrated_yet(toy_flow):
    key = ("ToyFlow.Settings", "knob")
    text = toy_flow(Optional[str], None)
    assert toy_problems(text, not_migrated={key: "a time, as text"}) == []
    assert toy_problems(text, pass_through={key: "options"}) == [
        "ToyFlow.Settings.knob holds no number, but it is PASS_THROUGH"
    ]


def test_a_quantity_with_a_marker_fails(toy_flow):
    problems = toy_problems(toy_flow(Time, Time("1 ns"), **count()))
    assert problems == ["ToyFlow.Settings.knob is in more than one place: a quantity, a marker"]


def test_a_marker_on_a_field_without_a_number_fails(toy_flow):
    problems = toy_problems(toy_flow(str, "x", **count()))
    assert problems == ["ToyFlow.Settings.knob holds no number, but it is a marker"]


def test_a_marker_that_names_no_kind_of_number_fails(toy_flow):
    problems = toy_problems(toy_flow(float, 1.0, **{MEASURE_KEY: "lots"}))
    assert any("'lots' is no kind of number" in problem for problem in problems)


def test_library_units_need_the_words_in_the_description(toy_flow):
    flow = toy_flow(float, 1.0, **library_units())
    (problem,) = toy_problems(flow)
    assert problem.endswith('its description does not say "in the units of the library"')
    flow.Settings.model_fields["knob"].description = "A value in the units of the library."
    assert toy_problems(flow) == []


def test_a_percentage_is_named_with_percent(toy_flow):
    flow = toy_flow(float, 1.0, **percent())
    assert toy_problems(flow) == ["ToyFlow.Settings.knob: a percentage is named <name>_percent"]


def test_a_quantity_is_bounded_with_bounds_not_with_field_limits(toy_flow):
    flow = toy_flow(Time, Time("1 ns"))
    assert toy_problems(flow) == []
    # Pydantic cannot build a quantity with `Field(gt=...)`; the limit is added after the build.
    flow.Settings.model_fields["knob"].metadata.append(annotated_types.Gt(0))
    assert toy_problems(flow) == [
        "ToyFlow.Settings.knob: bound a quantity with Bounds(...), not Field(gt=...)"
    ]


def test_the_limits_of_a_number_are_no_problem(toy_flow):
    flow = toy_flow(Annotated[float, Field(gt=0.0, lt=1.0)], 0.5, **ratio())
    assert toy_problems(flow) == []


def test_two_models_with_one_name_fail(toy_flow):
    class Twin(XedaBaseModel):
        leaf: int = Field(0, json_schema_extra=count())

    first, second = Twin, type("Twin", (Twin,), {"__qualname__": Twin.__qualname__})
    assert numeric_problems([first, second], {}, {}) == [
        f"two settings models are named {Twin.__qualname__}"
    ]


# ------------------------------------------------------------------------------ identity

#: Spellings of one value of each kind, and another value.
EQUIVALENT: dict[type, tuple[str, ...]] = {
    Time: ("10 ns", "10000 ps", "0.01 us"),
    Frequency: ("100 MHz", "0.1 GHz", "100000 kHz"),
    Capacitance: ("2 fF", "0.002 pF"),
    Length: ("3 um", "3000 nm", "0.003 mm"),
    Voltage: ("1.2 V", "1200 mV"),
    Resistance: ("5 ohm", "0.005 kohm"),
    Power: ("2 mW", "0.002 W", "2000 uW"),
}
DIFFERENT: dict[type, str] = {
    Time: "11 ns",
    Frequency: "101 MHz",
    Capacitance: "3 fF",
    Length: "4 um",
    Voltage: "1.3 V",
    Resistance: "6 ohm",
    Power: "3 mW",
}


_kind_of = kind_of


@pytest.mark.parametrize("field", QUANTITY_FIELDS, ids=QUANTITY_FIELD_IDS)
def test_equivalent_spellings_of_a_quantity_setting_give_one_hash(field):
    cls, path = field
    kind = _kind_of(path)

    def identity(text: str) -> str:
        return flowrun_hash(cls.name, with_quantity(cls, path, text), "d")

    hashes = {text: identity(text) for text in EQUIVALENT[kind]}
    assert len(set(hashes.values())) == 1, hashes
    assert identity(DIFFERENT[kind]) not in hashes.values()


CLOCK_FLOWS = [cls for cls, _ in flow_classes() if "clocks" in cls.Settings.model_fields]


@pytest.mark.parametrize("cls", CLOCK_FLOWS, ids=[cls.name for cls in CLOCK_FLOWS])
def test_a_clock_hashes_by_its_value_in_whatever_unit_and_as_period_or_frequency(cls):
    def identity(**clock: str) -> str:
        given = {**minimal_settings(cls), "clock": clock}
        return flowrun_hash(cls.name, cls.Settings(**given), "d")

    period = identity(period="10 ns")
    assert period == identity(period="10000 ps") == identity(period="0.01 us")
    assert period == identity(freq="100 MHz") == identity(freq="0.1 GHz")
    assert period == identity(freq="100000 kHz") == identity(period="10 ns", freq="100 MHz")
    assert period != identity(period="11 ns")
    assert period != identity(freq="101 MHz")
    assert identity(period="10 ns", rise="1 ns") == identity(period="10 ns", rise="1000 ps")
    assert identity(period="10 ns", rise="1 ns") != identity(period="10 ns", rise="2 ns")


def test_a_quantity_in_a_plain_container_hashes_by_its_value():
    def hashed(*times: str):
        return semantic_hash({"a": [Time(text) for text in times], "b": {"c": Time(times[0])}})

    assert hashed("10 ns", "1 us") == hashed("10000 ps", "1000 ns")
    assert hashed("10 ns", "1 us") != hashed("10 ns", "2 us")
    # the hash of the number that a quantity replaces, so a run keeps its identity
    assert semantic_hash([Time("10 ns")]) == semantic_hash([10.0])


# ------------------------------------------------------------------------------ writers


def _quantities() -> list:
    """One quantity of each kind, written in a unit that is not the canonical one."""
    return [
        Time("10000 ps"),
        Frequency("0.1 GHz"),
        Capacitance("0.002 pF"),
        Length("3000 nm"),
        Voltage("1200 mV"),
        Resistance("0.005 kohm"),
        Power("2000 uW"),
    ]


CANONICAL = ["10 ns", "100 MHz", "2 fF", "3 um", "1.2 V", "5 ohm", "0.002 W"]


def _in_plain_containers() -> dict:
    """The quantities in a dict, a list, a tuple, and containers nested in them."""
    quantities = _quantities()
    return {
        "dict": {q.kind: q for q in quantities},
        "list": list(quantities),
        "tuple": tuple(quantities),
        "nested": [{"deep": (quantities[0], [quantities[1]])}],
    }


def _assert_written_as_text(written: Any) -> None:
    assert written == {
        "dict": {q.kind: text for q, text in zip(_quantities(), CANONICAL)},
        "list": CANONICAL,
        "tuple": CANONICAL,
        "nested": [{"deep": [CANONICAL[0], [CANONICAL[1]]]}],
    }
    # Each reloads to an equal value.
    for quantity, text in zip(_quantities(), written["list"]):
        assert type(quantity)(text) == quantity


def test_dump_json_writes_a_quantity_in_a_plain_container_as_text(tmp_path):
    dump_json(_in_plain_containers(), tmp_path / "plain.json", backup=False)
    _assert_written_as_text(json.loads((tmp_path / "plain.json").read_text()))


def test_the_trace_records_a_quantity_in_a_plain_container_as_text():
    _assert_written_as_text(as_recorded(_in_plain_containers()))


def test_json_safe_writes_a_quantity_in_a_plain_container_as_text():
    _assert_written_as_text(json_safe(_in_plain_containers()))


def test_the_run_document_writes_a_quantity_in_the_results_as_text():
    flow = SimpleNamespace(
        name="toy",
        run_path=Path("run"),
        design=SimpleNamespace(name="d", target=None),
        results=_in_plain_containers(),
    )
    document = _run_document("toy", None, flow, True)
    _assert_written_as_text(document["results"])
    _assert_written_as_text(json.loads(json.dumps(document))["results"])


@pytest.mark.parametrize("cls", CLOCK_FLOWS, ids=[cls.name for cls in CLOCK_FLOWS])
def test_the_json_of_settings_with_a_clock_writes_the_clock_as_text_and_reloads(cls):
    settings = cls.Settings(
        **{**minimal_settings(cls), "clock": {"freq": "0.1 GHz", "rise": "1000 ps"}}
    )
    written = json.loads(settings.model_dump_json())
    assert {key: written["clocks"]["main_clock"][key] for key in ("period", "rise")} == {
        "period": "10 ns",
        "rise": "1 ns",
    }
    reloaded = cls.Settings(**written)
    assert reloaded.clocks == settings.clocks
    assert reloaded.model_dump() == settings.model_dump()


def test_a_run_with_a_clock_prints_one_document_and_writes_the_clock_as_text(tmp_path):
    """The document of `xeda run --json` echoes no settings. It names `settings_json`, and that
    file, with the effective settings, holds the clock as its canonical text."""
    design = Path(__file__).parent / "resources" / "design0" / "design0.toml"
    proc = run_xeda(
        "run",
        "quartus",
        str(design),
        "-s",
        "fpga=10CL025YU256C8G",
        "-s",
        "clock.period=5500 ps",
        "--json",
        cwd=tmp_path,
        fake_tools=True,
    )
    document = json_stdout(proc)
    assert document["success"] is True, proc.stderr
    assert "clocks" not in document and "5.5 ns" not in json.dumps(document)
    settings = json.loads(Path(document["settings_json"]).read_text())
    for key in ("flow_settings", "effective_flow_settings"):
        clock = settings[key]["clocks"]["main_clock"]
        assert (clock["period"], clock["rise"]) == ("5.5 ns", "0 ns")
    assert Quartus.Settings(**settings["flow_settings"]).clocks["main_clock"].period == Time(
        "5.5 ns"
    )
