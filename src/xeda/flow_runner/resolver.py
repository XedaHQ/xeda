"""Pure default-graph resolution, with origin-preserving shared-setting agreement.

Composition precedes agreement: a node keeps P1's origin-first precedence, while explicit
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
from ..design import Design
from ..flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash
from ..flow.flow import written_path_problems
from ..flow.fpga import FPGA
from ..flow.io import declared_inputs, declared_outputs, is_declared, selected_types
from ..flow.synth import PhysicalClock
from ..utils import semantic_hash
from .settings_layers import (
    _flow_of,
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
from .trace import as_recorded, settings_difference

SHARED_SETTINGS = ("fpga", "board", "custom_boards_file", "clocks", "prjxray_db")
ORIGIN_NAMES = ("the project file", "the design file", "the command line")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedInput:
    """An input's ordered sources, default producer, or permitted absence."""

    name: str
    origin: Literal["source", "producer", "none"]
    producer: str | None = None
    output: str | None = None
    sources: tuple[Path, ...] = ()

    def describe(self) -> str:
        if self.origin == "producer":
            return f"{self.name} <- {self.producer}.{self.output}"
        if self.origin == "source":
            return f"{self.name} <- source " + ", ".join(map(str, self.sources))
        return f"{self.name} <- none"


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


@dataclass(frozen=True)
class PlanNode:
    """A final effective configuration. Execution and inspection receive private copies."""

    name: str
    flow_class: type[Flow]
    _settings: Flow.Settings = field(repr=False)
    flowrun_hash: str
    run_path: Path
    declared: bool
    inputs: tuple[ResolvedInput, ...] = ()
    switched_on: tuple[str, ...] = ()

    @property
    def settings(self) -> Flow.Settings:
        return self._settings.model_copy(deep=True)


@dataclass(frozen=True)
class Plan:
    """Producers before consumers, with the requested node last."""

    requested: str
    nodes: tuple[PlanNode, ...]
    context: PlanContext

    def __contains__(self, name: str) -> bool:
        return any(node.name == name for node in self.nodes)

    def node(self, name: str) -> PlanNode:
        return next(node for node in self.nodes if node.name == name)


def check_launchable(flow_cls: type[Flow], settings: Flow.Settings, design: Design) -> None:
    """Check final settings and source support without constructing a flow or probing tools."""
    problems = written_path_problems(settings)
    if problems:
        raise FlowSettingsError(
            [(key, message, None, "value_error") for key, message in problems], flow_cls.Settings
        )
    flow_cls.check_required_settings(settings)
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

    def child(self, key: str) -> _Located:
        values = self.values.get(key, {})
        return _Located(
            deepcopy(dict(values)) if isinstance(values, Mapping) else {},
            {path[1:]: loc for path, loc in self.locations.items() if path[0] == key},
            {
                path[1:]: value
                for path, value in self.clock_inputs.items()
                if path and path[0] == key
            },
        )


