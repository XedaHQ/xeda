"""Pure graph resolution, with origin-preserving shared-setting agreement.

An input is supplied, in this order of precedence, by an explicit binding (a chain adjacency,
or `flows.<consumer>.inputs.<input>` in a file, on the command line or through the API), by
the design's typed sources, or by its declared default producer. Nodes are told apart by
`NodeKey`, each resolved once, so every demand on a producer's outputs is known before its
settings are frozen and hashed.

Composition precedes agreement: a node keeps the origin-first precedence of `settings_layers`, while explicit
shared leaves at different nodes are independent evidence. Only CLI/API leaves override the
whole connected component. No flow instance or execution state is needed to make a plan.
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
from ..dataclass import BaseModel
from ..design import DESIGN_PARTS, Design
from ..flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash, is_unset
from ..flow.flow import NoReadableSource, written_path_problems
from ..flow.fpga import FPGA
from ..flow.io import declared_inputs, declared_outputs, selected_types
from ..flow.synth import PhysicalClock
from .bindings import (
    BindingLayer,
    InputBinding,
    NodeKey,
    check_chain_collisions,
    input_origins,
    node_bindings,
    node_identity,
)
from .chains import FlowRequest, fitting_outputs
from .related_inputs import Node, check_related_inputs
from .settings_layers import (
    _nested_model,
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
)
#: Shared settings that are one value, compared and propagated whole -- never split into
#: leaves and merged key by key with another node's: a platform is a model.
INDIVISIBLE_SETTINGS = ("platform",)
ORIGIN_NAMES = ("the project file", "the design file", "the command line")
log = logging.getLogger(__name__)


BINDING_ORIGINS = {
    "chain": "chain",
    "file": "saved binding",
    "cli": "command line",
    "api": "API",
}


@dataclass(frozen=True)
class ResolvedReference:
    """One producer node of the plan and the output key it supplies."""

    node: str
    output: str


@dataclass(frozen=True)
class ResolvedInput:
    """An input's ordered sources, its ordered producer references, or permitted absence.

    `producer`/`output` are the first reference's (the only one of a scalar input).
    `binding_origin` is None for design sources and default producers, else where the explicit
    binding came from (`BINDING_ORIGINS`); with `binding_location` and `overridden` it explains
    the edge and is no part of a run's identity.
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

    def describe(self) -> str:
        explained = ""
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
    """The captured request, including delivery destinations omitted from run hashes."""

    design_hash: str
    design_root: Path
    runner_cwd: Path
    run_root: Path
    hashed_run_dirs: bool
    debug: bool
    input_settings: Mapping[str, Any]
    #: the design's selected target: no part of any identity, but where every node runs
    #: (`<design>/<target>/<flow>`), so a plan belongs to the target it was made for
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
    #: the settings-only hash and the ordered input origins `flowrun_hash`, the node's
    #: identity, is made of (`bindings.node_identity`): kept so the launcher can recompute it
    settings_hash: str = ""
    origins: tuple[tuple[str, Any], ...] = ()

    @property
    def settings(self) -> Flow.Settings:
        return self._settings.model_copy(deep=True)

    @property
    def node_key(self) -> NodeKey:
        """The node's identity in its plan; `name` is its label."""
        return self.key or NodeKey(self.name)


@dataclass(frozen=True)
class Plan:
    """Producers before consumers, with the requested node last."""

    requested: str
    nodes: tuple[PlanNode, ...]
    context: PlanContext
    #: the request as given (a chain's elements) and its binding layers: explanation and
    #: validation data, immutable like the rest
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
    """Check final settings and source support without constructing a flow or probing tools.
    `unreached` explains a missing required setting (`Flow.check_required_settings`)."""
    problems = written_path_problems(settings)
    if problems:
        raise FlowSettingsError(
            [(key, message, None, "value_error") for key, message in problems], flow_cls.Settings
        )
    flow_cls.check_required_settings(settings, unreached)
    flow_cls.check_design_supported(design)
    flow_cls.check_settings_supported(settings)


