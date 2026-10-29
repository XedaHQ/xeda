"""The request for a launch: a flow, a design and the settings of the flows.

A `Spec` is a value. It holds no state of a launch, and nothing in it can change.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from typing import TYPE_CHECKING, Any

from .design import Design
from .frozen import freeze, thaw

if TYPE_CHECKING:
    from .flow_runner.bindings import BindingLayer

__all__ = ["Spec", "spec_sections"]

_PWD = re.compile(r"\$(?:PWD(?![A-Za-z0-9_])|\{PWD\})")


def _with_pwd(value: Any, directory: str) -> Any:
    """Return `value` with `$PWD` and `${PWD}` replaced by `directory` in every text it holds."""
    if isinstance(value, str):
        return _PWD.sub(lambda _: directory, value) if "$" in value else value
    if isinstance(value, Mapping):
        return {key: _with_pwd(item, directory) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_with_pwd(item, directory) for item in value]
    return value


@dataclass(frozen=True)
class Spec:
    """What to launch.

    `flow` is a flow name or the text of a chain. `settings` is the contents of a `flows` table:
    the settings of the flows by node, where a node is a flow of the run. `{"nextpnr": {"seed":
    7}}` and `{"nextpnr.seed": 7}` are one setting. The constructor copies the design (a deep
    copy), replaces `$PWD` in the text of a setting with the current directory, and copies the
    settings and freezes them at every depth. So a later change of the design or of the table that
    the caller passed never reaches the spec, and nothing can assign into the spec. The settings
    are checked when the launch is planned. Two specs are equal when their flow, design and
    settings are equal, and a spec can be pickled. A spec is not hashable."""

    flow: str
    design: Design
    _: KW_ONLY
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "design", self.design.model_copy(deep=True))
        object.__setattr__(self, "settings", freeze(_with_pwd(self.settings, os.getcwd())))


def spec_sections(spec: Spec) -> tuple[dict[str, dict[str, Any]], BindingLayer]:
    """Return the settings of each node that `spec` gives, and the bindings among them.

    Every top-level key of `spec.settings` names a node of the run of `spec.flow` on
    `spec.design`. Dotted keys and nested tables mean the same. A key that names no node, a node
    that is not in the run, a value that is no table and a leaf given twice are errors. The
    sections hold the settings of each node, without the input bindings. The layer holds those."""
    from .flow_runner.bindings import split_bindings
    from .flow_runner.chains import parse_request
    from .flow_runner.node_settings import split_node_sections
    from .flow_runner.settings_layers import (
        COMMAND_LINE_ORIGIN,
        check_run_flows,
        registered_flow,
        transitive_dependencies,
    )

    request = parse_request(spec.flow)
    requested = request.requested
    flows = tuple(
        dict.fromkeys(
            (
                *(element.flow_class for element in request.elements),
                *(flow for link in request.links for flow in link.through),
            )
        )
    )
    sections = split_node_sections(thaw(spec.settings), requested.name, registered_flow, flows)
    nodes = {flow.name for flow in flows} | set(transitive_dependencies(requested, spec.design))
    check_run_flows(sections, requested, spec.design, run_flows=nodes)
    return split_bindings(sections, location=COMMAND_LINE_ORIGIN, kind="cli")
