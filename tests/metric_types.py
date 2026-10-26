"""Check that a written value has the type that its descriptor declares."""

from typing import Any, Callable, Dict, Iterable, Mapping, Type

from xeda.flow import Flow, MetricSpec


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _table_of(is_value: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, dict) and all(
        isinstance(key, str) and is_value(item) for key, item in value.items()
    )


#: The test of a value for each metric type.
VALUE_TESTS: Dict[str, Callable[[Any], bool]] = {
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "text": lambda value: isinstance(value, str),
    "text_list": lambda value: isinstance(value, list) and all(isinstance(v, str) for v in value),
    "counts": _table_of(_is_integer),
    "numbers": _table_of(_is_number),
    "object": lambda value: isinstance(value, dict),
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