def _explicit(value: Any) -> Any:
    """Recover model input without making validator-derived/default fields contributions.

    Descend even into unset parent fields: callers can edit a default-created dependency.
    FPGA part parsing adds fields to fields_set; retain only differences from that parsing.
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
    """Merge as `settings_layers` does, retaining single-clock spellings until their lower layer is known."""
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
    singular: dict[str, Any] = {
        key: values[key] for key in ("clock", "clock_period") if key in values
    }
    if len(singular) == 1 and "clocks" not in values and "clocks" in model.model_fields:
        raw = singular.get("clock")
        if raw is None or isinstance(raw, Mapping):
            result[prefix] = deepcopy(singular)
    for key, value in values.items():
        info = model.model_fields.get(key)
        child = _nested_model(info.annotation) if info else None
        if child and isinstance(value, dict):
            result.update(_clock_inputs(value, child, (*prefix, key)))
    return result


def _fpga_shorthands(values: dict[str, Any]) -> None:
    for key, value in values.items():
        if key == "fpga" and isinstance(value, str):
            values[key] = {"part": value}
        elif isinstance(value, dict):
            _fpga_shorthands(value)


def _located(raw: Mapping[str, Any], cls: type[Flow], label: str, kind: str) -> _Located:
    values = merge_layers(_explicit(raw), settings_cls=cls.Settings)
    _fpga_shorthands(values)
    locations = {path: _Location(label, kind) for path in _leaves(values)}
    # `settings_layers` synthesizes a name when a singular spelling creates a clock. It is not caller input.
    original = merge_layers(_explicit(raw))
    singular = original.get("clock", {})
    if "clock_period" in original or (
        "clock" in original and isinstance(singular, Mapping) and not singular.get("name")
    ):
        locations = {
            path: loc
            for path, loc in locations.items()
            if not (path[0] == "clocks" and path[-1] == "name")
        }
    return _Located(values, locations, _clock_inputs(original, cls.Settings))


def _compose(cls: type[Flow], sections: Mapping[str, Any], origin: str, kind: str) -> _Located:
    own = _located(sections.get(cls.name, {}), cls, f"[flows.{cls.name}] in {origin}", kind)
    # Attach the original location to each leaf, before composition loses that route.
    own.locations = {
        path: _Location(f"{loc.label} ({'.'.join(path)})", loc.kind)
        for path, loc in own.locations.items()
    }
    located = _overlay(_Located(), own, cls)
    # `settings_layers` owns ordinary precedence and aliases; this traversal carries the parallel locations.
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
    #: the distinct producers this node reads, in first-use order
    children: list[_Request] = field(default_factory=list)
    #: input name -> its ordered (producer, output key) references
    producers: dict[str, list[tuple[_Request, str]]] = field(default_factory=dict)
    #: every output of this node a consumer reads: the union over the whole graph
    needed: set[str] = field(default_factory=set)
    switched: tuple[str, ...] = ()
    settings: Flow.Settings | None = None
    key: NodeKey | None = None

    @property
    def label(self) -> str:
        return self.key.label if self.key else self.cls.name


def _error(cls: type[Flow], leaf: str, message: str) -> FlowSettingsError:
    return FlowSettingsError([(leaf, message, None, "shared_setting_conflict")], cls.Settings)


def _given_at(location: _Location | None) -> str:
    """` at <where it was given>`, or nothing for a value that has no recorded origin."""
    return f" at {location.label}" if location else ""


def _components(requests: list[_Request], shared: str) -> list[list[_Request]]:
    """Components include only edges whose endpoints both expose this shared field."""
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


#: one node's contribution to a shared setting at one leaf: the value it propagates, the key
#: it is compared by (the same value for every leaf but `platform` and `corner`), and
#: where it was given
_Leaf = tuple[Any, Any, "_Location"]


def _platform_key(platform: Any) -> Any:
    """What a platform is compared by: its validated model, every path in it written relative
    to its resolved root (one outside the root as itself), together with that root. Two
    spellings of one platform -- a bundled name and the path to its `config.toml` -- compare
    equal; two models that share a name and a root but differ anywhere do not."""
    if platform is None:
        return None
    root = Path(platform.root_dir).resolve()

    def located(value: Any) -> Any:
        if isinstance(value, Path):
            path = (value if value.is_absolute() else root / value).resolve()
            return (
                ("root", path.relative_to(root).as_posix())
                if path.is_relative_to(root)
                else ("path", str(path))
            )
        if isinstance(value, Mapping):
            return {key: located(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [located(item) for item in value]
        return value

    return (str(root), located(platform.model_dump(exclude={"root_dir"})))


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
    if shared in INDIVISIBLE_SETTINGS:
        # one value, wherever its parts were given: located at the most specific of them
        rank = {"file": 0, "cli": 1, "api": 2}
        given = [loc for path, loc in request.raw.locations.items() if path and path[0] == shared]
        value = request.raw.values.get(shared)
        if not given or value is None or value == {}:
            return result
        loc = max(given, key=lambda location: rank[location.kind])
        try:
            model = settings_in_context(request.cls, {shared: value}, **context)
            key = _platform_key(getattr(model, shared))
        except (ValueError, FlowSettingsError) as error:
            raise _error(request.cls, shared, f"{error} at {loc.label}") from error
        result[(shared,)] = (value, key, loc)
        return result
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
                    clock = PhysicalClock.model_validate({"period": 1, path[-1]: value})
                    value = getattr(clock, path[-1])
            elif shared == "board":
                # The board and database may come from different nodes. Lookup is valid
                # only after they agree; here check the same strict name syntax as the field.
                if value is not None and not isinstance(value, str):
                    raise ValueError("board must be a string or None")
            elif shared in ("prjxray_db", "dont_use_cells"):
                model = settings_in_context(request.cls, {shared: value}, **context)
                value = getattr(model, shared)
                if shared == "prjxray_db" and value is not None:
                    value = (context["design_root"] / value).resolve()
            elif shared == "custom_boards_file":
                board = WithFpgaBoardSettings.from_input({shared: value}, **context)
                value = getattr(board, shared)
        except ValueError as error:
            raise _error(request.cls, ".".join(path), f"{error} at {loc.label}") from error
        if shared == "corner" and isinstance(value, list):
            # propagated as given; compared by the corner it selects, the first of a list
            key = value[0] if value else None
        else:
            key = value
        result[target] = (value, key, loc)
    return result


def _nonshared_input(cls: type[Flow], values: Mapping[str, Any]) -> dict[str, Any]:
    """Syntax validation must not erase malformed nested values during reconciliation."""
    ordinary = deepcopy(dict(values))
    for shared in SHARED_SETTINGS:
        if shared in cls.Settings.model_fields:
            ordinary.pop(shared, None)
    if "clocks" in cls.Settings.model_fields:
        ordinary.pop("clock", None)
        ordinary.pop("clock_period", None)
    return ordinary


def _shared_locations(
    raw: _Located, cls: type[Flow], context: dict[str, Any]
) -> dict[tuple[str, ...], _Leaf]:
    """Compare composed mappings with validated models without inventing API overrides."""
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


#: where each agreed leaf of each node was given, by the node's `id`: every node of a group holds
#: the winning contribution's value, so the winner's location is that value's origin
_Given = dict[int, dict[tuple[str, ...], "_Location"]]


def _agree(
    requests: list[_Request], shared: str, context: dict[str, Any], *, provisional: bool = False
) -> _Given:
    """Agree `shared` along the edges of `requests`, and say where each agreed leaf was given."""
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
            # the winner's value is propagated exactly as given; contributions agree by key
            first, value, key, location = winners[0]
            for other, alternative, other_key, other_location in winners[1:]:
                if key != other_key and not provisional:
                    leaf = ".".join(path)
                    raise _error(
                        first.cls,
                        leaf,
                        f"{first.cls.name} {leaf}={value!r} at {location.label} disagrees with "
                        f"{other.cls.name} {leaf}={alternative!r} at {other_location.label}",
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
    """For each required setting `request` lacks, the sections that give it without reaching
    `request`: a section of a flow that is not part of this run, or of a flow that shares no
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
                        f"The `{key}` in [flows.{flow}] in {label} does not reach "
                        f"{request.cls.name}: {why}"
                    )
        if notes:
            found[name] = ". ".join(notes)
    return found


