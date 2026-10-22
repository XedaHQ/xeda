"""Pure graph resolution, with agreement of shared settings that keeps their origins.

An input is supplied, in this order of precedence, by an explicit binding (a chain link,
or `flows.<consumer>.inputs.<input>` in a file, on the command line or through the API), by
the typed sources of the design, or by its declared default producer. Nodes are told apart by
`NodeKey` and each is resolved once, so every demand on the outputs of a producer is known
before its settings are frozen and hashed.

Composition precedes agreement: a node keeps the origin-first precedence of `settings_layers`,
while explicit shared leaves at different nodes are independent evidence. Only leaves from the
command line and the API override the whole connected component. A plan needs no flow instance
and no execution state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from ..board import WithFpgaBoardSettings
from ..dataclass import BaseModel, expand_forms, nested_model
from ..design import DESIGN_PARTS, LANGUAGE_TYPES, Design
from ..flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash, is_unset
from ..flow.flow import NoReadableSource, written_path_problems
from ..flow.derived import planned_design
from ..flow.fpga import FPGA
from ..flow.io import (
    declared_inputs,
    declared_outputs,
    kinds_text,
    output_enabled,
    selected_types,
)
from ..flow.synth import PhysicalClock
from ..units import Quantity
from .bindings import (
    BindingLayer,
    InputBinding,
    NodeKey,
    check_chain_collisions,
    input_origins,
    node_bindings,
    node_identity,
)
from .chains import (
    FlowRequest,
    converter_edges,
    default_graph,
    design_edge_refusal,
    fitting_outputs,
    request_text,
)
from .related_inputs import Node, check_related_inputs
from .settings_layers import (
    API_ORIGIN,
    COMMAND_LINE_ORIGIN,
    SUPPLIED_SECTIONS_ORIGIN,
    carry_diagnostics,
    check_run_flows,
    compose_flow_settings,
    merge_flow_sections,
    merge_layers,
    registered_flow,
    settings_in_context,
    suggest_dependency_node,
    transitive_dependencies,
)
from .trace import as_recorded

SHARED_SETTINGS = (
    "fpga",
    "board",
    "custom_boards_file",
    "clocks",
    "prjxray_db",
    "platform",
    "corner",
    "dont_use_cells",
    "platform_file",
    "liberty",
    "pdk_provider",
    "pdk_version",
    "pdk_root",
    "hierarchical",
    "wire_rc",
)
#: Shared settings that name a file or a directory. They are compared by the resolved path, a
#: relative one against the design root.
SHARED_PATH_SETTINGS = ("prjxray_db", "platform_file", "pdk_root")
#: Shared settings that name an ordered list of files. They are compared, and propagated, as the
#: list of the resolved paths, so the spelling of the node that wins does not change the identity
#: of the others.
SHARED_PATH_LIST_SETTINGS = ("liberty",)
#: The settings that pin the PDK of a platform. The resolver fills them in when the user gives
#: none (`_pin_pdks`).
PIN_SETTINGS = ("pdk_provider", "pdk_version")
log = logging.getLogger(__name__)


BINDING_ORIGINS = {
    "chain": "chain",
    "file": "saved binding",
    "cli": "`-s` or `flow_settings`",
    "api": "API",
}


@dataclass(frozen=True)
class ResolvedReference:
    """One producer node of the plan and the output key it supplies."""

    node: str
    output: str


@dataclass(frozen=True)
class ResolvedInput:
    """The ordered sources, the ordered producer references, or the permitted absence of an input.

    `producer` and `output` are those of the first reference (the only one of a scalar input).
    `binding_origin` is None for design sources and default producers. Otherwise it says where
    the explicit binding came from (`BINDING_ORIGINS`). With `binding_location` and
    `overridden`, it explains the edge, and it is no part of the identity of a run. So is
    `reason`, the explanation of an edge that the resolver chose on its own: the conversion of
    the design (`discover`). An explicit binding has none.
    """

    name: str
    origin: Literal["source", "producer", "none"]
    producer: str | None = None
    output: str | None = None
    sources: tuple[Path, ...] = ()
    references: tuple[ResolvedReference, ...] = ()
    binding_origin: str | None = None
    binding_location: str | None = None
    overridden: tuple[str, ...] = ()
    reason: str | None = None

    def describe(self) -> str:
        explained = f" ({self.reason})" if self.reason else ""
        if self.binding_origin is not None:
            explained = f" ({BINDING_ORIGINS[self.binding_origin]})"
            if self.overridden:
                explained += " overriding " + ", ".join(self.overridden)
        if self.origin == "producer":
            made = ", ".join(f"{ref.node}.{ref.output}" for ref in self.references)
            return f"{self.name} <- {made}{explained}"
        if self.origin == "source":
            return f"{self.name} <- source " + ", ".join(map(str, self.sources))
        return f"{self.name} <- none{explained}"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class PlanContext:
    """The captured request, including delivery destinations that run hashes leave out."""

    design_hash: str
    design_root: Path
    runner_cwd: Path
    run_root: Path
    hashed_run_dirs: bool
    debug: bool
    input_settings: Mapping[str, Any]
    #: The selected target of the design. It is no part of any identity, but it is where every
    #: node runs (`<design>/<target>/<flow>`), so a plan belongs to the target it was made for.
    target: str | None = None


@dataclass(frozen=True)
class PlanNode:
    """A final effective configuration. Execution and inspection receive private copies."""

    name: str
    flow_class: type[Flow]
    _settings: Flow.Settings = field(repr=False)
    flowrun_hash: str
    run_path: Path
    inputs: tuple[ResolvedInput, ...] = ()
    switched_on: tuple[str, ...] = ()
    key: NodeKey | None = None
    #: The settings-only hash and the ordered input origins that `flowrun_hash`, the identity of
    #: the node, is made of (`bindings.node_identity`). The launcher keeps them so that it can
    #: recompute the identity.
    settings_hash: str = ""
    origins: tuple[tuple[str, Any], ...] = ()

    @property
    def settings(self) -> Flow.Settings:
        return self._settings.model_copy(deep=True)

    @property
    def node_key(self) -> NodeKey:
        """The key of the node in its plan; `name` is its label."""
        return self.key or NodeKey(self.name)


@dataclass(frozen=True)
class Plan:
    """Producers before consumers, with the requested node last."""

    requested: str
    nodes: tuple[PlanNode, ...]
    context: PlanContext
    #: The request as given (the elements of a chain) and its binding layers: data for
    #: explanation and validation, immutable like the rest.
    request: FlowRequest | None = None
    bindings: tuple[BindingLayer, ...] = ()

    def __contains__(self, name: str | NodeKey) -> bool:
        return any(name in (node.name, node.node_key) for node in self.nodes)

    def node(self, name: str | NodeKey) -> PlanNode:
        return next(node for node in self.nodes if name in (node.name, node.node_key))


def check_launchable(
    flow_cls: type[Flow],
    settings: Flow.Settings,
    design: Design,
    unreached: Mapping[str, str] | None = None,
) -> None:
    """Check final settings and sources without constructing a flow or probing tools.

    `unreached` explains a missing required setting (`Flow.check_required_settings`)."""
    problems = written_path_problems(settings)
    if problems:
        raise FlowSettingsError(
            [(key, message, None, "value_error") for key, message in problems], flow_cls.Settings
        )
    flow_cls.check_required_settings(settings, unreached)
    flow_cls.check_design_supported(design, settings)
    try:
        flow_cls.check_settings_supported(settings)
    except FlowSettingsError as error:
        error.refuses_support = True
        raise


def planned_design_of(
    planned: Plan | Sequence[_Request],
    node: PlanNode | _Request,
    design: Design,
    run_path: Callable[..., Path],
) -> Design:
    """Return the design that `node` is planned to read: the one view of it that every check uses.

    That is the loaded `design`, or, when the `design` input of the node comes from a converter
    (`xeda.flow.derived.planned_design`), the design that the converter will derive. The view
    serves checks only: the languages the flow will get, its metadata and its clocks. It is no
    identity. `planned` is the plan, or the requests of the resolver while it builds one.
    `run_path` is the `run_path_of` of the launcher, which names the run directory of the
    converter (without a run hash) for messages."""
    if isinstance(planned, Plan):
        assert isinstance(node, PlanNode)
        selected = next((item for item in node.inputs if item.name == "design"), None)
        if selected is None or not selected.references:
            return design
        producer = planned.node(selected.references[0].node)
        flow_class, settings, label = producer.flow_class, producer.settings, producer.name
    else:
        found = node.producers.get("design") if isinstance(node, _Request) else None
        if not found:
            return design
        child = found[0][0]
        assert child.settings is not None
        flow_class, settings, label = child.cls, child.settings, child.label
    conversion = flow_class.design_conversion(settings)
    if conversion is None:
        return design
    return planned_design(
        design, conversion, run_path(design.name, label, None, target=design.target)
    )


def _explicit(value: Any) -> Any:
    """Recover the input of a model without turning derived or default fields into contributions.

    Descend even into unset parent fields: callers can edit a nested model that was created by
    default. FPGA part parsing adds fields to `fields_set`; retain only the differences from
    that parsing.
    """
    if isinstance(value, FPGA) and value.part:
        inferred = FPGA(part=value.part)
        return {
            "part": value.part,
            **{
                key: _explicit(getattr(value, key))
                for key in type(value).model_fields
                if getattr(value, key) != getattr(inferred, key)
            },
        }
    if isinstance(value, BaseModel):
        result = {}
        for key in type(value).model_fields:
            item = getattr(value, key)
            child = _explicit(item)
            if key in value.model_fields_set or (isinstance(item, BaseModel) and child):
                if isinstance(value, PhysicalClock) and key == "name" and not child:
                    continue
                result[key] = child
            elif isinstance(item, Mapping):
                edited = {k: _explicit(v) for k, v in item.items() if isinstance(v, BaseModel)}
                if edited:
                    result[key] = edited
        if isinstance(value, WithFpgaBoardSettings) and value.board:
            if value.fpga == value._board_fpga(value.board_data()):
                result.pop("fpga", None)
        return result
    if isinstance(value, Mapping):
        return {key: _explicit(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_explicit(item) for item in value]
    return deepcopy(value)


def _leaves(values: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    result = {}
    for key, value in values.items():
        if isinstance(value, Mapping) and value:
            result.update(_leaves(value, (*prefix, key)))
        else:
            result[(*prefix, key)] = value
    return result


def _put(values: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        old = values.get(key)
        if not isinstance(old, dict):
            values[key] = {}
        values = values[key]
    values[path[-1]] = deepcopy(value)


@dataclass(frozen=True)
class _Location:
    label: str
    kind: str = "file"


@dataclass
class _Located:
    values: dict[str, Any] = field(default_factory=dict)
    locations: dict[tuple[str, ...], _Location] = field(default_factory=dict)
    clock_inputs: dict[tuple[str, ...], dict[str, Any]] = field(default_factory=dict)


def _at(values: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    for key in path:
        values = values.setdefault(key, {})
    return values


def _overlay(base: _Located, layer: _Located, cls: type[Flow]) -> _Located:
    """Merge as `settings_layers` does, keeping single-clock spellings.

    A single-clock spelling stays as written until the lower layer is known."""
    values = deepcopy(layer.values)
    incoming = dict(layer.locations)
    clock_inputs = dict(base.clock_inputs)
    retained = dict(base.locations)
    for path, clock_input in layer.clock_inputs.items():
        subtree = _at(values, path)
        subtree.pop("clocks", None)
        subtree.update(clock_input)
        existing = _at(deepcopy(base.values), path).get("clocks", {})
        clock = clock_input.get("clock", {})
        name = clock.get("name") or "main_clock"
        destination = name
        if existing and name not in existing:
            if name == "main_clock":
                destination = next(iter(existing))
            elif len(existing) == 1:
                old_name = next(iter(existing))
                retained = {
                    (
                        (*path, "clocks", name, *key[len(path) + 2 :])
                        if key[: len(path) + 2] == (*path, "clocks", old_name)
                        else key
                    ): loc
                    for key, loc in retained.items()
                }
        incoming = {
            (
                (*path, "clocks", destination, *key[len(path) + 2 :])
                if key[: len(path) + 2] == (*path, "clocks", name)
                else key
            ): loc
            for key, loc in incoming.items()
        }
        if existing:
            clock_inputs.pop(path, None)
        else:
            clock_inputs[path] = clock_input
    merged = merge_layers(base.values, values, settings_cls=cls.Settings)
    leaves = _leaves(merged)
    locations = {path: loc for path, loc in retained.items() if path in leaves}
    locations.update({path: loc for path, loc in incoming.items() if path in leaves})
    return _Located(merged, locations, clock_inputs)


def _clock_inputs(
    values: dict[str, Any], model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], dict[str, Any]]:
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    if "clock" in values and "clocks" not in values and "clocks" in model.model_fields:
        raw = values["clock"]
        if isinstance(raw, Mapping):
            result[prefix] = {"clock": deepcopy(raw)}
    for key, value in values.items():
        info = model.model_fields.get(key)
        child = nested_model(info.annotation) if info else None
        if child and isinstance(value, dict):
            result.update(_clock_inputs(value, child, (*prefix, key)))
    return result


def _located(raw: Mapping[str, Any], cls: type[Flow], label: str, kind: str) -> _Located:
    values = expand_forms(cls.Settings, merge_layers(_explicit(raw), settings_cls=cls.Settings))
    locations = {path: _Location(label, kind) for path in _leaves(values)}
    # `settings_layers` synthesizes a name when a singular spelling creates a clock. That name is
    # not input from the caller.
    original = merge_layers(_explicit(raw))
    singular = original.get("clock", {})
    if "clock" in original and isinstance(singular, Mapping) and not singular.get("name"):
        locations = {
            path: loc
            for path, loc in locations.items()
            if not (path[0] == "clocks" and path[-1] == "name")
        }
    return _Located(values, locations, _clock_inputs(original, cls.Settings))


def _compose(cls: type[Flow], sections: Mapping[str, Any], origin: str, kind: str) -> _Located:
    own = _located(sections.get(cls.name, {}), cls, f"`flows.{cls.name}` in {origin}", kind)
    # Attach the original location to each leaf, before composition loses that route.
    own.locations = {
        path: _Location(f"{loc.label} ({'.'.join(path)})", loc.kind)
        for path, loc in own.locations.items()
    }
    located = _overlay(_Located(), own, cls)
    # `settings_layers` owns ordinary precedence and aliases. This traversal carries the parallel
    # locations.
    composed = compose_flow_settings(cls, [sections])
    located.values = _located(composed, cls, origin, kind).values
    return located


@dataclass
class _Request:
    """One node while it is resolved: reached once, however many consumers demand it."""

    cls: type[Flow]
    raw: _Located
    requester: str
    inputs: list[ResolvedInput] = field(default_factory=list)
    #: The distinct producers this node reads, in order of first use.
    children: list[_Request] = field(default_factory=list)
    #: Maps an input name to its ordered (producer, output key) references.
    producers: dict[str, list[tuple[_Request, str]]] = field(default_factory=dict)
    #: Every output of this node that a consumer reads: the union over the whole graph.
    needed: set[str] = field(default_factory=set)
    switched: tuple[str, ...] = ()
    settings: Flow.Settings | None = None
    key: NodeKey | None = None

    @property
    def label(self) -> str:
        return self.key.label if self.key else self.cls.name


def _check_links_pass_through(
    flow_request: FlowRequest | None, requests: Sequence[_Request]
) -> None:
    """Refuse a chain link when a flow that it passes through is not in the plan.

    The check covers every flow of `link.through`, also a flow that the link does not bind. A
    source or a binding of an input can take such a flow out of the plan. The request would then
    list a flow that does not run, so it would not mean what it says."""
    if flow_request is None:
        return
    retained = {request.cls: request for request in requests}
    for index, link in enumerate(flow_request.links):
        if not link.through:
            continue
        # The bound flows are in `through`, and the next element itself is always in the plan.
        for missing in dict.fromkeys(
            [*link.through, *(bound for bound, _input, _output in link.binds)]
        ):
            if missing in retained:
                continue
            elements = flow_request.elements[index : index + 2]
            graph = default_graph(elements[1].flow_class)
            cause = ""
            for edge in graph.edges:
                if (
                    edge.consumer in retained
                    and edge.producer not in retained
                    and (
                        edge.producer is missing or missing in default_graph(edge.producer).flows()
                    )
                ):
                    resolved = next(
                        item for item in retained[edge.consumer].inputs if item.name == edge.input
                    )
                    where = f"`{edge.consumer.name}.{edge.input}`"
                    cause = (
                        f"the source of {where} takes"
                        if resolved.origin == "source"
                        else f"the binding of {where} at {resolved.binding_location} takes"
                    )
                    break
            raise FlowSettingsException(
                f"The chain link `{request_text(elements)}` passes through `{missing.name}`, "
                + (f"which {cause} out of the plan." if cause else "which the plan does not keep.")
            )


def _error(cls: type[Flow], leaf: str, message: str) -> FlowSettingsError:
    return FlowSettingsError([(leaf, message, None, "shared_setting_conflict")], cls.Settings)


def _shown(value: Any) -> str:
    """Return `value` as a message shows it: a quantity as its text (`10 ns`), anything else
    as its `repr`, so that text stays quoted."""
    return str(value) if isinstance(value, Quantity) else repr(value)


def _given_at(location: _Location | None) -> str:
    """Return ` at <where it was given>`, or nothing for a value that has no recorded origin."""
    return f" at {location.label}" if location else ""


def _components(requests: list[_Request], shared: str) -> list[list[_Request]]:
    """Group `requests` into connected components for the shared setting `shared`.

    A component follows only edges whose endpoints both expose this field."""
    neighbors: dict[int, list[_Request]] = {id(r): [] for r in requests}
    for parent in requests:
        for child in parent.children:
            if (
                shared in parent.cls.Settings.model_fields
                and shared in child.cls.Settings.model_fields
            ):
                neighbors[id(parent)].append(child)
                neighbors[id(child)].append(parent)
    seen: set[int] = set()
    components = []
    for request in requests:
        if id(request) in seen or shared not in request.cls.Settings.model_fields:
            continue
        stack, group = [request], []
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            group.append(current)
            stack.extend(neighbors[id(current)])
        components.append(group)
    return components


#: The contribution of one node to a shared setting at one leaf: the value it propagates, the
#: key it is compared by, and where it was given.
_Leaf = tuple[Any, Any, "_Location"]


def _normalized_leaves(
    request: _Request, shared: str, context: dict[str, Any]
) -> dict[tuple[str, ...], _Leaf]:
    result: dict[tuple[str, ...], _Leaf] = {}
    if shared == "clocks":
        clocks = request.raw.values.get("clocks", {})
        if not isinstance(clocks, Mapping):
            settings_in_context(request.cls, {"clocks": clocks}, **context)
        for name, raw_clock in clocks.items():
            if not isinstance(raw_clock, Mapping):
                raise _error(request.cls, f"clocks.{name}", "a clock must be a mapping")
            if isinstance(raw_clock, Mapping) and ("period" in raw_clock or "freq" in raw_clock):
                try:
                    PhysicalClock.model_validate(raw_clock)
                except ValueError as error:
                    raise _error(request.cls, f"clocks.{name}", str(error)) from error
    for path, loc in request.raw.locations.items():
        if not path or path[0] != shared:
            continue
        value = _leaves(request.raw.values).get(path)
        if value == {}:
            continue
        target = path
        try:
            if shared == "fpga" and len(path) == 2:
                fpga = FPGA.model_validate({"vendor": "resolver", path[1]: value})
                value = getattr(fpga, path[1])
            elif shared == "clocks" and len(path) == 3:
                if path[-1] == "freq":
                    value = PhysicalClock.model_validate({"freq": value}).period
                    target = (*path[:-1], "period")
                else:
                    clock = PhysicalClock.model_validate({"period": "1 ns", path[-1]: value})
                    value = getattr(clock, path[-1])
            elif shared == "board":
                # The board and database may come from different nodes. Lookup is valid
                # only after they agree; here check the same strict name syntax as the field.
                if value is not None and not isinstance(value, str):
                    raise ValueError("board must be a string or None")
            elif shared in SHARED_PATH_LIST_SETTINGS:
                model = settings_in_context(request.cls, {shared: value}, **context)
                value = [
                    (context["design_root"] / item).resolve() for item in getattr(model, shared)
                ]
                if not value:
                    continue  # no file says nothing: the flow reads the files of the platform
            elif shared in (*SHARED_PATH_SETTINGS, "dont_use_cells"):
                model = settings_in_context(request.cls, {shared: value}, **context)
                value = getattr(model, shared)
                if shared in SHARED_PATH_SETTINGS and value is not None:
                    value = (context["design_root"] / value).resolve()
            elif shared == "custom_boards_file":
                board = WithFpgaBoardSettings.from_input({shared: value}, **context)
                value = getattr(board, shared)
            elif shared == "hierarchical":
                value = getattr(
                    settings_in_context(request.cls, {shared: value}, **context), shared
                )
            elif shared == "wire_rc":
                if value is None:
                    continue  # a `wire_rc` that is not given says nothing about any leaf
                nested: dict[str, Any] = {}
                _put(nested, path[1:], value)
                model = settings_in_context(request.cls, {shared: nested}, **context)
                rc = getattr(model, shared)
                leaves: Any = rc.model_dump(mode="json") if rc else None
                for part in path[1:]:
                    leaves = leaves[part]
                value = leaves
        except ValueError as error:
            raise _error(request.cls, ".".join(path), f"{error} at {loc.label}") from error
        result[target] = (value, value, loc)
    return result


def _group_value(group: list[_Request], name: str) -> Any:
    """Return the value that a node of `group` gives to the shared setting `name`, or None.
    After agreement every node that declares it holds the same value."""
    return next(
        (value for request in group if (value := request.raw.values.get(name)) is not None),
        None,
    )


def _pin_pdks(requests: list[_Request], context: dict[str, Any]) -> None:
    """Fill in the PDK pin that a user left out, in every node of each group of ASIC flows.

    A group is a set of connected nodes that declare `platform`. When the group has a `platform`
    or a `platform_file`, and `pdk_provider` or `pdk_version` is unset, the provider is that of
    the residue (`providers.default`) and the version is the one that the residue pins for the
    provider (`unpinned` for a directory, which pins nothing). The pin then belongs to the
    settings, so it is part of the identity of every node. The residue is the bundled one, or
    the user file of `platform_file`, which planning reads like a design file. A group without a
    platform gets nothing.

    The settings of a flow that declares `platform` must inherit `AsicPlatformSettings`, so that
    every node of a group declares the pin. A node that cannot hold the pin would leave it out of
    its identity. Such a flow is a programming error."""
    from ..flow.asic import platform_residues
    from ..platforms.providers import PROVIDERS, UNPINNED
    from ..platforms.residue import effective_residue

    for group in _components(requests, "platform"):
        for request in group:
            needed = (*PIN_SETTINGS, *SHARED_PATH_LIST_SETTINGS)
            if not all(name in request.cls.Settings.model_fields for name in needed):
                raise TypeError(
                    f"{request.cls.name} declares `platform` but not {', '.join(needed)}: "
                    "its settings must inherit AsicPlatformSettings"
                )
        provider = _group_value(group, "pdk_provider")
        version = _group_value(group, "pdk_version")
        platform = _group_value(group, "platform")
        platform_file = _group_value(group, "platform_file")
        if provider is not None and version is not None:
            continue
        if platform is None and platform_file is None:
            continue
        first = group[0]
        if platform is not None:
            # The name is checked as the field checks it, before it is looked for.
            settings_in_context(first.cls, {"platform": platform}, **context)
        if platform_file is not None:
            platform_file = Path(platform_file)
            if not platform_file.is_absolute():
                platform_file = (context["design_root"] / platform_file).resolve()
        try:
            bundled, user = platform_residues(platform, platform_file)
        except FlowSettingsException as error:
            raise _error(
                first.cls, "platform_file" if platform_file else "platform", str(error)
            ) from error
        try:
            residue = effective_residue(bundled, user, provider)
        except ValueError as error:
            raise _error(
                first.cls, "platform_file" if platform_file else "platform", str(error)
            ) from error
        if provider is None:
            provider = residue.providers.default
        if provider in PROVIDERS and version is None:
            entry = residue.providers.entry(provider)
            if entry is not None:
                version = UNPINNED if provider == "directory" else entry.version
        for request in group:
            for name, value in (("pdk_provider", provider), ("pdk_version", version)):
                if value is not None and request.raw.values.get(name) is None:
                    request.raw.values[name] = value


def _nonshared_input(cls: type[Flow], values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of `values` without the shared settings, for syntax validation.

    Validation must not erase malformed nested values during reconciliation."""
    ordinary = deepcopy(dict(values))
    for shared in SHARED_SETTINGS:
        if shared in cls.Settings.model_fields:
            ordinary.pop(shared, None)
    if "clocks" in cls.Settings.model_fields:
        ordinary.pop("clock", None)
    return ordinary


