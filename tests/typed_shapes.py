"""Check that a value has exactly the keys and the value types of a `TypedDict` class.

`xeda.PlanOptions`, `xeda.introspect.FlowInfo` and the other classes of the public API describe
mappings that Xeda fills. A type checker cannot see through the code that fills them (`freeze`,
`json_safe`, a document read), so a test checks the values that a real call returns.
"""

from __future__ import annotations

import warnings
from typing import Any

import typing_extensions
from pydantic import ConfigDict, TypeAdapter


def check_shape(shape: type, value: Any) -> None:
    """Raise `ValidationError` unless `value` has every required key of `shape`, no key that
    `shape` lacks and, for each key, a value of the type that `shape` gives it.

    A table of a plan or of a record is a `FrozenDict`: it is checked as the plain `dict` that it
    stands for, a list as the tuple that `freeze` makes of it is checked as a tuple."""
    assert typing_extensions.is_typeddict(shape), shape
    # A `TypedDict` takes no `config` of its own here, so a list holds it.
    with warnings.catch_warnings():
        # Pydantic cannot enforce `ReadOnly`, and the check changes no value.
        warnings.filterwarnings("ignore", message=".*using the `ReadOnly` qualifier")
        adapter = TypeAdapter(list[shape], config=ConfigDict(extra="forbid", strict=True))  # type: ignore[valid-type]
        adapter.validate_python([_plain(value)], strict=True)


def _plain(value: Any) -> Any:
    """Return `value` with each read-only table as a `dict`. A tuple stays a tuple."""
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_plain(item) for item in value)
    return value
