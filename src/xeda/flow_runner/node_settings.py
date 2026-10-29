"""Settings that are addressed by node.

The command line (`-s`) and `Spec.settings` give the contents of a `flows` table: every top-level
key names a node, and a node is a flow of the run. `nextpnr.seed=7` and `{"nextpnr": {"seed": 7}}`
set the same setting. A key that names no node is refused, with the address to write instead. So
appending a flow to a chain moves no setting.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..flow import Flow, FlowSettingsError
from ..flow.io import declared_inputs
from ..utils import settings_to_dict
from .settings_layers import (
    COMMAND_LINE_ORIGIN,
    _given_leaves,
    _has_clock_table,
    _repeated_leaves,
    check_flow_name,
    merge_flow_sections,
    registered_flow,
)

__all__ = ["split_node_sections"]

#: One finding of a refusal: `(location, message, context, kind)`. The context of a finding that
#: has suggestions holds them, separated by commas.
Finding = tuple[str | None, str | None, str | None, str | None]


def _owners(key: str, flows: Sequence[type[Flow]], name: str | None = None) -> list[str]:
    """Return the names of the `flows` that have the setting `key`. For `inputs`, they are the
    flows that declare the input `name`."""
    if key == "inputs":
        return [flow.name for flow in flows if name in declared_inputs(flow)]
    return [
        flow.name
        for flow in flows
        if key in flow.Settings.model_fields or (key == "clock" and _has_clock_table(flow.Settings))
    ]


def _unscoped(
    key: str, written: str, flows: Sequence[type[Flow]], requested: str, name: str | None = None
) -> Finding:
    """Return the finding for the key `key`, written (up to the first unknown part) as `written`,
    that names no node. `name` is the part after `inputs`."""
    owners = _owners(key, flows, name) or [requested]
    suggested = [f"{owner}.{written}" for owner in owners]
    shown = " or ".join(f"`{address}`" for address in suggested)
    return (
        written,
        f"a key starts with the flow that it addresses: write {shown}",
        ",".join(suggested),
        "unscoped_setting",
    )


def _first_key(value: Any, default: str) -> str:
    return next(iter(value), default) if isinstance(value, Mapping) else default


def _problems(
    table: Mapping[str, Any], flows: Sequence[type[Flow]], requested: str
) -> list[Finding]:
    """Return what is wrong with the top-level keys of `table`, one finding for each key."""
    problems: list[Finding] = []
    nodes = sorted({flow.name for flow in flows})
    for key, value in table.items():
        if key == "flows":
            # The old spelling of a node: `flows.<node>.<key>`.
            for node, section in value.items() if isinstance(value, Mapping) else ():
                written = f"flows.{node}.{_first_key(section, '<key>')}"
                address = written.removeprefix("flows.")
                problems.append(
                    (
                        written,
                        f"a key starts with its flow, with no `flows.` before it: write `{address}`",
                        address,
                        "unscoped_setting",
                    )
                )
            if not isinstance(value, Mapping) or not value:
                problems.append(
                    (
                        key,
                        "a key starts with the flow that it addresses: write `<flow>.<key>`",
                        None,
                        "unscoped_setting",
                    )
                )
        elif registered_flow(key) is not None:
            if not isinstance(value, (Mapping, list, tuple)):
                problems.append(
                    (
                        key,
                        f"`{key}` is a flow: give its settings as `{key}.<key>=<value>`, "
                        f"not the value {value!r:.40}",
                        None,
                        "invalid_value",
                    )
                )
        elif key == "inputs" and isinstance(value, Mapping):
            name = _first_key(value, "<input>")
            problems.append(_unscoped(key, f"inputs.{name}", flows, requested, name))
        elif not isinstance(value, Mapping):
            problems.append(_unscoped(key, key, flows, requested))
        elif _owners(key, flows):
            # A setting that is a table (`clocks`, `env`), written without its node.
            written = f"{key}.{_first_key(value, '<key>')}"
            problems.append(_unscoped(key, written, flows, requested))
        else:
            close = difflib.get_close_matches(key, nodes, n=3)
            hint = "".join(f"; did you mean `{name}`?" for name in close)
            problems.append(
                (
                    key,
                    f"names no node of this run ({', '.join(nodes)}){hint}",
                    ",".join(close),
                    "unknown_node",
                )
            )
    return problems


def split_node_sections(
    layer: Mapping[str, Any] | Sequence[str],
    requested: str,
    flow_class_for: Callable[[str], Any | None] | None = None,
    flows: Sequence[type[Flow]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the settings of each node that `layer` gives, as the sections of a `flows` table.

    `layer` is the `-s` items or a table, where dotted keys and nested tables mean the same.
    Every top-level key must name a node. A key that names none is `unscoped_setting` when it is a
    setting (the error holds the node.key addresses to write), and `unknown_node` otherwise.
    `flows` are the flows of the request, which an unscoped key may belong to (the requested
    flow and the flows below it, by default). A table given both dotted and nested is an error
    (`conflicting_keys`). Two items that set one setting to different values are an error that
    names both. An item that repeats another is that item. Whether a node belongs to the run is for
    the caller to check (`check_run_flows`)."""
    requested_class = (flow_class_for or registered_flow)(requested)
    if flows is None:
        from .chains import default_graph

        flows = default_graph(requested_class).flows() if requested_class is not None else ()
    table = settings_to_dict(layer)  # type: ignore[arg-type]
    for key in table:
        if key != "flows":
            check_flow_name(key)  # a removed flow, or another spelling of a flow
    problems = _problems(table, flows, requested)
    if not problems:
        problems = _repeated_leaves(_given_leaves(layer, requested, flow_class_for, scoped=True))
    if problems:
        raise FlowSettingsError(problems, "flows")
    return merge_flow_sections(table, flow_class_for=flow_class_for, location=COMMAND_LINE_ORIGIN)
