"""The request for a launch: a flow, a design and the settings of the flows.

A `Spec` is a value. It holds no state of a launch, and nothing in it can change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from typing import Any

from .design import Design
from .frozen import freeze

__all__ = ["Spec"]


@dataclass(frozen=True)
class Spec:
    """What to launch.

    `flow` is a flow name or the text of a chain. `settings` is the contents of a `flows` table:
    the settings of the flows by flow name. The constructor copies it and freezes it at every
    depth, so a later change of the table that the caller passed never reaches the spec, and
    nothing can assign into the spec."""

    flow: str
    design: Design
    _: KW_ONLY
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", freeze(self.settings))