def _shared_locations(
    raw: _Located, cls: type[Flow], context: dict[str, Any]
) -> dict[tuple[str, ...], _Leaf]:
    """Return the shared leaves of a composed mapping, as validated models read them.

    No API override is invented."""
    try:
        settings_in_context(cls, _nonshared_input(cls, raw.values), **context)
    except FlowSettingsError as error:
        suggest_dependency_node(cls, error)
        raise
    request = _Request(cls, raw, cls.name)
    result = {}
    for shared in SHARED_SETTINGS:
        if shared in cls.Settings.model_fields:
            result.update(_normalized_leaves(request, shared, context))
    return result


#: Where each agreed leaf of each node was given, by the `id` of the node. Every node of a group
#: holds the value of the winning contribution, so the location of the winner is the origin of
#: that value.
_Given = dict[int, dict[tuple[str, ...], "_Location"]]


def _agree(
    requests: list[_Request], shared: str, context: dict[str, Any], *, provisional: bool = False
) -> _Given:
    """Agree `shared` along the edges of `requests`, and return where each agreed leaf was given."""
    given: _Given = {}
    for group in _components(requests, shared):
        candidates: dict[tuple[str, ...], list[tuple[_Request, Any, Any, _Location]]] = {}
        for request in group:
            for path, (value, key, loc) in _normalized_leaves(request, shared, context).items():
                candidates.setdefault(path, []).append((request, value, key, loc))
        if shared == "board" and not any(r.raw.values.get("custom_boards_file") for r in group):
            # A bundled board is found by its name in any letter case, so two spellings are one
            # board; the names of a custom database are case-sensitive and compare as written.
            candidates = {
                path: [
                    (r, v, k.lower() if isinstance(k, str) else k, loc) for r, v, k, loc in items
                ]
                for path, items in candidates.items()
            }
        agreed: dict[str, Any] = {}
        winning: dict[tuple[str, ...], _Location] = {}
        for path, contributions in candidates.items():
            rank = {"file": 0, "cli": 1, "api": 2}
            highest = max(rank[loc.kind] for _r, _v, _k, loc in contributions)
            winners = [c for c in contributions if rank[c[3].kind] == highest]
            # The value of the winner is propagated exactly as given. Contributions agree by key.
            first, value, key, location = winners[0]
            for other, alternative, other_key, other_location in winners[1:]:
                if key != other_key and not provisional:
                    leaf = ".".join(path)
                    raise _error(
                        first.cls,
                        leaf,
                        f"{first.cls.name} {leaf}={_shown(value)} at {location.label} disagrees with "
                        f"{other.cls.name} {leaf}={_shown(alternative)} at "
                        f"{other_location.label}",
                    )
            _put(agreed, path[1:], value) if len(path) > 1 else agreed.update({shared: value})
            winning[path] = location
        if not agreed:
            continue
        value = agreed[shared] if shared in agreed else agreed
        if shared == "clocks":
            for name, clock in agreed.items():
                if "name" not in clock:
                    inherited_name = next(
                        (
                            r.raw.values.get("clocks", {}).get(name, {}).get("name")
                            for r in group
                            if r.raw.values.get("clocks", {}).get(name, {}).get("name")
                        ),
                        None,
                    )
                    if inherited_name:
                        clock["name"] = inherited_name
        for request in group:
            request.raw.values[shared] = deepcopy(value)
            given.setdefault(id(request), {}).update(winning)
    return given


