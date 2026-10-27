"""Make a changed copy of a design, for the tests: a design never changes in place."""

from __future__ import annotations

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
