"""Pint is the one public quantity type, in both directions.

`RunRecord.quantity` and `SweepResult.rows(quantities=True)` give pint quantities. A quantity
setting takes a pint quantity from Python. The unit mapping between the two is checked in each
direction against a registry with the default definitions of pint.
"""

import copy
import json
import pickle
import sys
from decimal import Decimal
from fractions import Fraction

import pint
import pytest

from xeda import Design, LocalSettings, Spec, Workspace
from xeda._internal.dataclass import ValidationError
from xeda._internal.flow.flow import flowrun_hash
from xeda._internal.flows import GhdlSim, Yosys
from xeda._internal.frozen import FrozenDict
from xeda._internal.records import decode_record
from xeda._internal.sweep import _metric_quantities
from xeda._internal.units import (
    METRIC_ONLY_UNITS,
    Quantity,
    QuantityError,
    Time,
    pint_quantity_text,
    pint_unit_text,
    verify_unit,
)
from xeda._internal.utils import json_encodable

from .record_documents import record_document
from .test_sweep import _spec, _workspace, design, isolate_registration  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")


@pytest.fixture
def application():
    """The application registry of pint, put back after the test."""
    wrapper = pint.get_application_registry()
    previous = wrapper.get()
    yield wrapper
    wrapper.set(previous)


def Q(value, unit):
    return pint.get_application_registry().Quantity(value, unit)


# ---------------------------------------------------------------------------------- the mapping


@pytest.mark.python_compat
def test_every_unit_of_xeda_means_the_same_in_a_fresh_pint_registry():
    registry = pint.UnitRegistry()
    canonical = {}
    for text, kind in Quantity._kind_of_unit.items():
        canonical[kind] = kind.canonical_unit
        verify_unit(registry, text)
        want = registry.Quantity(1, text).to(registry.parse_units(kind.canonical_unit))
        factor = float(kind._factors[text])
        assert want.magnitude == pytest.approx(factor, rel=1e-12), text
    assert set(canonical.values()) == {kind.canonical_unit for kind in set(canonical)}
    for text, base in {
        "degC": "degree_Celsius",
        "delta_degC": "delta_degree_Celsius",
        "K/W": "kelvin / watt",
        "um^2": "micrometer ** 2",
    }.items():
        assert text in METRIC_ONLY_UNITS
        assert registry.parse_units(text) == registry.parse_units(base)
        verify_unit(registry, text)


def test_a_unit_that_pint_has_and_xeda_has_not_has_no_text():
    registry = pint.get_application_registry()
    assert pint_unit_text(registry.parse_units("kilosecond"), registry) is None
    assert pint_unit_text(registry.parse_units("MHz"), registry) == "MHz"
    assert pint_unit_text(registry.parse_units("minute"), registry) == "min"
    assert pint_unit_text(registry.parse_units("um ** 2"), registry) == "um^2"


def test_a_registry_that_defines_a_unit_again_has_no_text_for_it():
    registry = pint.UnitRegistry()
    registry.define("nanosecond = 1 * second = ns")
    assert pint_unit_text(registry.parse_units("ns"), registry) is None
    with pytest.raises(ValueError, match="`ns`"):
        verify_unit(registry, "ns")
    for factor in ("1e-3", "1e3"):  # a unit that got larger, and one that got smaller
        other = pint.UnitRegistry()
        other.define(f"nanosecond = {factor} * second = ns")
        assert pint_unit_text(other.parse_units("ns"), other) is None


# ------------------------------------------------------------------------------------ quantity()


def _record(tmp_path, metrics, info):
    document = record_document(metrics=metrics)
    document["metric_info"] = {
        path: {"description": "d", "type": kind, "unit": unit}
        for path, (kind, unit) in info.items()
    }
    return decode_record(document, run_dir=tmp_path / "d" / "flow", run_root=tmp_path)


