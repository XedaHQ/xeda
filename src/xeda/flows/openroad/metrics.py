"""Read the metrics that OpenROAD writes (`openroad -metrics <file>`).

OpenROAD writes one JSON object. Each key is `<stage>__<metric>`, and each value is a number or a
text. A command that runs again writes its key again, and the last value is the one that counts
(`json.load` keeps it). The readers here refuse a value that is not what the flow needs, and
every refusal names the file, the key and the value: a verdict is never judged on a value that
is not a finite number.

Times in the file are in the time unit of the Liberty files. The results are in nanoseconds.
"""

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from ...units import convert_unit

__all__ = ["MetricsError", "count", "final_results", "load_metrics", "number", "stage_results"]


class MetricsError(ValueError):
    """A metrics file, or a value in it, is not what the flow needs."""


def load_metrics(path: str | Path) -> dict[str, Any]:
    """Return the metrics of the JSON object in the file `path`.

    A key that appears twice keeps its last value. Raise `MetricsError` if the file holds no JSON
    object."""
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as stream:
            loaded = json.load(stream)
    except json.JSONDecodeError as error:
        raise MetricsError(f"{path} is not JSON: {error}") from None
    if not isinstance(loaded, dict):
        raise MetricsError(f"{path} holds {type(loaded).__name__}, not a JSON object")
    return loaded


def _refusal(file: str | Path, key: str, value: Any, wanted: str) -> MetricsError:
    return MetricsError(f"{file}: the metric {key!r} must be {wanted}, but it is {value!r}")


def number(metrics: Mapping[str, Any], key: str, *, file: str | Path) -> float:
    """Return the metric `key` as a float. It must be a JSON number: not a bool, not `null`, not
    text, and finite (the JSON reader of Python takes `NaN` and `Infinity`; they are refused)."""
    if key not in metrics:
        raise MetricsError(f"{file}: the metric {key!r} is missing")
    value = metrics[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _refusal(file, key, value, "a finite number")
    return float(value)


def count(metrics: Mapping[str, Any], key: str, *, file: str | Path) -> int:
    """Return the metric `key` as an int. It must be a finite number with a whole value that is
    not negative, and no bool. A float such as `3.0` is a count."""
    if key not in metrics:
        raise MetricsError(f"{file}: the metric {key!r} is missing")
    value = metrics[key]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value != int(value)
        or value < 0
    ):
        raise _refusal(file, key, value, "a whole number that is not negative")
    return int(value)


def to_nanoseconds(value: float, time_unit: str, *, file: str | Path, key: str) -> float:
    """Convert `value`, in `time_unit` (the Liberty unit, such as `1ns`), to nanoseconds."""
    try:
        return convert_unit(value, "ns", from_unit=time_unit)
    except ValueError as error:
        raise MetricsError(
            f"{file}: cannot read the metric {key!r} in the time unit {time_unit!r}: {error}"
        ) from None


#: Result name, metric name (after the stage prefix) and the kind of the value.
#: `time` is converted to ns; `hertz` to MHz; the others are read as they are.
STAGE_TABLE: tuple[tuple[str, str, str], ...] = (
    ("worst_slack", "timing__setup__ws", "time"),
    ("tns", "timing__setup__tns", "time"),
    ("whs", "timing__hold__ws", "time"),
    ("hold_tns", "timing__hold__tns", "time"),
    ("fmax", "timing__fmax", "hertz"),
    ("area", "design__instance__area__stdcell", "plain"),
    ("utilization", "design__instance__utilization", "plain"),
    ("power", "power__total", "plain"),
    ("clock_skew_setup", "clock__skew__setup", "time"),
    ("clock_skew_hold", "clock__skew__hold", "time"),
)


