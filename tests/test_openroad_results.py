"""The metrics that OpenROAD writes become results through typed readers.

`openroad -metrics <file>` writes one JSON object with a key for each metric. The readers refuse
what is not a finite number, so a verdict is never judged on a value that is not one, and every
refusal names the file, the key and the value.
"""

import json
import math
from pathlib import Path

import pytest

from xeda.flows.openroad.metrics import (
    MetricsError,
    count,
    load_metrics,
    number,
    stage_results,
)

from .openroad_utils import probe_flows

METRICS = Path(__file__).parent / "resources" / "openroad" / "metrics"


def recorded(name: str) -> dict:
    return load_metrics(METRICS / f"{name}.json")


# ------------------------------------------------------------------------------ the file


def test_a_duplicate_key_keeps_the_last_value(tmp_path):
    """OpenROAD writes a key again when a command runs again, and the last value is the result
    (`json.load` keeps it)."""
    path = tmp_path / "metrics.json"
    path.write_text(
        '{"finish__design__instance__area": 1082.89, "other": 1, '
        '"finish__design__instance__area": 907.592}'
    )
    assert load_metrics(path)["finish__design__instance__area"] == 907.592


def test_a_file_that_is_not_a_json_object_is_refused(tmp_path):
    for text in ("[1, 2]", "3", "not json", ""):
        path = tmp_path / "metrics.json"
        path.write_text(text)
        with pytest.raises(MetricsError, match="metrics.json"):
            load_metrics(path)


# ------------------------------------------------------------------------------ number and count


@pytest.mark.parametrize(
    "text, shown",
    [
        ("null", "None"),
        ('"1.5"', "'1.5'"),
        ("true", "True"),
        ("false", "False"),
        ("NaN", "nan"),
        ("Infinity", "inf"),
        ("-Infinity", "-inf"),
        ("[1]", "[1]"),
        ("{}", "{}"),
    ],
)
def test_number_refuses_what_is_not_a_finite_number(tmp_path, text, shown):
    path = tmp_path / "m.json"
    path.write_text(f'{{"a__timing__setup__ws": {text}}}')
    with pytest.raises(MetricsError) as error:
        number(load_metrics(path), "a__timing__setup__ws", file=path)
    message = str(error.value)
    assert message.startswith(f"{path}:")
    assert "'a__timing__setup__ws'" in message and message.endswith(f"it is {shown}")


def test_number_reads_integers_and_floats():
    assert number({"a": 3}, "a", file=Path("m.json")) == 3.0
    assert number({"a": -0.158984}, "a", file=Path("m.json")) == -0.158984
    assert isinstance(number({"a": 3}, "a", file=Path("m.json")), float)


def test_number_refuses_a_missing_key(tmp_path):
    with pytest.raises(MetricsError, match="the metric 'a' is missing"):
        number({}, "a", file=tmp_path / "m.json")


@pytest.mark.parametrize("value", [1.5, -1, True, False, None, "3", math.inf, math.nan, -2.0])
def test_count_refuses_what_is_not_a_non_negative_whole_number(value):
    with pytest.raises(MetricsError) as error:
        count({"c": value}, "c", file=Path("m.json"))
    message = str(error.value)
    assert "m.json" in message and "'c'" in message and f"it is {value!r}" in message


@pytest.mark.parametrize("value, expected", [(0, 0), (7, 7), (3.0, 3), (1e3, 1000)])
def test_count_reads_a_whole_number(value, expected):
    result = count({"c": value}, "c", file=Path("m.json"))
    assert result == expected and isinstance(result, int)


# ------------------------------------------------------------------------------ the stage table


def test_the_detailed_placement_stage_of_the_recorded_run():
    metrics = recorded("openroad_place")
    results = stage_results(
        metrics, "detailedplace", time_unit="1ns", file=METRICS / "openroad_place.json"
    )
    assert results["worst_slack"] == pytest.approx(-0.154591)
    assert results["wns"] == pytest.approx(-0.154591)
    assert results["tns"] == pytest.approx(-6.68972)
    assert results["whs"] == pytest.approx(0.0846505)
    assert results["hold_tns"] == 0
    assert results["Fmax"] == pytest.approx(2471.63)
    assert results["area"] == pytest.approx(807.044)
    assert results["utilization"] == pytest.approx(0.745271)
    assert results["power"] == pytest.approx(0.00524598)
    assert "clock_skew_setup" not in results  # the stage does not report the skew


def test_the_clock_tree_stage_reports_the_skew():
    metrics = recorded("openroad_cts")
    results = stage_results(metrics, "cts", time_unit="1ns", file=METRICS / "openroad_cts.json")
    assert results["clock_skew_setup"] == pytest.approx(0.00135155)
    assert results["clock_skew_hold"] == pytest.approx(0.00135155)


def test_the_wns_is_never_positive():
    metrics = {"s__timing__setup__ws": 0.075}
    results = stage_results(metrics, "s", time_unit="1ns", file=Path("m.json"))
    assert results["worst_slack"] == 0.075 and results["wns"] == 0


def test_times_convert_from_the_liberty_unit_to_nanoseconds():
    metrics = {
        "s__timing__setup__ws": -150.0,
        "s__timing__setup__tns": -2000.0,
        "s__timing__hold__ws": 80.0,
        "s__timing__hold__tns": 0,
        "s__clock__skew__setup": 12.0,
    }
    results = stage_results(metrics, "s", time_unit="1ps", file=Path("m.json"))
    assert results["worst_slack"] == pytest.approx(-0.15)
    assert results["wns"] == pytest.approx(-0.15)
    assert results["tns"] == pytest.approx(-2.0)
    assert results["whs"] == pytest.approx(0.08)
    assert results["clock_skew_setup"] == pytest.approx(0.012)


def test_a_result_whose_metric_is_absent_is_left_out():
    results = stage_results({"s__power__total": 1.0}, "s", time_unit="1ns", file=Path("m.json"))
    assert results == {"power": 1.0}


def test_a_result_whose_metric_is_invalid_is_an_error_naming_the_key():
    for bad in (None, "fast", True, math.nan):
        with pytest.raises(MetricsError, match="s__timing__fmax"):
            stage_results({"s__timing__fmax": bad}, "s", time_unit="1ns", file=Path("m.json"))


def test_a_time_unit_that_is_no_time_is_refused():
    with pytest.raises(MetricsError, match="1um"):
        stage_results({"s__timing__setup__ws": 1.0}, "s", time_unit="1um", file=Path("m.json"))


# ------------------------------------------------------------------------------ the flow reads it


def test_read_metrics_ignores_a_file_this_run_did_not_write(tmp_path):
    """A metrics file that a previous run left is no result of this run."""
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        (flow.run_path / "metrics.json").write_text(json.dumps({"x": 1}))
        flow.start_run()  # what the launcher does right before `run()`
        assert flow.read_metrics("metrics.json") is None
        (flow.run_path / "metrics_fresh.json").write_text(json.dumps({"x": 2}))
        assert flow.read_metrics("metrics_fresh.json") == {"x": 2}
        (flow.run_path / "metrics.json").write_text(json.dumps({"x": 3}))  # written again
        assert flow.read_metrics("metrics.json") == {"x": 3}


def test_read_metrics_is_none_for_a_file_that_is_missing_or_no_object(tmp_path):
    with probe_flows() as probes:
        flow = probes.flow(tmp_path)
        assert flow.read_metrics("absent.json") is None
        for name, text in (("list.json", "[1]"), ("broken.json", "{"), ("number.json", "3")):
            (flow.run_path / name).write_text(text)
            assert flow.read_metrics(name) is None, name