def _unreached(
    request: _Request,
    requests: list[_Request],
    layers: Sequence[tuple[str, Mapping[str, Any], str]],
) -> dict[str, str]:
    """Return, for each required setting that `request` lacks, the sections that give it.

    These sections give the setting without reaching `request`.

    Such a section belongs to a flow that is not part of this run, or to a flow that shares no
    edge with `request` along which the setting is shared. A board gives `fpga` too: a
    board-aware flow derives its device from it."""
    assert request.settings is not None
    nodes = {other.cls.name for other in requests}
    found: dict[str, str] = {}
    for name in request.cls.required_settings_for(request.settings):
        if not is_unset(getattr(request.settings, name, None)):
            continue
        reached = {request.cls.name}
        if name in SHARED_SETTINGS:
            for group in _components(requests, name):
                if any(member is request for member in group):
                    reached = {member.cls.name for member in group}
        notes = []
        for label, sections, _kind in layers:
            for flow, values in sections.items():
                if flow in reached or not isinstance(values, Mapping):
                    continue
                for key in (name, "board") if name == "fpga" else (name,):
                    if is_unset(values.get(key)):
                        continue
                    why = (
                        f"{flow} is not part of this run"
                        if flow not in nodes
                        else f"no edge of this run carries `{name}` from {flow} to it"
                    )
                    notes.append(
                        f"The `{key}` in `flows.{flow}` in {label} does not reach "
                        f"{request.cls.name}: {why}"
                    )
        if notes:
            found[name] = ". ".join(notes)
    return found