def stage_results(
    metrics: Mapping[str, Any], stage: str, *, time_unit: str, file: str | Path
) -> dict[str, float]:
    """Return the results of one stage from its metrics, by result name.

    `worst_slack` is the worst setup slack, and `wns` is `min(0, worst_slack)`, since OpenROAD
    writes no WNS. `fmax` is in MHz and every time is in ns (`time_unit` is the unit of the
    Liberty files, such as `1ns`). `area` is the area of the standard cells in um^2, and `power`
    is in watts. A result whose metric is absent is left out. A metric that is present and not a
    finite number raises `MetricsError`."""
    results: dict[str, float] = {}
    for name, metric, kind in STAGE_TABLE:
        key = f"{stage}__{metric}"
        if key not in metrics:
            continue
        value = number(metrics, key, file=file)
        if kind == "time":
            value = to_nanoseconds(value, time_unit, file=file, key=key)
        elif kind == "hertz":
            value /= 1e6
        results[name] = value
        if name == "worst_slack":
            results["wns"] = min(0.0, value)
    return results


#: A reader of one metric: `number` or `count`.
Reader = Callable[..., Any]

#: The results of the last stage (`finish`) that `stage_results` does not cover: the result, the
#: metric after the stage prefix, and the reader of its value.
FINISH_TABLE: tuple[tuple[str, str, Reader], ...] = (
    ("setup_violations", "timing__drv__setup_violation_count", count),
    ("hold_violations", "timing__drv__hold_violation_count", count),
    ("max_slew_violations", "timing__drv__max_slew", count),
    ("max_cap_violations", "timing__drv__max_cap", count),
    ("max_fanout_violations", "timing__drv__max_fanout", count),
    ("core_area", "design__core__area", number),
    ("die_area", "design__die__area", number),
    ("power_internal", "power__internal__total", number),
    ("power_switching", "power__switching__total", number),
    ("power_leakage", "power__leakage__total", number),
    ("instances", "design__instance__count__stdcell", count),
    ("fill_cells", "design__instance__count__class:fill_cell", count),
    ("tap_cells", "design__instance__count__class:tap_cell", count),
    ("warnings", "flow__warnings__count", count),
    ("errors", "flow__errors__count", count),
)

#: The results of the detailed route that a verdict reads: the result, the metric after the stage
#: prefix, and the reader of its value. Each must be there and valid.
ROUTE_VERDICTS: tuple[tuple[str, str, Reader], ...] = (
    ("drc_errors", "route__drc_errors", count),
    ("antenna_violating_nets", "antenna__violating__nets", count),
)
#: The other results of the detailed route, in the same form.
ROUTE_TABLE: tuple[tuple[str, str, Reader], ...] = (
    ("antenna_violating_pins", "antenna__violating__pins", count),
    ("antenna_diodes", "antenna_diodes_count", count),
    ("wirelength", "route__wirelength", number),
    ("vias", "route__vias", count),
)


def final_results(
    route: Mapping[str, Any],
    final: Mapping[str, Any],
    *,
    time_unit: str,
    route_file: str | Path,
    final_file: str | Path,
) -> dict[str, Any]:
    """Return the results of the last flow from the metrics of its two processes, by result name.

    `route` holds the metrics of the detailed route (`detailedroute__*`, from `route_file`) and
    `final` those of the final report (`finish__*`, from `final_file`). The times are in
    nanoseconds (`time_unit` is the unit of the Liberty files), `fmax` is in MHz, the areas are
    in um^2, `power` is in watts, `wirelength` is in um, and the rest are counts and ratios.

    The four values that the verdicts read must be there and valid: the worst setup slack and
    hold slack, the design rule violations and the nets with an antenna violation. A missing or
    invalid one raises `MetricsError`, whatever the checks of the flow are. Any other result is
    left out when its metric is absent, and a metric that is present and invalid raises
    `MetricsError`."""
    for metric in ("timing__setup__ws", "timing__hold__ws"):
        number(final, f"finish__{metric}", file=final_file)
    results: dict[str, Any] = stage_results(final, "finish", time_unit=time_unit, file=final_file)
    for result, metric, reader in ROUTE_VERDICTS:
        results[result] = reader(route, f"detailedroute__{metric}", file=route_file)
    for table, metrics, stage, file in (
        (FINISH_TABLE, final, "finish", final_file),
        (ROUTE_TABLE, route, "detailedroute", route_file),
    ):
        for result, metric, reader in table:
            key = f"{stage}__{metric}"
            if key in metrics:
                results[result] = reader(metrics, key, file=file)
    return results
