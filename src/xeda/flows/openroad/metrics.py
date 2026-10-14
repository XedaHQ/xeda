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
from typing import Any, Mapping

from ...units import convert_unit

__all__ = ["MetricsError", "count", "load_metrics", "number", "stage_results"]


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
    ("Fmax", "timing__fmax", "hertz"),
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
    writes no WNS. `Fmax` is in MHz and every time is in ns (`time_unit` is the unit of the
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