def _at(values: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    for key in path:
        values = values.setdefault(key, {})
    return values


def _overlay(base: _Located, layer: _Located, cls: type[Flow]) -> _Located:
    """Merge with P1, retaining single-clock spellings until their lower layer is known."""
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
    # P1 synthesizes a name when a singular spelling creates a clock. It is not caller input.
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
    result = _Located()
    for key in cls.Settings.dependency_settings:
        dependency = _flow_of(cls.Settings._dependency_settings_class(key))
        if dependency is not None:
            child = _compose(dependency, sections, origin, kind)
            result.values[key] = child.values
            result.locations.update({(key, *path): loc for path, loc in child.locations.items()})
            result.clock_inputs.update(
                {(key, *path): value for path, value in child.clock_inputs.items()}
            )
    own = _located(sections.get(cls.name, {}), cls, f"[flows.{cls.name}] in {origin}", kind)
    # Attach the original nested location to each leaf, before composition loses that route.
    own.locations = {
        path: _Location(f"{loc.label} ({'.'.join(path)})", loc.kind)
        for path, loc in own.locations.items()
    }
    located = _overlay(result, own, cls)
    # P1 owns ordinary precedence and aliases; this traversal carries the parallel locations.
    composed = compose_flow_settings(cls, [sections])
    located.values = _located(composed, cls, origin, kind).values
    return located


@dataclass
class _Request:
    cls: type[Flow]
    raw: _Located
    requester: str
    inputs: list[ResolvedInput] = field(default_factory=list)
    children: list[tuple[str | None, _Request]] = field(default_factory=list)
    producers: dict[str, _Request] = field(default_factory=dict)
    needed: set[str] = field(default_factory=set)
    switched: tuple[str, ...] = ()
    settings: Flow.Settings | None = None


def _error(cls: type[Flow], leaf: str, message: str) -> FlowSettingsError:
    return FlowSettingsError([(leaf, message, None, "shared_setting_conflict")], cls.Settings)


def _components(requests: list[_Request], shared: str) -> list[list[_Request]]:
    """Components include only edges whose endpoints both expose this shared field."""
    neighbors: dict[int, list[_Request]] = {id(r): [] for r in requests}
    for parent in requests:
        for _key, child in parent.children:
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


def _normalized_leaves(
    request: _Request, shared: str, context: dict[str, Any]
) -> dict[tuple[str, ...], tuple[Any, _Location]]:
    result = {}
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
                    clock = PhysicalClock.model_validate({"period": 1, path[-1]: value})
                    value = getattr(clock, path[-1])
            elif shared == "board":
                # The board and database may come from different nodes. Lookup is valid
                # only after they agree; here check the same strict name syntax as the field.
                if value is not None and not isinstance(value, str):
                    raise ValueError("board must be a string or None")
            elif shared == "prjxray_db":
                model = settings_in_context(request.cls, {shared: value}, **context)
                value = getattr(model, shared)
                if value is not None:
                    value = (context["design_root"] / value).resolve()
            elif shared == "custom_boards_file":
                board = WithFpgaBoardSettings.from_input({shared: value}, **context)
                value = getattr(board, shared)
        except ValueError as error:
            raise _error(request.cls, ".".join(path), f"{error} at {loc.label}") from error
        result[target] = (value, loc)
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
    for key in cls.Settings.dependency_settings:
        dependency = _flow_of(cls.Settings._dependency_settings_class(key))
        if dependency and isinstance(ordinary.get(key), Mapping):
            ordinary[key] = _nonshared_input(dependency, ordinary[key])
    return ordinary


def _shared_locations(
    raw: _Located, cls: type[Flow], context: dict[str, Any]
) -> dict[tuple[str, ...], tuple[Any, _Location]]:
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
    for key in cls.Settings.dependency_settings:
        dependency = _flow_of(cls.Settings._dependency_settings_class(key))
        if dependency:
            result.update(
                {
                    (key, *path): leaf
                    for path, leaf in _shared_locations(raw.child(key), dependency, context).items()
                }
            )
    return result


def _agree(
    requests: list[_Request], shared: str, context: dict[str, Any], *, provisional: bool = False
) -> None:
    for group in _components(requests, shared):
        candidates: dict[tuple[str, ...], list[tuple[_Request, Any, _Location]]] = {}
        for request in group:
            for path, (value, loc) in _normalized_leaves(request, shared, context).items():
                candidates.setdefault(path, []).append((request, value, loc))
        agreed: dict[str, Any] = {}
        for path, contributions in candidates.items():
            rank = {"file": 0, "cli": 1, "api": 2}
            highest = max(rank[loc.kind] for _r, _v, loc in contributions)
            winners = [c for c in contributions if rank[c[2].kind] == highest]
            first, value, location = winners[0]
            for other, alternative, other_location in winners[1:]:
                if value != alternative and not provisional:
                    leaf = ".".join(path)
                    raise _error(
                        first.cls,
                        leaf,
                        f"{first.cls.name} {leaf}={value!r} at {location.label} disagrees with "
                        f"{other.cls.name} {leaf}={alternative!r} at {other_location.label}",
                    )
            _put(agreed, path[1:], value) if len(path) > 1 else agreed.update({shared: value})
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


def _sections(values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Use P1's flow-name checks while preserving per-layer single-clock input syntax."""
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
    run_path: Callable[[str, str, str], Path],
    origins: Sequence[tuple[str, Mapping[str, Any]]] = (),
    command_line: Mapping[str, Mapping[str, Any]] | None = None,
    api_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    debug: bool = False,
) -> Plan:
    """Resolve current default declarations, agree settings, enable outputs, validate and freeze."""
    context = dict(design_root=design.root_path, runner_cwd=runner_cwd)
    if isinstance(settings, Flow.Settings) and settings.context:
        context = {key: settings.context.get(key) or value for key, value in context.items()}
    layers = [
        (label, _sections(values), "cli" if "command line" in label else "file")
        for label, values in origins
    ]
    if not layers:
        layers.append(("the supplied flow sections", _sections(sections), "file"))
    if command_line:
        cli_sections = _sections(command_line)
        check_run_flows(cli_sections, flow_cls)
        layers.append(("the command line", cli_sections, "cli"))
    if api_overrides:
        layers.append(("the API", _sections(api_overrides), "api"))

    root_raw = _Located()
    for label, values, kind in layers:
        root_raw = _overlay(root_raw, _compose(flow_cls, values, label, kind), flow_cls)
    # settings is normally already composed by P1. Preserve locations for identical leaves;
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
        # P1's single-clock normalization puts a generated name in model_fields_set too.
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
    for path, (value, _location) in normalized_supplied.items():
        if path in normalized_known and value == normalized_known[path][0]:
            # Models store a frequency as period; locate that original contribution too.
            supplied.locations[path] = normalized_known[path][1]
            if path[-1] == "period":
                supplied.locations[(*path[:-1], "freq")] = normalized_known[path][1]
    root_raw = _overlay(root_raw, supplied, flow_cls)
    requests: list[_Request] = []
    order: list[_Request] = []
    visiting: list[str] = []

    def discover(cls: type[Flow], raw: _Located, requester: str) -> _Request:
        if cls.name in visiting:
            raise FlowSettingsException(
                "Flow declaration cycle: " + " -> ".join([*visiting, cls.name])
            )
        visiting.append(cls.name)
        request = _Request(cls, raw, requester)
        requests.append(request)
        for declaration in declared_inputs(cls).values():
            sources = tuple(
                source.path for source in design.rtl.sources if source.type in declaration.types
            )
            specialized = getattr(cls.input_types, "__func__") is not getattr(
                Flow.input_types, "__func__"
            )
            if sources and not specialized:
                if declaration.cardinality != "many" and len(sources) != 1:
                    kinds = "/".join(t.name for t in declaration.types)
                    raise FlowSettingsException(
                        f"{cls.name}.{declaration.name} takes one {kinds} file; found "
                        + ", ".join(map(str, sources))
                    )
                request.inputs.append(ResolvedInput(declaration.name, "source", sources=sources))
                if declaration.producer:
                    log.info(
                        "%s.%s uses design sources; settings for displaced producer %s are unused",
                        cls.name,
                        declaration.name,
                        declaration.producer,
                    )
                continue
            if declaration.producer is None:
                if declaration.required and not specialized:
                    raise FlowSettingsException(
                        f"{cls.name} needs its input `{declaration.name}` ({', '.join(t.name for t in declaration.types)}); list a source or declare a producer"
                    )
                request.inputs.append(ResolvedInput(declaration.name, "none"))
                continue
            producer = registered_flow(declaration.producer)
            if producer is None:
                raise FlowSettingsException(
                    f"{cls.name}.{declaration.name} names unknown producer {declaration.producer!r}"
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
                    f"{cls.name}.{declaration.name}: {producer.name} has no unambiguous compatible output {declaration.output or ''!r}"
                )
            if output.cardinality == "many" and declaration.cardinality != "many":
                raise FlowSettingsException(
                    f"{cls.name}.{declaration.name} takes one file, but {producer.name}.{output.name} produces many"
                )
            key = next(
                (
                    key
                    for key in cls.Settings.dependency_settings
                    if cls.Settings._dependency_settings_class(key) is producer.Settings
                ),
                None,
            )
            if key:
                # Already composed once, including producer sections and direct nested edits.
                child_raw = raw.child(key)
                # A consumer can refine its producer's defaults (e.g. keeping source
                # attributes for placement). Retain nonshared defaults below all input
                # origins, without turning shared defaults into explicit contributions.
                default = cls.Settings.model_fields[key].get_default(call_default_factory=True)
                defaults = (
                    {
                        name: value
                        for name, value in _explicit(default).items()
                        if name not in SHARED_SETTINGS
                    }
                    if isinstance(default, Flow.Settings)
                    else {}
                )
                child_raw.values = merge_layers(
                    defaults, child_raw.values, settings_cls=producer.Settings
                )
            else:
                child_raw = _Located()
                for label, values, kind in layers:
                    child_raw = _overlay(
                        child_raw, _compose(producer, values, label, kind), producer
                    )
            child = discover(producer, child_raw, f"{cls.name}.{declaration.name}")
            child.needed.add(output.name)
            request.producers[declaration.name] = child
            request.children.append((key, child))
            request.inputs.append(
                ResolvedInput(declaration.name, "producer", producer.name, output.name)
            )
        visiting.pop()
        order.append(request)
        return request

    root = discover(flow_cls, root_raw, flow_cls.name)

    def agree_targets(candidates: list[_Request], *, provisional: bool = False) -> None:
        # Board/database agreement precedes expansion into explicit FPGA contributions.
        for shared in ("board", "custom_boards_file"):
            _agree(candidates, shared, context, provisional=provisional)
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
                    location = request.raw.locations.get(("board",), _Location("the shared board"))
                    fpga = _explicit(board.fpga)
                    request.raw.values["fpga"] = merge_layers(fpga, request.raw.values.get("fpga"))
                    for path in _leaves(fpga, ("fpga",)):
                        request.raw.locations.setdefault(path, location)
        for shared in SHARED_SETTINGS:
            if shared not in ("board", "custom_boards_file") and (
                not provisional or shared == "fpga"
            ):
                _agree(candidates, shared, context, provisional=provisional)

    if any(
        getattr(r.cls.input_types, "__func__") is not getattr(Flow.input_types, "__func__")
        for r in requests
    ):
        # A bounded selection pass on copies: a consumer's target breaks provisional ties.
        # Strict agreement below applies only to edges that survive source displacement.
        proposals = deepcopy(requests)
        agree_targets(proposals, provisional=True)
        for request, proposal in zip(requests, proposals):
            values = _nonshared_input(proposal.cls, proposal.raw.values)
            for shared in ("fpga", "board", "custom_boards_file"):
                if shared in proposal.raw.values:
                    values[shared] = proposal.raw.values[shared]
            settings = settings_in_context(proposal.cls, values, **context)
            bindings = []
            for declaration in declared_inputs(request.cls).values():
                types = selected_types(request.cls, settings, declaration.name)
                sources = tuple(
                    source.path for source in design.rtl.sources if source.type in types
                )
                old = next(item for item in request.inputs if item.name == declaration.name)
                if sources:
                    bindings.append(ResolvedInput(declaration.name, "source", sources=sources))
                    child = request.producers.pop(declaration.name, None)
                    if child is not None:
                        request.children = [
                            (key, edge) for key, edge in request.children if edge is not child
                        ]
                        log.info(
                            "%s.%s uses design sources; settings for displaced producer %s are unused",
                            request.cls.name,
                            declaration.name,
                            child.cls.name,
                        )
                else:
                    bindings.append(
                        old if old.origin == "producer" else ResolvedInput(declaration.name, "none")
                    )
            request.inputs = bindings
        requests, order = [], []

        def retain(request: _Request) -> None:
            requests.append(request)
            request.needed.clear()
            for _key, child in request.children:
                retain(child)
            order.append(request)

        retain(root)
        for request in requests:
            for selected in request.inputs:
                if selected.origin == "producer":
                    assert selected.output is not None
                    request.producers[selected.name].needed.add(selected.output)

    active = {request.cls.name for request in requests}
    for name, cls in transitive_dependencies(flow_cls).items():
        if name not in active:
            unused = compose_flow_settings(cls, [values for _label, values, _kind in layers])
            settings_in_context(cls, unused, **context)  # syntax only, no launch requirements
    agree_targets(requests)

    required: dict[str, set[str]] = {}
    for request in requests:
        required.setdefault(request.cls.name, set()).update(request.needed)
    # Shared agreement may complete a partial nested clock; reconcile raw children before
    # constructing parent models, whose validators otherwise reject that partial input.
    for request in order:
        for key, child in request.children:
            if key:
                request.raw.values[key] = deepcopy(child.raw.values)
    for request in requests:
        try:
            request.settings = settings_in_context(request.cls, request.raw.values, **context)
        except FlowSettingsError as error:
            suggest_dependency_node(request.cls, error)
            raise
        switched = []
        for name in declared_outputs(request.cls):
            if name in required[request.cls.name]:
                before = request.settings.model_dump()
                try:
                    request.cls.enable_output(request.settings, name)
                except ValueError as error:
                    raise FlowSettingsException(
                        f"{request.cls.name}.{name} is required by a consumer: {error}"
                    ) from error
                if request.settings.model_dump() != before:
                    switched.append(name)
        request.switched = tuple(switched)
        # Assignment validators may change other fields; validate the final snapshot together.
        request.settings = settings_in_context(
            request.cls, request.settings.model_dump(), **context
        )
    switched_by_node: dict[str, set[str]] = {}
    for request in requests:
        switched_by_node.setdefault(request.cls.name, set()).update(request.switched)
    for request in requests:
        request.switched = tuple(
            name
            for name in declared_outputs(request.cls)
            if name in switched_by_node[request.cls.name]
        )
    assert root.settings is not None
    if debug:
        root.settings.debug = True
    for request in requests:
        assert request.settings is not None
        for _key, child in request.children:
            assert child.settings is not None
            carry_diagnostics(child.settings, request.settings)
    # Reconcile nested models bottom-up before parent hashing and required-setting checks.
    for request in order:
        assert request.settings is not None
        for key, child in request.children:
            if key:
                setattr(request.settings, key, child.settings)
        if isinstance(request.settings, WithFpgaBoardSettings) and request.settings.board:
            board_fpga = request.settings._board_fpga(request.settings.board_data())
            if board_fpga and request.settings.fpga:
                for key, value in _explicit(board_fpga).items():
                    if value is not None and getattr(request.settings.fpga, key) != value:
                        raise _error(
                            request.cls,
                            f"fpga.{key}",
                            f"board {request.settings.board!r} and fpga.{key} disagree",
                        )
    for request in [root, *(r for r in requests if r is not root)]:
        assert request.settings is not None
        check_launchable(request.cls, request.settings, design)

    # The final settings select the very same sources and producer formats as discovery.
    for request in requests:
        assert request.settings is not None
        for binding in request.inputs:
            declaration = declared_inputs(request.cls)[binding.name]
            types = selected_types(request.cls, request.settings, binding.name)
            sources = tuple(source.path for source in design.rtl.sources if source.type in types)
            if binding.origin == "source" and sources != binding.sources:
                raise FlowSettingsException(
                    f"{request.cls.name}.{binding.name} source selection changed after target agreement"
                )
            if binding.origin != "source" and sources:
                raise FlowSettingsException(
                    f"{request.cls.name}.{binding.name} source selection changed after target agreement"
                )
            if binding.origin == "none" and declaration.required:
                raise FlowSettingsException(
                    f"{request.cls.name} needs its input `{binding.name}` ({', '.join(t.name for t in types)}); list a source or declare a producer"
                )
            if (
                binding.origin == "source"
                and declaration.cardinality != "many"
                and len(sources) != 1
            ):
                raise FlowSettingsException(
                    f"{request.cls.name}.{binding.name} takes one {'/'.join(t.name for t in types)} file; found "
                    + ", ".join(map(str, sources))
                )
            if binding.origin == "producer":
                child = request.producers[binding.name]
                assert child.settings is not None and binding.output is not None
                produced = selected_types(child.cls, child.settings, binding.output, output=True)
                if not produced or not set(produced) <= set(types):
                    raise FlowSettingsException(
                        f"{request.cls.name}.{binding.name}: {child.cls.name}.{binding.output} has no compatible output for the selected target"
                    )

    nodes: dict[str, PlanNode] = {}
    first_requests: dict[str, str] = {}
    for request in order:
        assert request.settings is not None
        name = request.cls.name
        identity = flowrun_hash(name, request.settings, design.name)
        node = PlanNode(
            name,
            request.cls,
            request.settings.model_copy(deep=True),
            identity,
            run_path(design.name, name, identity),
            is_declared(request.cls),
            tuple(request.inputs),
            request.switched,
        )
        if name in nodes:
            previous = nodes[name]
            if as_recorded(previous.settings) != as_recorded(node.settings):
                difference = settings_difference(
                    as_recorded(previous.settings), as_recorded(node.settings)
                )
                raise _error(
                    request.cls,
                    difference,
                    f"{name} has incompatible requests from {first_requests[name]} and {request.requester}: {difference}",
                )
        else:
            nodes[name] = node
            first_requests[name] = request.requester
    return Plan(
        flow_cls.name,
        tuple(nodes.values()),
        PlanContext(
            semantic_hash(dict(rtl_hash=design.rtl_hash, tb_hash=design.tb_hash)),
            design.root_path,
            runner_cwd,
            run_root,
            hashed_run_dirs,
            debug,
            _freeze(as_recorded(root.settings)),
        ),
    )
