"""The one test of a `schema_version` that a reader of a stored document makes."""

from typing import Any


def is_version(value: Any, supported: int) -> bool:
    """Return whether `value` is the integer `supported`. A Boolean is not: `True == 1` in Python,
    and the schema of a document (`{"const": 1}`) does not accept `true`."""
    return type(value) is int and value == supported