@pytest.fixture
def record(tmp_path):
    return _record(
        tmp_path,
        {
            "fmax": 250.5,
            "lut": 12,
            "sim": {"time": 123456789012345678, "ended_by": "finish"},
            "power": {"static": 1.5, "dynamic": 0.25},
            "clocks": {"clk": 100.0, "slow": 10.0},
            "cells": {"and": 3},
            "meta": {"a": 1.0, "b": 2},
            "ok": True,
            "info": {"x": 1},
            "t": {"delay": 2.5, "count": 4},
            "temp": 40.0,
            "area": 3.0,
            "rth": 0.5,
            "dt": 2.0,
        },
        {
            "fmax": ("number", "MHz"),
            "lut": ("integer", None),
            "sim.time": ("integer", "fs"),
            "sim.ended_by": ("text", None),
            "power": ("numbers", "W"),
            "clocks": ("numbers", "MHz"),
            "cells": ("counts", None),
            "meta": ("object", None),
            "ok": ("boolean", None),
            "info": ("object", "ns"),
            "t.delay": ("number", "ns"),
            "t.count": ("integer", None),
            "temp": ("number", "degC"),
            "area": ("number", "um^2"),
            "rth": ("number", "K/W"),
            "dt": ("number", "delta_degC"),
        },
    )


def test_a_number_is_a_quantity_with_the_stored_value_and_the_unit(record):
    fmax = record.quantity("fmax")
    assert isinstance(fmax, pint.Quantity)
    assert fmax == Q(250.5, "MHz") and fmax.magnitude == 250.5
    assert str(fmax.units) == "megahertz"
    assert record.metric("fmax") == 250.5, "metric() is unchanged"


def test_an_integer_stays_exact(record):
    time = record.quantity("sim.time")
    assert time.magnitude == 123456789012345678 and isinstance(time.magnitude, int)
    assert time.to("ns").magnitude == pytest.approx(123456789012.345678)


@pytest.mark.parametrize(
    "path, unit", [("temp", "degC"), ("area", "um^2"), ("rth", "K/W"), ("dt", "delta_degC")]
)
def test_a_unit_that_only_metrics_have_is_a_quantity_too(record, path, unit):
    quantity = record.quantity(path)
    assert quantity.magnitude == record.metric(path)
    assert quantity.units == pint.get_application_registry().parse_units(unit)


def test_a_table_of_numbers_is_a_read_only_table_of_quantities(record):
    power = record.quantity("power")
    assert isinstance(power, FrozenDict)
    assert dict(power) == {"static": Q(1.5, "W"), "dynamic": Q(0.25, "W")}
    with pytest.raises(TypeError):
        power["static"] = Q(1, "W")  # type: ignore[index]


@pytest.mark.parametrize(
    "path, problem",
    [
        ("lut", "integer"),
        ("sim.ended_by", "text"),
        ("ok", "boolean"),
        ("cells", "counts"),
        ("meta", "object"),
        ("t.count", "integer"),
        ("info", "object"),
    ],
)
def test_a_metric_that_has_no_unit_or_no_number_raises_value_error(record, path, problem):
    with pytest.raises(ValueError, match=rf"`{path}`.*{problem}.*`metric`"):
        record.quantity(path)


@pytest.mark.parametrize("path", ["nosuch", "sim.nosuch", "fmax.x", "sim.time.x"])
def test_a_path_that_is_no_metric_raises_key_error(record, path):
    with pytest.raises(KeyError):
        record.quantity(path)


def test_a_group_path_raises_key_error_and_says_why(record):
    with pytest.raises(KeyError, match="group has no unit"):
        record.quantity("sim")


def test_a_unit_text_that_xeda_does_not_know_is_refused_before_pint_sees_it(tmp_path):
    for unit in ("kilosecond", "ns * 2", "__import__('os')", "s ** 100"):
        record = _record(tmp_path, {"x": 1.0}, {"x": ("number", unit)})
        with pytest.raises(ValueError, match="not known"):
            record.quantity("x")


def test_the_registry_is_the_application_registry_at_the_time_of_the_call(record, application):
    before = record.quantity("fmax")
    private = pint.UnitRegistry()
    pint.set_application_registry(private)
    after = record.quantity("fmax")
    assert after._REGISTRY is private
    assert after.magnitude == before.magnitude
    assert after + private.Quantity(1, "MHz") == private.Quantity(251.5, "MHz")


def test_a_registry_that_defines_a_unit_again_is_refused(record, application):
    private = pint.UnitRegistry()
    private.define("megahertz = 1 * hertz = MHz")
    pint.set_application_registry(private)
    with pytest.raises(ValueError, match="`MHz`"):
        record.quantity("fmax")


