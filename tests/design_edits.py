"""Make a changed copy of a design, for the tests: a design never changes in place."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from xeda.design import Design
from xeda.frozen import freeze


def with_updates(
    design: Design,
    *,
    rtl: dict[str, Any] | None = None,
    tb: dict[str, Any] | None = None,
    **fields: Any,
) -> Design:
    """Return `design` with these fields of the design, of `rtl` and of `tb` replaced.

    A value is frozen like the one a design holds (a list becomes a tuple, a table a read-only
    table). Nothing is validated: give the value in its final form."""
    unknown = set(fields) - set(Design.model_fields)
    if unknown:
        raise ValueError(f"not fields of a design: {sorted(unknown)}")
    update: dict[str, Any] = {name: freeze(value) for name, value in fields.items()}
    for name, part in (("rtl", rtl), ("tb", tb)):
        if part is not None:
            changed = getattr(design, name).model_copy(
                update={field: freeze(value) for field, value in part.items()}
            )
            changed.invalidate_cached_properties()
            update[name] = changed
    result = design.model_copy(update=update)
    result.invalidate_cached_properties()
    return result


def design_from_dump(dump: Mapping[str, Any]) -> Design:
    """Return the design that a dump of a design holds (`model_dump()`, or the `design` of a
    `settings.json`): it keeps the recorded root and drops the target and the provenance."""
    document = dict(dump)
    root = document.pop("root")
    for key in ("target", "targets", "updates"):
        document.pop(key, None)
    return Design(root=root, **document)


def design_of(data: Mapping[str, Any], root: Any = None) -> Design:
    """Return the design that a design file with this content would hold, by the same path as a
    loaded file takes: keys that a design does not have are validation errors, not `TypeError`s.
    The root is the directory of the tests unless one is given."""
    return Design._built(Path(__file__).resolve().parent if root is None else root, dict(data))


def design_of_target(target: str, root: Any, **declared: Any) -> Design:
    """Return a design that records `target` as its selected target without a `targets` table
    to select it from (a design of a document made on another machine). Nothing checks the
    name."""
    return Design._built(root, declared, recorded_target=target)
