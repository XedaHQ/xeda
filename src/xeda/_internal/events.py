"""The events of a launch, as `on_event` receives them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .outcomes import Disposition, Outcome

__all__ = ["Event"]


@dataclass(frozen=True)
class Event:
    """One step of a launch.

    `kind` is `launch_started`, `node_started`, `node_finished` or `launch_finished`. A launch has
    one `launch_started`, then a `node_started` and a `node_finished` for each node that it
    started (a reused or shared node has both), then one `launch_finished`. `node` names the node
    of a node event. `outcome` and `disposition` are those of the node in the `Result`
    (`node_finished`), and `outcome` is the aggregate of the launch (`launch_finished`). A field
    that does not apply is `None`. `time` is aware and in UTC, as `RunRecord.started`."""

    kind: str
    launch_id: str
    node: str | None
    outcome: "Outcome | None"
    disposition: "Disposition | None"
    time: datetime