def test_a_setting_cell_refuses_a_registry_that_defines_its_unit_again(application):
    from xeda._internal.introspect import settings_as_quantities

    private = pint.UnitRegistry()
    private.define("nanosecond = 1 * second = ns")
    pint.set_application_registry(private)
    with pytest.raises(ValueError, match="`ns`"):
        settings_as_quantities("ghdl_sim", {"stop_time": "10 ns"})


def test_each_call_builds_new_objects(record):
    first = record.quantity("fmax")
    first.ito("kHz")
    assert record.quantity("fmax") == Q(250.5, "MHz")
    table = record.quantity("power")
    table["static"].ito("mW")
    assert record.quantity("power")["static"] == Q(1.5, "W")


def test_a_quantity_pickles_and_adds_to_the_quantity_of_a_user(record):
    fmax = record.quantity("fmax")
    loaded = pickle.loads(pickle.dumps(fmax))
    assert loaded == fmax
    assert loaded + fmax == Q(501.0, "MHz")
    mine = pint.get_application_registry().Quantity(1.5, "GHz")
    assert (fmax + mine).to("MHz").magnitude == pytest.approx(1750.5)


# ----------------------------------------------------------------------------------- the sweep


def test_a_mixed_row_has_quantities_where_a_metric_has_a_unit(tmp_path, design):  # noqa: F811
    spec = _spec(design, 1, **{"nextpnr.clocks.main_clock.period": "0.000000001 s"})
    result = _workspace(tmp_path).sweep([spec])
    metrics = ("nextpnr.fmax", "nextpnr.wns", "nextpnr.lut", "nextpnr.timing_met")
    plain = result.rows(metrics=metrics)[0]
    row = result.rows(metrics=metrics, quantities=True)[0]
    assert row["nextpnr.fmax"] == Q(plain["nextpnr.fmax"], "MHz")
    assert row["nextpnr.wns"] == Q(plain["nextpnr.wns"], "ns")
    assert row["nextpnr.lut"] == plain["nextpnr.lut"] and not isinstance(
        row["nextpnr.lut"], pint.Quantity
    )
    assert row["nextpnr.timing_met"] is plain["nextpnr.timing_met"]
    assert result.rows(metrics=metrics) == result.rows(metrics=metrics, quantities=False)
    for key in ("index", "flow", "design", "outcome", "settings.nextpnr.seed", "run_dir"):
        assert row[key] == plain[key]
    # the setting is the value of the field, not text and not a float of another unit
    period = row["settings.nextpnr.clocks.main_clock.period"]
    assert plain["settings.nextpnr.clocks.main_clock.period"] == "1 ns"
    assert period == Q(1, "ns") and period.magnitude == 1.0
    assert row["settings.nextpnr.fpga.part"] == plain["settings.nextpnr.fpga.part"]
    pytest.importorskip("pandas")
    frame = result.to_pandas(metrics=metrics, quantities=True)
    assert frame["nextpnr.fmax"][0] == row["nextpnr.fmax"] and frame["nextpnr.fmax"].dtype == object
    assert result.to_pandas(metrics=metrics)["nextpnr.fmax"][0] == plain["nextpnr.fmax"]


def test_a_point_with_no_record_keeps_none_and_the_value_as_given(
    tmp_path, design, monkeypatch  # noqa: F811
):
    monkeypatch.setenv("XEDA_FAKE_FPGA_TOOL", "yosys")
    monkeypatch.setenv("XEDA_FAKE_FPGA_MODE", "fail")
    given = Q(10, "ps")
    specs = [
        _spec(design, 1, **{"nextpnr.clocks.main_clock.period": given}),
        _spec(design, 2, **{"nextpnr.clocks.main_clock.period": "10000 ps"}),
    ]
    rows = _workspace(tmp_path).sweep(specs).rows(metrics=("nextpnr.fmax",), quantities=True)
    assert [row["nextpnr.fmax"] for row in rows] == [None, None]
    first = rows[0]["settings.nextpnr.clocks.main_clock.period"]
    assert first == given and str(first.units) == "picosecond"
    second = rows[1]["settings.nextpnr.clocks.main_clock.period"]
    assert second == Q(10, "ns") and str(second.units) == "nanosecond"