def _replaceable_by_a_source(producer: _Request, requests: list[_Request]) -> str:
    """`; a <Type> source would supply <consumer>'s <input> and skip <producer>`, when that one
    source takes `producer` out of the plan: exactly one input of the plan reaches it, and by
    default, which a typed source of a type the input takes replaces. Otherwise the plan keeps
    `producer` whatever source the design adds -- an input bound to it (a chain, a saved binding,
    the command line or the API) keeps it, and so does any other input that reaches it -- and the
    refusal adds nothing."""
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
    kinds = "/".join(kind.name for kind in selected_types(consumer.cls, consumer.settings, name))
    return f"; a {kinds} source would supply {consumer.label}'s {name} and skip {producer.label}"


def _sections(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Use the flow-name checks of `settings_layers` while preserving per-layer single-clock input syntax."""
    normalized = merge_flow_sections(values, flow_class_for=registered_flow)
    for name, raw in (values or {}).items():
        cls = registered_flow(name)
        normalized[cls.name if cls else name] = merge_layers(_explicit(raw))
    return normalized


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
) -> Plan:
    """Resolve the graph (explicit bindings, then sources, then default producers), agree
    settings, union every demand on each producer, enable outputs, validate and freeze."""
    context = dict(design_root=design.root_path, runner_cwd=runner_cwd)
    if isinstance(settings, Flow.Settings) and settings.context:
        context = {key: settings.context.get(key) or value for key, value in context.items()}
    layers = [
        (label, _sections(values), "cli" if "command line" in label else "file")
        for label, values in origins
    ]
    if not layers:
        layers.append(("the supplied flow sections", _sections(sections), "file"))
    cli_sections = _sections(command_line) if command_line else {}
    if cli_sections:
        layers.append(("the command line", cli_sections, "cli"))
    if api_overrides:
        layers.append(("the API", _sections(api_overrides), "api"))

    root_raw = _Located()
    for label, values, kind in layers:
        root_raw = _overlay(root_raw, _compose(flow_cls, values, label, kind), flow_cls)
    # settings is normally already composed by `settings_layers`. Preserve locations for identical leaves;
    # only additional caller edits form a final direct-API contribution.
    supplied = _located(
        _explicit(settings) if settings is not None else {}, flow_cls, "the API", "api"
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
        # The single-clock normalization puts a generated name in model_fields_set too.
        # A name equal to its mapping key carries no independent naming contribution.
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
            # Models store a frequency as period; locate that original contribution too.
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
        """The output key one explicit reference selects, by declared kinds and cardinality.
        Target-selected types are judged once the final settings agree."""
        outputs = declared_outputs(producer)
        if not outputs:
            raise FlowSettingsException(
                f"{where}: {producer.name} declares no outputs, so it cannot supply "
                f"{cls.name}.{declaration.name}"
            )
        made = ", ".join(f"{out.name} ({kinds(out.types)})" for out in outputs.values())
        # The one edge predicate, shared with chain adjacency (`chains.fitting_outputs`).
        fitting, many = fitting_outputs(producer, declaration, output=wanted)
        if wanted is not None:
            if wanted in many:
                raise FlowSettingsException(
                    f"{where}: {cls.name}.{declaration.name} takes one file, but "
                    f"{producer.name}.{wanted} produces many"
                )
            if not fitting:
                raise FlowSettingsException(
                    f"{where}: {cls.name}.{declaration.name} takes {kinds(declaration.types)}; "
                    f"{producer.name}.{wanted} makes {kinds(outputs[wanted].types)}. "
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
                f"take ({kinds(declaration.types)}); it makes: {made}"
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
            matching = [out for out in outputs.values() if set(out.types) & set(declaration.types)]
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
        """The nodes the requested flow still reaches, each once, with their demands."""
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
                    # The device leaves rank as the node's own board does (one it received
                    # along an edge ranks as a file's), and are located where the board that
                    # won was written.
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
        # A bounded selection pass on copies: a consumer's target breaks provisional ties.
        # Strict agreement below applies only to edges that survive source displacement.
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
                old = next(item for item in request.inputs if item.name == declaration.name)
                if old.binding_origin is not None:
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
                # a default producer a source or a binding displaced, or one of its producers
                log.info("%s is not part of this run: its settings are unused", unused_name)
    given = agree_targets(requests)

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
        try:
            check_launchable(
                request.cls, request.settings, design, _unreached(request, requests, layers)
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
            types = selected_types(request.cls, request.settings, selected.name)
            sources = tuple(source.path for source in design.rtl.sources if source.type in types)
            if selected.origin == "source" and sources != selected.sources:
                raise FlowSettingsException(
                    f"{where} source selection changed after target agreement"
                )
            # An explicit binding stands whatever sources the design lists for that input.
            if selected.origin != "source" and sources and selected.binding_origin is None:
                raise FlowSettingsException(
                    f"{where} source selection changed after target agreement"
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
                            f"for the selected target (takes {kinds(types) or 'nothing'}, "
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
            design.parts_hash(DESIGN_PARTS),
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
