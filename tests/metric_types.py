"""Check that a written value has the type that its descriptor declares."""

import re
from typing import Any, Callable, Dict, Mapping, Type

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


def descriptor_of(key: str, declared: Mapping[str, MetricSpec]) -> MetricSpec | None:
    """Return the descriptor of the result `key`: the one under its name, or the one whose name
    has a placeholder (`Component Power: <component>`) that the key fills."""
    if key in declared:
        return declared[key]
    for name, spec in declared.items():
        if "<" in name:
            pattern = re.escape(name).replace(r"\<", "<").replace(r"\>", ">")
            if re.fullmatch(re.sub(r"<[^>]*>", ".+", pattern), key):
                return spec
    return None


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