def test_a_group_of_metrics_is_a_table_with_each_leaf_converted(tmp_path):
    record = _record(
        tmp_path,
        {"t": {"delay": 2.5, "count": 4, "none": 1.0}, "cells": {"a": 1}, "p": {"a": 1.0}},
        {
            "t.delay": ("number", "ns"),
            "t.count": ("integer", None),
            "t.none": ("number", None),
            "cells": ("counts", None),
            "p": ("numbers", "W"),
        },
    )
    table = _metric_quantities(record, "t")
    assert table == {"delay": Q(2.5, "ns"), "count": 4, "none": 1.0}
    assert type(table) is dict and isinstance(table["count"], int)
    assert _metric_quantities(record, "cells") == {"a": 1}
    assert _metric_quantities(record, "p") == {"a": Q(1.0, "W")}
    with pytest.raises(KeyError):
        _metric_quantities(record, "t.absent")


# ------------------------------------------------------------------------------------- the input


def _design(tmp_path, **fields):
    (tmp_path / "top.v").write_text("module top(input clk); endmodule\n")
    return Design(
        name="d",
        root=tmp_path,
        rtl={"sources": ["top.v"], "top": "top", "clocks": [{"port": "clk"}]},
        **fields,
    )


def _clock(period):
    return {"yosys": {"platform": "nangate45", "clocks": {"main_clock": {"period": period}}}}


def _identity(spec, tmp_path, **options):
    plan = Workspace(tmp_path / "run", **options).plan(spec)
    return plan.nodes["yosys"].identity


def _settings(period):
    return Yosys.Settings(
        platform="nangate45", clocks={"main_clock": {"name": "main_clock", "period": period}}
    )


def test_a_pint_quantity_gives_the_validated_settings_and_the_identity_of_the_text(tmp_path):
    as_text = _settings("3 ps")
    as_pint = _settings(Q(3, "ps"))
    assert as_pint == as_text
    assert as_pint.clocks["main_clock"].period == Time("3 ps")
    assert flowrun_hash("yosys", as_pint, "x") == flowrun_hash("yosys", as_text, "x")
    assert flowrun_hash("yosys", _settings(Q(0.003, "ns")), "x") == flowrun_hash(
        "yosys", _settings("0.003 ns"), "x"
    )
    assert _settings(Q(2, "min")).clocks["main_clock"].period == Time("120 s")


def test_a_spec_takes_a_pint_quantity(tmp_path):
    top = _design(tmp_path)
    text = Spec("yosys", top, settings=_clock("10 ns"))
    pint_spec = Spec("yosys", top, settings=_clock(Q(10000, "ps")))
    assert _identity(pint_spec, tmp_path) == _identity(text, tmp_path)
    assert pint_spec != text, "the raw tables differ, the validated settings do not"


def test_local_settings_take_a_pint_quantity(tmp_path):
    top = _design(tmp_path)
    base = _identity(Spec("yosys", top, settings=_clock("10 ns")), tmp_path)
    for layer in ("user", "local"):
        local = LocalSettings(**{layer: _clock(Q(10, "ns"))})
        spec = Spec("yosys", top)
        assert _identity(spec, tmp_path, local_settings=local) == base


def test_a_design_takes_a_pint_quantity_in_flows_and_in_a_target(tmp_path):
    base = _identity(Spec("yosys", _design(tmp_path, flows=_clock("10 ns"))), tmp_path)
    in_flows = _design(tmp_path, flows=_clock(Q(10, "ns")))
    assert _identity(Spec("yosys", in_flows), tmp_path) == base
    in_target = _design(tmp_path, targets={"fast": {"flows": _clock(Q(10, "ns"))}})
    chosen = in_target.with_target("fast")
    assert _identity(Spec("yosys", chosen), tmp_path) == base
    updated = _design(tmp_path).with_updates({"flows": _clock(Q(10, "ns"))})
    assert _identity(Spec("yosys", updated), tmp_path) == base


def test_a_pint_quantity_is_assigned_to_a_settings_model():
    settings = GhdlSim.Settings()
    settings.timeout = Q(2, "min")
    assert settings.timeout == Time("120 s") and str(settings.timeout) == "120 s"
    settings.stop_time = Q(1, "us")
    assert str(settings.stop_time) == "1000 ns"


def test_a_quantity_the_caller_changes_later_does_not_change_the_design(tmp_path):
    period = Q(10, "ns")
    top = _design(tmp_path, flows=_clock(period))
    base = _identity(Spec("yosys", top), tmp_path)
    period.ito("ps")
    assert _identity(Spec("yosys", top), tmp_path) == base


