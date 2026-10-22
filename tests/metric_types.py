"""Check that a written value has the type that its descriptor declares."""

from functools import partial
from typing import Any, Callable, Dict, Iterable, Mapping, Type, get_args

from xeda.flow import Flow, MetricSpec
from xeda.flow.metrics import MetricType, value_fits

#: The test of a value for each metric type: `xeda.flow.metrics.value_fits`, the rule that the
#: record codec applies.
VALUE_TESTS: Dict[str, Callable[[Any], bool]] = {
    metric_type: partial(value_fits, metric_type) for metric_type in get_args(MetricType)
}


#: The keys that every run has, whatever the flow describes.
RUN_KEYS = frozenset({"success", "tools", "artifacts", "runtime"})


def descriptor_of(key: str, declared: Mapping[str, MetricSpec]) -> MetricSpec | None:
    """Return the descriptor of the result `key`, or `None` when the flow does not describe it."""
    return declared.get(key)


def paths_inside_paths(paths: Iterable[str]) -> list[tuple[str, str]]:
    """Return each pair of `paths` of which the first starts the second at a segment boundary
    (`bram` and `bram.ramb18`). Such paths cannot nest into one object."""
    found = set(paths)
    return sorted(
        (outer, inner) for outer in found for inner in found if inner.startswith(outer + ".")
    )


def undescribed_keys(flow_class: Type[Flow], results: Mapping[str, Any]) -> list[str]:
    """Return the keys of `results` that the flow does not describe, leaving out an internal key
    (it starts with `_`) and the keys of the run itself (`RUN_KEYS`)."""
    declared = flow_class.results_description
    return [
        key
        for key in results
        if not key.startswith("_") and key not in declared and key not in RUN_KEYS
    ]


def mismatches(flow_class: Type[Flow], results: Mapping[str, Any]) -> list[str]:
    """Return a line for each described key of `results` whose value is not of the declared type."""
    declared = flow_class.results_description
    found = []
    for key, value in results.items():
        spec = descriptor_of(key, declared)
        if spec is not None and not VALUE_TESTS[spec.type](value):
            found.append(f"{key}: declared {spec.type}, written {value!r}")
    return found


def described_keys(flow_class: Type[Flow], results: Mapping[str, Any]) -> list[str]:
    """Return the keys of `results` that the flow describes."""
    return [key for key in results if descriptor_of(key, flow_class.results_description)]
