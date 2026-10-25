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
    final_results,
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
        '{"finish__design__instance__area": 871.416, "other": 1, '
        '"finish__design__instance__area": 773.528}'
    )
    assert load_metrics(path)["finish__design__instance__area"] == 773.528


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
    assert number({"a": -0.244411}, "a", file=Path("m.json")) == -0.244411
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
    assert results["worst_slack"] == pytest.approx(-0.1835)
    assert results["wns"] == pytest.approx(-0.1835)
    assert results["tns"] == pytest.approx(-7.7916)
    assert results["whs"] == pytest.approx(0.0846772)
    assert results["hold_tns"] == 0
    assert results["fmax"] == pytest.approx(2306.81)
    assert results["area"] == pytest.approx(657.02)
    assert results["utilization"] == pytest.approx(0.753968)
    assert results["power"] == pytest.approx(0.00388374)
    assert "clock_skew_setup" not in results  # the stage does not report the skew


def test_the_clock_tree_stage_reports_the_skew():
    metrics = recorded("openroad_cts")
    results = stage_results(metrics, "cts", time_unit="1ns", file=METRICS / "openroad_cts.json")
    assert results["clock_skew_setup"] == pytest.approx(0.00116677)
    assert results["clock_skew_hold"] == pytest.approx(0.00116677)


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


# ------------------------------------------------------------------------------ the last flow


def last_flow() -> dict:
    """The results of the recorded run of the last flow."""
    return final_results(
        recorded("openroad_route"),
        recorded("openroad_final"),
        time_unit="1ns",
        route_file=METRICS / "openroad_route.json",
        final_file=METRICS / "openroad_final.json",
    )


def test_the_last_flow_takes_the_slack_from_the_final_file_and_the_counts_from_the_route_file():
    results = last_flow()
    assert results["worst_slack"] == pytest.approx(-0.244411)
    assert results["whs"] == pytest.approx(0.0159159)
    assert results["drc_errors"] == 0 and results["antenna_violating_nets"] == 0
    assert results["wirelength"] == 3599 and results["vias"] == 3073
    assert results["area"] == pytest.approx(773.528)
    assert results["fmax"] == pytest.approx(2022.61)


def test_a_verdict_key_that_is_missing_is_named_with_its_file():
    route, final = recorded("openroad_route"), recorded("openroad_final")
    del route["detailedroute__route__drc_errors"]
    with pytest.raises(
        MetricsError, match=r"route\.json.*'detailedroute__route__drc_errors'.*missing"
    ):
        final_results(
            route, final, time_unit="1ns", route_file=Path("route.json"), final_file=Path("f.json")
        )
    route, final = recorded("openroad_route"), recorded("openroad_final")
    del final["finish__timing__hold__ws"]
    with pytest.raises(MetricsError, match=r"f\.json.*'finish__timing__hold__ws'.*missing"):
        final_results(
            route, final, time_unit="1ns", route_file=Path("route.json"), final_file=Path("f.json")
        )


def test_the_diodes_of_a_route_that_did_not_repair_are_left_out():
    """The diode of nangate45 is not usable, so the recorded route skipped the repair."""
    route, final = recorded("openroad_route"), recorded("openroad_final")
    assert "detailedroute__antenna_diodes_count" not in route
    results = final_results(
        route, final, time_unit="1ns", route_file=Path("r.json"), final_file=Path("f.json")
    )
    assert "antenna_diodes" not in results and results["antenna_violating_pins"] == 0
    route["detailedroute__antenna_diodes_count"] = 3
    results = final_results(
        route, final, time_unit="1ns", route_file=Path("r.json"), final_file=Path("f.json")
    )
    assert results["antenna_diodes"] == 3


def test_a_key_of_the_wrong_file_is_not_the_value():
    route, final = recorded("openroad_route"), recorded("openroad_final")
    final["detailedroute__route__drc_errors"] = 7
    route["finish__timing__setup__ws"] = -7.0
    results = final_results(
        route, final, time_unit="1ns", route_file=Path("r.json"), final_file=Path("f.json")
    )
    assert results["drc_errors"] == 0 and results["worst_slack"] == pytest.approx(-0.244411)