@pytest.mark.parametrize(
    "value, message",
    [
        (Q(1, "kilosecond"), "unknown unit `kilosecond`"),
        (Q(1, "MHz"), "is a frequency, not a time"),
        (Q(1, "m"), "unknown unit"),
        (Q(1, "dimensionless"), "unknown unit"),
        (Q(Fraction(1, 2), "ns"), "`int` or a `float`"),
        (Q(float("nan"), "ns"), "not a finite number"),
        (Q(float("inf"), "ns"), "not a finite number"),
        (Q(1 + 2j, "ns"), "`int` or a `float`"),
        (Q(Decimal("1e-400"), "ps"), "rounds to zero"),
    ],
)
def test_a_pint_quantity_that_is_no_time_is_refused_like_its_text(value, message):
    with pytest.raises(ValidationError, match=message):
        GhdlSim.Settings(timeout=value)


def test_a_bool_magnitude_is_refused():
    value = Q(1, "ns")
    value._magnitude = True  # pint itself refuses to build such a quantity
    with pytest.raises(QuantityError, match="`int` or a `float`"):
        Time(value)


def test_a_unit_that_the_registry_defines_again_is_refused():
    private = pint.UnitRegistry()
    private.define("nanosecond = 1 * second = ns")
    with pytest.raises(ValidationError, match="another meaning"):
        _settings(private.Quantity(1, "ns"))


def test_the_quantity_of_a_private_registry_is_accepted():
    private = pint.UnitRegistry()
    assert _settings(private.Quantity(3, "ps")) == _settings("3 ps")


def test_a_numpy_scalar_is_accepted_and_an_array_is_refused():
    numpy = pytest.importorskip("numpy")
    assert _settings(Q(numpy.float32(2.5), "ns")) == _settings("2.5 ns")
    assert _settings(Q(numpy.int64(3), "ps")) == _settings("3 ps")
    assert _settings(Q(numpy.float64(2.5), "ns")) == _settings("2.5 ns")
    with pytest.raises(ValidationError, match="ndarray"):
        _settings(Q(numpy.array(2.0), "ns"))


def test_a_setting_that_is_no_quantity_refuses_a_pint_quantity():
    with pytest.raises(ValidationError):
        GhdlSim.Settings(analysis_flags=Q(1, "ns"))
    with pytest.raises(ValidationError):
        GhdlSim.Settings(analysis_flags=[Q(1, "ns")])
    with pytest.raises(ValidationError):
        GhdlSim.Settings(work=Q(1, "ns"))


def test_a_pint_quantity_is_written_as_the_text_that_xeda_reads(tmp_path):
    assert json_encodable(Q(10, "ns")) == "10 ns"
    assert json_encodable(Q(2.5, "MHz")) == "2.5 MHz"
    assert json_encodable(Q(1, "kilosecond")) == "1 ks"
    assert pint_quantity_text(Q(3, "minute")) == "3 min"
    document = _design(tmp_path, flows=_clock(Q(10, "ns"))).to_dict()
    assert document["flows"]["yosys"]["clocks"]["main_clock"]["period"] == "10 ns"
    assert json.dumps(document["flows"])
    reloaded = Design(root=tmp_path, **document)
    assert _identity(Spec("yosys", reloaded), tmp_path) == _identity(
        Spec("yosys", _design(tmp_path, flows=_clock("10 ns"))), tmp_path
    )


def test_a_unit_that_the_registry_defines_again_is_not_written_as_text():
    private = pint.UnitRegistry()
    private.define("nanosecond = 1 * second = ns")
    with pytest.raises(ValueError, match="`ns`"):
        json_encodable(private.Quantity(1, "ns"))


def test_a_copy_of_a_design_keeps_the_quantity(tmp_path):
    top = _design(tmp_path, flows=_clock(Q(10, "ns")))
    assert copy.deepcopy(top).to_dict() == top.to_dict()
    assert pickle.loads(pickle.dumps(top)).to_dict() == top.to_dict()


def test_the_spec_document_writes_a_pint_quantity_in_the_unit_of_its_field(tmp_path):
    from xeda._internal.outcomes import spec_document_of

    spec = Spec("ghdl_sim", _design(tmp_path), settings={"ghdl_sim": {"timeout": Q(2, "min")}})
    assert spec_document_of(spec)["settings"] == {"ghdl_sim": {"timeout": "120 s"}}