def _replaceable_by_a_source(producer: _Request, requests: list[_Request]) -> str:
    """Return `; a <Type> source would supply <consumer>'s <input> and skip <producer>`, or "".

    It applies when one source takes `producer` out of the plan. That is the case when exactly
    one input of the plan reaches the producer, and by default, and a typed source of a type
    that the input takes replaces it. Otherwise the plan keeps `producer` whatever source the
    design adds, and the refusal adds nothing. An input bound to the producer (a chain, a saved
    binding, the command line or the API) keeps it, and so does any other input that reaches
    it."""
    edges = [
        (consumer, name)
        for consumer in requests
        for name, reached in consumer.producers.items()
        if any(child is producer for child, _output in reached)
    ]
    if len(edges) != 1:
        return ""
    consumer, name = edges[0]
    assert consumer.settings is not None
    selected = next((item for item in consumer.inputs if item.name == name), None)
    if selected is None or selected.binding_origin is not None:
        return ""
    if declared_inputs(consumer.cls)[name].is_design:
        return ""
    kinds = "/".join(kind.name for kind in selected_types(consumer.cls, consumer.settings, name))
    return f"; a {kinds} source would supply {consumer.label}'s {name} and skip {producer.label}"


def _sections(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Use the flow-name checks of `settings_layers`, and keep the single-clock input syntax."""
    normalized = merge_flow_sections(values, flow_class_for=registered_flow)
    for name, raw in (values or {}).items():
        cls = registered_flow(name)
        normalized[cls.name if cls else name] = merge_layers(_explicit(raw))
    return normalized


def _check_design_producers(request: _Request, selected: ResolvedInput) -> None:
    """Refuse a `design` input whose producer has no design output under the final settings.

    The producer needs an enabled design output, and its conversion under its settings has to
    write only languages that the consumer reads, and convert one that it does not. The consumer
    converts no design under its settings: a conversion is one hop."""
    where = f"{request.cls.name}.{selected.name}"
    reads = request.cls.reads_sources or frozenset()
    for child, output_name in request.producers.get(selected.name, ()):
        assert child.settings is not None
        made = declared_outputs(child.cls)[output_name]
        conversion = (
            child.cls.design_conversion(child.settings)
            if made.is_design and output_enabled(child.settings, made)
            else None
        )
        if conversion is None or not conversion.produces <= reads:
            writes = (
                "/".join(sorted(member.name for member in conversion.produces))
                if conversion is not None
                else ""
            )
            raise FlowSettingsException(
                f"{where}: {child.cls.name}.{output_name} has no compatible output under the "
                f"selected settings (takes design written in "
                f"{'/'.join(sorted(member.name for member in reads)) or 'nothing'}, makes "
                f"{writes or 'nothing'})"
                + (f"; bound by {selected.binding_location}" if selected.binding_location else "")
            )
        refusal = design_edge_refusal(
            child.cls,
            request.cls,
            producer_settings=child.settings,
            consumer_settings=request.settings,
        )
        if refusal is not None:
            raise FlowSettingsException(
                f"{where}: {refusal}"
                + (f"; bound by {selected.binding_location}" if selected.binding_location else "")
            )


def _languages(types: Sequence[Any]) -> str:
    """Return the names of source types for a message: `Bluespec`, `Bluespec and Vhdl`."""
    return " and ".join(sorted(member.name for member in types))


def converting_flow(cls: type[Flow], design: Design) -> tuple[type[Flow], str, str] | None:
    """Return the converter that supplies the unbound `design` input of `cls`, or None.

    The rule has one hop and looks at `rtl` only. A flow with a design output itself gets no
    conversion. The languages of `rtl.sources` that `cls` does not read are the unread ones.
    The registered converters that convert one of them and write only languages that `cls` reads
    fit (`chains.converter_edges`, from the declarations alone). None fits: the design input stays
    unbound and the flow refuses the language itself. One fits: it is the producer, with the
    output it makes and the reason. More than one fits: the request is ambiguous and the error
    names the choices."""
    reads = cls.reads_sources
    if reads is None:
        return None
    unread = {
        source.type
        for source in design.rtl.sources
        if source.type in LANGUAGE_TYPES and source.type not in reads
    }
    if not unread:
        return None
    fitting = []
    for edge in converter_edges(cls):
        conversion = edge.producer.design_conversion(edge.producer.Settings())
        if conversion is not None and conversion.transforms & unread:
            fitting.append((edge, conversion.transforms & unread))
    if not fitting:
        return None
    if len(fitting) > 1:
        names = ", ".join(edge.producer.name for edge, _converted in fitting)
        first = fitting[0][0].producer.name
        converted_all = set().union(*(converted for _edge, converted in fitting))
        raise FlowSettingsException(
            f"{cls.name} does not read the design's {_languages(list(converted_all))} "
            f"sources, and more than one flow converts them: {names}. Choose one with a chain "
            f"(`{first}+{cls.name}`) or a binding (`flows.{cls.name}.inputs.design: {first}`)"
        )
    edge, converted = fitting[0]
    assert edge.output is not None
    return (
        edge.producer,
        edge.output,
        f"the design has {_languages(list(converted))} sources, which {cls.name} " "does not read",
    )


def resolve(
    flow_cls: type[Flow],
    design: Design,
    settings: Mapping[str, Any] | Flow.Settings | None,
    sections: Mapping[str, Any] | None = None,
    *,
    runner_cwd: Path,
    run_root: Path,
    hashed_run_dirs: bool,
    run_path: Callable[..., Path],
    origins: Sequence[tuple[str, Mapping[str, Any]]] = (),
    command_line: Mapping[str, Mapping[str, Any]] | None = None,
    api_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    debug: bool = False,
    flow_request: FlowRequest | None = None,
    binding_layers: Sequence[BindingLayer] = (),
    identify_design: bool = True,
) -> Plan:
    """Resolve the graph, agree settings, union the demands on each producer, and freeze the plan.

    The union covers every demand on the outputs of each producer. Resolution also enables the
    outputs that consumers need.

    The graph comes from explicit bindings first, then sources, then default producers. The
    plan is validated before it is frozen.

    Without `identify_design`, the plan judges a declared design, whose generated sources may
    not exist yet (`design.deferring_load_side_effects`). It is not identified by content, and
    its empty `design_hash` is one that no launch accepts (`FlowLauncher._validate_plan`)."""
    context = dict(design_root=design.root_path, runner_cwd=runner_cwd)
    if isinstance(settings, Flow.Settings) and settings.context:
        context = {key: settings.context.get(key) or value for key, value in context.items()}
    layers = [
        (label, _sections(values), "cli" if label == COMMAND_LINE_ORIGIN else "file")
        for label, values in origins
    ]
    if not layers:
        layers.append((SUPPLIED_SECTIONS_ORIGIN, _sections(sections), "file"))
    cli_sections = _sections(command_line) if command_line else {}
    if cli_sections:
        layers.append((COMMAND_LINE_ORIGIN, cli_sections, "cli"))
    if api_overrides:
        layers.append((API_ORIGIN, _sections(api_overrides), "api"))

    root_raw = _Located()
    for label, values, kind in layers:
        root_raw = _overlay(root_raw, _compose(flow_cls, values, label, kind), flow_cls)
    # `settings` is normally composed by `settings_layers` already. Preserve the locations of
    # identical leaves. Only additional edits by the caller form a final contribution of the
    # direct API.
    supplied = _located(
        _explicit(settings) if settings is not None else {}, flow_cls, API_ORIGIN, "api"
    )
    known = _leaves(root_raw.values)
    normalized_known = _shared_locations(root_raw, flow_cls, context)
    normalized_supplied = _shared_locations(supplied, flow_cls, context)
    supplied.locations = {
        path: root_raw.locations.get(path, loc) if known.get(path) == supplied_leaves[path] else loc
        for path, loc in supplied.locations.items()
        for supplied_leaves in [_leaves(supplied.values)]
        if path in supplied_leaves
        and not (known.get(path) == supplied_leaves[path] and path not in root_raw.locations)
    }
    if isinstance(settings, Flow.Settings):
        # The single-clock normalization puts a generated name in `model_fields_set` too. A name
        # equal to its mapping key carries no independent naming contribution.
        supplied.locations = {
            path: loc
            for path, loc in supplied.locations.items()
            if not (
                len(path) >= 3
                and path[-3] == "clocks"
                and path[-1] == "name"
                and _leaves(supplied.values).get(path) == path[-2]
            )
        }
    for path, (_value, key, _location) in normalized_supplied.items():
        if path in normalized_known and key == normalized_known[path][1]:
            # Models store a frequency as a period. Locate that original contribution too.
            supplied.locations[path] = normalized_known[path][2]
            if path[-1] == "period":
                supplied.locations[(*path[:-1], "freq")] = normalized_known[path][2]
    root_raw = _overlay(root_raw, supplied, flow_cls)
    check_chain_collisions(binding_layers, flow_request)
    by_node: dict[NodeKey, _Request] = {}
    visiting: list[NodeKey] = []

    def kinds(types: Sequence[Any]) -> str:
        return "/".join(t.name for t in types)

    def bound_output(
        cls: type[Flow], declaration: Any, producer: type[Flow], wanted: str | None, where: str
    ) -> str:
        """Return the output key that one explicit reference selects.

        The choice follows the declared kinds and the cardinality. Types that depend on the
        settings are judged once the final settings agree."""
        outputs = declared_outputs(producer)
        if not outputs:
            raise FlowSettingsException(
                f"{where}: {producer.name} declares no outputs, so it cannot supply "
                f"{cls.name}.{declaration.name}"
            )
        made = ", ".join(f"{out.name} ({kinds_text(out)})" for out in outputs.values())
        # The one edge predicate, shared with chain links (`chains.fitting_outputs`).
        fitting, many = fitting_outputs(producer, declaration, output=wanted)
        if wanted is not None:
            if wanted in many:
                raise FlowSettingsException(
                    f"{where}: {cls.name}.{declaration.name} takes one file, but "
                    f"{producer.name}.{wanted} produces many"
                )
            if not fitting:
                raise FlowSettingsException(
                    f"{where}: {cls.name}.{declaration.name} takes {kinds_text(declaration)}; "
                    f"{producer.name}.{wanted} makes {kinds_text(outputs[wanted])}. "
                    f"{producer.name} makes: {made}"
                )
            return wanted
        if len(fitting) == 1:
            return fitting[0]
        if not fitting:
            if many:
                raise FlowSettingsException(
                    f"{where}: {cls.name}.{declaration.name} takes one file, but "
                    f"{producer.name}.{many[0]} produces many"
                )
            raise FlowSettingsException(
                f"{where}: {producer.name} has no output {cls.name}.{declaration.name} can "
                f"take ({kinds_text(declaration)}); it makes: {made}"
            )
        raise FlowSettingsException(
            f"{where}: {cls.name}.{declaration.name} can take several outputs of "
            f"{producer.name}: "
            + ", ".join(f"{producer.name}.{name}" for name in fitting)
            + "; name one"
        )

    def demand(request: _Request, name: str, child: _Request, output: str) -> None:
        child.needed.add(output)
        request.producers.setdefault(name, []).append((child, output))
        if all(child is not known for known in request.children):
            request.children.append(child)

    def discover(cls: type[Flow], raw: _Located | None, requester: str) -> _Request:
        key = NodeKey(cls.name)
        if key in visiting:
            raise FlowSettingsException(
                "Flow declaration cycle: " + " -> ".join(k.label for k in [*visiting, key])
            )
        if key in by_node:
            return by_node[key]
        if raw is None:
            # A producer's settings come from its own `flows.<producer>` sections.
            raw = _Located()
            for label, values, kind in layers:
                raw = _overlay(raw, _compose(cls, values, label, kind), cls)
        visiting.append(key)
        request = _Request(cls, raw, requester, key=key)
        bound: Mapping[str, InputBinding] = (
            node_bindings(binding_layers, key, cls, request=flow_request)
            if binding_layers or flow_request is not None
            else {}
        )
        specialized = getattr(cls.input_types, "__func__") is not getattr(
            Flow.input_types, "__func__"
        )
        for declaration in declared_inputs(cls).values():
            where = f"{cls.name}.{declaration.name}"
            explicit = bound.get(declaration.name)
            if explicit is not None:
                # Binding > design source > default producer: neither is consulted here.
                references = []
                for reference in explicit.references:
                    producer = registered_flow(reference.node.flow)
                    assert producer is not None
                    if declaration.is_design and producer is cls:
                        raise FlowSettingsException(
                            f"{explicit.location}: `{where}` cannot come from {cls.name} itself: "
                            f"{cls.name} converts the design, so it reads the design as it is"
                        )
                    if declaration.is_design:
                        # The rule of the chain, judged before the fit of the outputs.
                        refusal = design_edge_refusal(producer, cls)
                        if refusal is not None:
                            raise FlowSettingsException(f"{explicit.location}: {refusal}")
                    output_name = bound_output(
                        cls, declaration, producer, reference.output, explicit.location
                    )
                    child = discover(producer, None, where)
                    demand(request, declaration.name, child, output_name)
                    references.append(ResolvedReference(child.label, output_name))
                request.inputs.append(
                    ResolvedInput(
                        declaration.name,
                        "producer" if references else "none",
                        references[0].node if references else None,
                        references[0].output if references else None,
                        references=tuple(references),
                        binding_origin=explicit.origin,
                        binding_location=explicit.location,
                        overridden=explicit.overridden,
                    )
                )
                continue
            if declaration.is_design:
                # Unbound, the design input has no `ResolvedInput`: the flow reads the design
                # as it is, and the identity of the node is what it was without the input.
                converter = converting_flow(cls, design)
                if converter is not None:
                    producer_cls, output_name, reason = converter
                    child = discover(producer_cls, None, where)
                    demand(request, declaration.name, child, output_name)
                    request.inputs.append(
                        ResolvedInput(
                            declaration.name,
                            "producer",
                            child.label,
                            output_name,
                            references=(ResolvedReference(child.label, output_name),),
                            reason=reason,
                        )
                    )
                continue
            sources = tuple(
                source.path for source in design.rtl.sources if source.type in declaration.types
            )
            if sources and not specialized:
                if declaration.cardinality != "many" and len(sources) != 1:
                    raise FlowSettingsException(
                        f"{where} takes one {kinds(declaration.types)} file; found "
                        + ", ".join(map(str, sources))
                    )
                request.inputs.append(ResolvedInput(declaration.name, "source", sources=sources))
                continue
            if declaration.producer is None:
                if declaration.required and not specialized:
                    raise FlowSettingsException(
                        f"{cls.name} needs its input `{declaration.name}` "
                        f"({', '.join(t.name for t in declaration.types)}); list a source or "
                        "declare a producer"
                    )
                request.inputs.append(ResolvedInput(declaration.name, "none"))
                continue
            producer = registered_flow(declaration.producer)
            if producer is None:
                raise FlowSettingsException(
                    f"{where} names unknown producer {declaration.producer!r}"
                )
            outputs = declared_outputs(producer)
            matching = [
                out
                for out in outputs.values()
                if not out.is_design and set(out.types) & set(declaration.types)
            ]
            output = (
                outputs.get(declaration.output)
                if declaration.output
                else (matching[0] if len(matching) == 1 else None)
            )
            if output is None:
                raise FlowSettingsException(
                    f"{where}: {producer.name} has no unambiguous compatible output "
                    f"{declaration.output or ''!r}"
                )
            if output.cardinality == "many" and declaration.cardinality != "many":
                raise FlowSettingsException(
                    f"{where} takes one file, but {producer.name}.{output.name} produces many"
                )
            child = discover(producer, None, where)
            demand(request, declaration.name, child, output.name)
            request.inputs.append(
                ResolvedInput(
                    declaration.name,
                    "producer",
                    child.label,
                    output.name,
                    references=(ResolvedReference(child.label, output.name),),
                )
            )
        visiting.pop()
        by_node[key] = request
        return request

    root = discover(flow_cls, root_raw, flow_cls.name)

    requests: list[_Request] = []  # consumers before producers: the order agreement reports in
    order: list[_Request] = []  # producers before consumers: the order of the plan

    def retain(request: _Request) -> None:
        """Collect the nodes that the requested flow still reaches, each once, with demands."""
        if any(request is kept for kept in requests):
            return
        requests.append(request)
        request.children = []
        for edges in request.producers.values():
            for child, _output in edges:
                if all(child is not known for known in request.children):
                    request.children.append(child)
        for child in request.children:
            retain(child)
        order.append(request)

    def demands() -> None:
        requests.clear()
        order.clear()
        retain(root)
        for request in requests:
            request.needed.clear()
        for request in requests:
            for edges in request.producers.values():
                for child, output in edges:
                    child.needed.add(output)

    demands()

    def agree_targets(candidates: list[_Request], *, provisional: bool = False) -> _Given:
        given: _Given = {}

        def agree(shared: str) -> None:
            for node, leaves in _agree(
                candidates, shared, context, provisional=provisional
            ).items():
                given.setdefault(node, {}).update(leaves)

        # Board/database agreement precedes expansion into explicit FPGA contributions.
        for shared in ("board", "custom_boards_file"):
            agree(shared)
        for request in candidates:
            if issubclass(request.cls.Settings, WithFpgaBoardSettings) and request.raw.values.get(
                "board"
            ):
                raw_board: dict[str, Any] = {
                    key: request.raw.values[key]
                    for key in ("board", "custom_boards_file")
                    if key in request.raw.values
                }
                board = WithFpgaBoardSettings.from_input(raw_board, **context)
                if board.fpga:
                    # The device leaves rank as the own board of the node does (a board that it
                    # received along an edge ranks as a file's), and they are located where the
                    # winning board was written.
                    own = request.raw.locations.get(("board",))
                    written = given.get(id(request), {}).get(("board",), own)
                    location = _Location(
                        written.label if written else "the shared board",
                        own.kind if own else "file",
                    )
                    fpga = _explicit(board.fpga)
                    request.raw.values["fpga"] = merge_layers(fpga, request.raw.values.get("fpga"))
                    for path in _leaves(fpga, ("fpga",)):
                        request.raw.locations.setdefault(path, location)
        for shared in SHARED_SETTINGS:
            if shared not in ("board", "custom_boards_file") and (
                not provisional or shared == "fpga"
            ):
                agree(shared)
        return given

    if any(
        getattr(r.cls.input_types, "__func__") is not getattr(Flow.input_types, "__func__")
        for r in requests
    ):
        # A bounded selection pass on copies: the device settings of a consumer break
        # provisional ties. Strict agreement below applies only to edges that survive source
        # displacement.
        proposals = deepcopy(requests)
        agree_targets(proposals, provisional=True)
        for request, proposal in zip(list(requests), proposals):
            values = _nonshared_input(proposal.cls, proposal.raw.values)
            for shared in ("fpga", "board", "custom_boards_file"):
                if shared in proposal.raw.values:
                    values[shared] = proposal.raw.values[shared]
            settings = settings_in_context(proposal.cls, values, **context)
            selected_inputs = []
            for declaration in declared_inputs(request.cls).values():
                old = next((item for item in request.inputs if item.name == declaration.name), None)
                if old is None:  # an unbound design input
                    continue
                if declaration.is_design or old.binding_origin is not None:
                    # An explicit binding is no candidate for displacement by a source.
                    selected_inputs.append(old)
                    continue
                types = selected_types(request.cls, settings, declaration.name)
                sources = tuple(
                    source.path for source in design.rtl.sources if source.type in types
                )
                if sources:
                    selected_inputs.append(
                        ResolvedInput(declaration.name, "source", sources=sources)
                    )
                    request.producers.pop(declaration.name, None)
                else:
                    selected_inputs.append(
                        old if old.origin == "producer" else ResolvedInput(declaration.name, "none")
                    )
            request.inputs = selected_inputs
        # A displaced producer leaves the graph, with its settings and shared leaves.
        demands()

    _check_links_pass_through(flow_request, requests)
    check_related_inputs(
        Node(request.label, request.cls, {item.name: item for item in request.inputs})
        for request in requests
    )
    active = {request.cls.name: request.cls for request in requests}
    default_flows = {**transitive_dependencies(flow_cls)}
    for active_cls in active.values():
        default_flows.update(transitive_dependencies(active_cls))
    if cli_sections:
        # `-s flows.<node>.*` may name a node of the resolved graph, or a default producer a
        # source or binding displaced (its settings are then unused, as in a file).
        check_run_flows(cli_sections, flow_cls, run_flows={*active, *default_flows})
    for unused_name, unused_cls in default_flows.items():
        if unused_name not in active:
            unused = compose_flow_settings(unused_cls, [values for _label, values, _kind in layers])
            settings_in_context(
                unused_cls, unused, **context
            )  # syntax only, no launch requirements
            if unused:
                # A default producer that a source or a binding displaced, or one of its producers.
                log.info("%s is not part of this run: its settings are unused", unused_name)
    given = agree_targets(requests)
    _pin_pdks(requests, context)

    for request in requests:
        try:
            request.settings = settings_in_context(request.cls, request.raw.values, **context)
        except FlowSettingsError as error:
            suggest_dependency_node(request.cls, error)
            raise
        switched = []
        for name in declared_outputs(request.cls):
            if name in request.needed:
                before = request.settings.model_dump()
                try:
                    request.cls.enable_output(request.settings, name, design_name=design.name)
                except ValueError as error:
                    consumers = [
                        consumer.label
                        for consumer in requests
                        if any(
                            producer is request and output == name
                            for edges in consumer.producers.values()
                            for producer, output in edges
                        )
                    ]
                    raise FlowSettingsException(
                        f"{request.cls.name}.{name} is required by "
                        f"{', '.join(consumers) or 'a consumer'}: {error}"
                    ) from error
                if request.settings.model_dump() != before:
                    switched.append(name)
        request.switched = tuple(switched)
        # Assignment validators may change other fields; validate the final snapshot together.
        request.settings = settings_in_context(
            request.cls, request.settings.model_dump(), **context
        )
    assert root.settings is not None
    if debug:
        root.settings.debug = True
    for request in requests:
        assert request.settings is not None
        for child in request.children:
            assert child.settings is not None
            carry_diagnostics(child.settings, request.settings)
    for request in order:
        assert request.settings is not None
        if isinstance(request.settings, WithFpgaBoardSettings) and request.settings.board:
            board_fpga = request.settings._board_fpga(request.settings.board_data())
            if board_fpga and request.settings.fpga:
                for key, value in _explicit(board_fpga).items():
                    actual = getattr(request.settings.fpga, key)
                    if value is not None and actual != value:
                        written = given.get(id(request), {})
                        board_at = written.get(("board",), request.raw.locations.get(("board",)))
                        fpga_at = written.get(
                            ("fpga", key), request.raw.locations.get(("fpga", key))
                        )
                        raise _error(
                            request.cls,
                            f"fpga.{key}",
                            f"board {request.settings.board!r}{_given_at(board_at)} and "
                            f"fpga.{key}={actual!r}{_given_at(fpga_at)} disagree: the board's "
                            f"fpga.{key} is {value!r}",
                        )
    for request in requests:
        assert request.settings is not None
        # A producer of the design that cannot supply it is the first problem, before the
        # languages of the design that it would give.
        for selected in request.inputs:
            if declared_inputs(request.cls)[selected.name].is_design:
                _check_design_producers(request, selected)
        try:
            check_launchable(
                request.cls,
                request.settings,
                planned_design_of(requests, request, design, run_path),
                _unreached(request, requests, layers),
            )
        except NoReadableSource as refusal:
            raise NoReadableSource(
                f"{refusal}{_replaceable_by_a_source(request, requests)}"
            ) from None

    # The final settings select the very same sources and producer formats as discovery.
    for request in requests:
        assert request.settings is not None
        for selected in request.inputs:
            declaration = declared_inputs(request.cls)[selected.name]
            where = f"{request.cls.name}.{selected.name}"
            if declaration.is_design:
                continue  # judged above, before the languages of the design
            types = selected_types(request.cls, request.settings, selected.name)
            sources = tuple(source.path for source in design.rtl.sources if source.type in types)
            if selected.origin == "source" and sources != selected.sources:
                raise FlowSettingsException(
                    f"{where} source selection changed after the settings were agreed"
                )
            # An explicit binding stands whatever sources the design lists for that input.
            if selected.origin != "source" and sources and selected.binding_origin is None:
                raise FlowSettingsException(
                    f"{where} source selection changed after the settings were agreed"
                )
            if selected.origin == "none" and declaration.required:
                raise FlowSettingsException(
                    f"{request.cls.name} needs its input `{selected.name}` "
                    f"({', '.join(t.name for t in types)}); list a source or declare a producer"
                )
            if (
                selected.origin == "source"
                and declaration.cardinality != "many"
                and len(sources) != 1
            ):
                raise FlowSettingsException(
                    f"{where} takes one {kinds(types)} file; found " + ", ".join(map(str, sources))
                )
            if selected.origin == "producer":
                for child, output_name in request.producers[selected.name]:
                    assert child.settings is not None
                    produced = selected_types(child.cls, child.settings, output_name, output=True)
                    if not produced or not set(produced) <= set(types):
                        raise FlowSettingsException(
                            f"{where}: {child.cls.name}.{output_name} has no compatible output "
                            f"under the selected settings (takes {kinds(types) or 'nothing'}, "
                            f"makes {kinds(produced) or 'nothing'})"
                            + (
                                f"; bound by {selected.binding_location}"
                                if selected.binding_location
                                else ""
                            )
                        )

    nodes: list[PlanNode] = []
    identities: dict[str, str] = {}
    for request in order:  # producers first: a consumer's identity includes theirs
        assert request.settings is not None and request.key is not None
        label = request.label
        settings_hash = flowrun_hash(request.cls.name, request.settings, design.name)
        origins = input_origins(request.inputs, identities.__getitem__)
        identity = identities[label] = node_identity(settings_hash, origins)
        nodes.append(
            PlanNode(
                label,
                request.cls,
                request.settings.model_copy(deep=True),
                identity,
                run_path(design.name, label, identity, target=design.target),
                tuple(request.inputs),
                tuple(out for out in declared_outputs(request.cls) if out in request.switched),
                request.key,
                settings_hash,
                origins,
            )
        )
    return Plan(
        flow_cls.name,
        tuple(nodes),
        PlanContext(
            design.parts_hash(DESIGN_PARTS) if identify_design else "",
            design.root_path,
            runner_cwd,
            run_root,
            hashed_run_dirs,
            debug,
            _freeze(as_recorded(root.settings)),
            design.target,
        ),
        flow_request,
        tuple(binding_layers),
    )
