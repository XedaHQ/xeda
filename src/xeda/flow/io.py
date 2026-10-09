"""Declared inputs and outputs of a flow.

A flow declares its files the way it declares its settings: nested `Inputs` and `Outputs`
models, one field per input or output, each made with `In(...)` or `Out(...)`. The annotation is
the cardinality -- `Path` exactly one file, `Path | None` zero or one, `list[Path]` an ordered
list -- and `In`/`Out` give the accepted source types and, for an input, its default producer.
The launcher fills `flow.inputs` before the flow's freshness is judged (`init()` never reads
them), and records what the flow set in `flow.outputs` after a successful run
(`flow_runner.outputs`). A flow with any declaration is *declared*: the launcher launches its
producers, and its `init()` registers no dependency.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from ..dataclass import BaseModel, ConfigDict, Field
from ..design import SourceType

__all__ = [
    "FlowInputs",
    "FlowOutputs",
    "In",
    "InputDeclaration",
    "Out",
    "OutputDeclaration",
    "check_io_declarations",
    "declared_inputs",
    "declared_outputs",
    "output_enabled",
    "selected_types",
    "switch_on",
]

#: The `json_schema_extra` key a declared field carries its declaration under.
IO_MARKER = "x-xeda-io"


class FlowInputs(BaseModel):
    """A flow's declared inputs (`Flow.Inputs`): one `In(...)` field each."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class FlowOutputs(BaseModel):
    """A flow's declared outputs (`Flow.Outputs`): one `Out(...)` field each."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _type_names(types: SourceType | Sequence[SourceType]) -> list[str]:
    if not isinstance(types, SourceType) and (
        not isinstance(types, Sequence) or isinstance(types, (str, bytes))
    ):
        raise TypeError(f"a declaration requires SourceType members, not {types!r}")
    members = [types] if isinstance(types, SourceType) else list(types)
    if not members or not all(isinstance(member, SourceType) for member in members):
        raise TypeError(f"a declared input or output takes one or more SourceType, not {types!r}")
    return [member.name for member in members]


def In(
    types: SourceType | Sequence[SourceType],
    *,
    producer: str | None = None,
    output: str | None = None,
    optional: bool = False,
    same_producer_as: str | None = None,
    via: str | None = None,
    description: str,
) -> Any:
    """A declared input: the source types it accepts; `producer`, the flow (by canonical name)
    whose output feeds it when the design lists no source of those types, and `output`, which
    of that flow's outputs; `optional` lets a `list[Path]` input be empty.

    `same_producer_as` names another input of the flow that this one belongs with: the plan takes
    both from one producer node. An input that comes from the design's sources or is absent is not
    compared, because a file the user lists is the user's own choice. `via` moves the comparison one
    step: it names an input of the flow that makes the other input, and this input has to come
    from the producer of that input. `vivado_power` uses it for its checkpoint, which must come
    from the synthesis behind the netlist that the simulation of its `activity` read (the
    activity's own producer is a simulation, not a synthesis)."""
    marker: dict[str, Any] = {
        "kind": "input",
        "types": _type_names(types),
        "producer": producer,
        "output": output,
        "optional": optional,
    }
    if same_producer_as is not None:
        marker["same_producer_as"] = same_producer_as
    if via is not None:
        marker["via"] = via
    return Field(None, description=description, json_schema_extra={IO_MARKER: marker})


def Out(
    types: SourceType | Sequence[SourceType],
    *,
    enabled_by: str | None = None,
    description: str,
) -> Any:
    """A declared output: the source types it may have; `enabled_by`, the setting that switches
    an optional output on -- a true flag, or a path or text that is set."""
    marker: dict[str, Any] = {
        "kind": "output",
        "types": _type_names(types),
        "enabled_by": enabled_by,
    }
    return Field(None, description=description, json_schema_extra={IO_MARKER: marker})


@dataclass(frozen=True)
class InputDeclaration:
    """One declared input of a flow."""

    name: str
    types: tuple[SourceType, ...]
    #: "one" (`Path`), "optional" (`Path | None`) or "many" (`list[Path]`)
    cardinality: str
    producer: str | None
    output: str | None
    optional: bool
    description: str
    #: another input of the flow that this one has to come from the same producer as, and, for
    #: a transitive relation, the input of the flow making that one whose producer this is
    same_producer_as: str | None = None
    via: str | None = None

    @property
    def required(self) -> bool:
        """Whether the flow cannot run without it."""
        return self.cardinality == "one" or (self.cardinality == "many" and not self.optional)


@dataclass(frozen=True)
class OutputDeclaration:
    """One declared output of a flow."""

    name: str
    types: tuple[SourceType, ...]
    cardinality: str
    enabled_by: str | None
    description: str


def _cardinality(owner: str, annotation: Any) -> str:
    if annotation is Path:
        return "one"
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType) and set(args) == {Path, type(None)}:
        return "optional"
    if origin is list and args == (Path,):
        return "many"
    raise TypeError(f"{owner} is a `Path`, `Path | None` or `list[Path]`, not {annotation!r}")


def _markers(model: type[BaseModel], kind: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for name, info in model.model_fields.items():
        extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
        marker = extra.get(IO_MARKER)
        if not isinstance(marker, dict) or marker.get("kind") != kind:
            maker = "In" if kind == "input" else "Out"
            raise TypeError(f"{model.__qualname__}.{name} is not declared with {maker}(...)")
        if not info.description or not info.description.strip():
            raise TypeError(f"{model.__qualname__}.{name} requires a non-blank description")
        found[name] = marker
    return found


def declared_inputs(flow_cls: Any) -> dict[str, InputDeclaration]:
    """`flow_cls`'s declared inputs, by name, in declaration order."""
    model = flow_cls.Inputs
    return {
        name: InputDeclaration(
            name=name,
            types=tuple(SourceType[t] for t in marker["types"]),
            cardinality=_cardinality(
                f"{model.__qualname__}.{name}", model.model_fields[name].annotation
            ),
            producer=marker["producer"],
            output=marker["output"],
            optional=marker["optional"],
            description=model.model_fields[name].description or "",
            same_producer_as=marker.get("same_producer_as"),
            via=marker.get("via"),
        )
        for name, marker in _markers(model, "input").items()
    }


def declared_outputs(flow_cls: Any) -> dict[str, OutputDeclaration]:
    """`flow_cls`'s declared outputs, by name, in declaration order."""
    model = flow_cls.Outputs
    return {
        name: OutputDeclaration(
            name=name,
            types=tuple(SourceType[t] for t in marker["types"]),
            cardinality=_cardinality(
                f"{model.__qualname__}.{name}", model.model_fields[name].annotation
            ),
            enabled_by=marker["enabled_by"],
            description=model.model_fields[name].description or "",
        )
        for name, marker in _markers(model, "output").items()
    }


def selected_types(
    flow_cls: Any, settings: Any, name: str, *, output: bool = False
) -> tuple[SourceType, ...]:
    """Check that a pure specialization only narrows its static declaration vocabulary."""
    declarations = declared_outputs(flow_cls) if output else declared_inputs(flow_cls)
    hook = flow_cls.output_types if output else flow_cls.input_types
    types = hook(settings, name)
    if (
        not isinstance(types, tuple)
        or not all(isinstance(t, SourceType) for t in types)
        or not set(types) <= set(declarations[name].types)
    ):
        raise ValueError(f"{flow_cls.name}.{name} types must narrow the declared vocabulary")
    return types


def check_io_declarations(flow_cls: Any) -> None:
    """Refuse a malformed declaration when the flow class is defined (a `TypeError`): every
    field made with `In`/`Out`, annotated `Path`, `Path | None` or `list[Path]`, an output's
    `enabled_by` naming a setting of an output that may be absent, and an input's
    `same_producer_as` naming another input of the flow that declares none itself."""
    inputs = declared_inputs(flow_cls)
    for input_declaration in inputs.values():
        _check_relation(flow_cls, input_declaration, inputs)
    for input_declaration in inputs.values():
        if input_declaration.optional and input_declaration.cardinality != "many":
            raise TypeError(
                f"{flow_cls.__qualname__}'s input `{input_declaration.name}` uses optional=True, "
                "which is only allowed on a list[Path] input"
            )
        if input_declaration.output is not None and input_declaration.producer is None:
            raise TypeError(
                f"{flow_cls.__qualname__}'s input `{input_declaration.name}` names an output "
                "without a producer"
            )
    for declaration in declared_outputs(flow_cls).values():
        if declaration.enabled_by is None:
            continue
        if declaration.enabled_by not in flow_cls.Settings.model_fields:
            raise TypeError(
                f"{flow_cls.__qualname__}'s output `{declaration.name}` is switched on by "
                f"`{declaration.enabled_by}`, which names no setting of the flow"
            )
        if declaration.cardinality == "one":
            raise TypeError(
                f"{flow_cls.__qualname__}'s output `{declaration.name}` is switched on by "
                f"`{declaration.enabled_by}`, so it may be absent: annotate it `Path | None`"
            )


def _check_relation(
    flow_cls: Any, declaration: InputDeclaration, inputs: dict[str, InputDeclaration]
) -> None:
    """Refuse a malformed `same_producer_as`/`via` of `declaration` (a `TypeError`)."""
    where = f"{flow_cls.__qualname__}'s input `{declaration.name}`"
    anchor = declaration.same_producer_as
    if anchor is None:
        if declaration.via is not None:
            raise TypeError(f"{where} names `via` and needs `same_producer_as` too")
        return
    if anchor == declaration.name:
        raise TypeError(f"{where} is related to itself")
    if anchor not in inputs:
        raise TypeError(
            f"{where} is related to `{anchor}`, which names no input of the flow "
            f"(it declares: {', '.join(f'`{name}`' for name in inputs)})"
        )
    if inputs[anchor].same_producer_as is not None:
        raise TypeError(
            f"{where} is related to `{anchor}`, which declares one too: relate every input to "
            "one that declares none"
        )
    if declaration.via is not None and inputs[anchor].producer is None:
        raise TypeError(
            f"{where} follows `{anchor}` to its producer's `{declaration.via}`, and `{anchor}` "
            "names a default producer to look that input up in: give it a `producer`"
        )


def output_enabled(settings: Any, declaration: OutputDeclaration) -> bool:
    """Whether the setting that switches `declaration` on (`enabled_by`) is set in `settings`:
    a true flag, or a non-empty value. An output without one is always enabled."""
    if declaration.enabled_by is None:
        return True
    value = getattr(settings, declaration.enabled_by)
    if value is None or value is False:
        return False
    return not (isinstance(value, (str, list, tuple, set, frozenset, dict)) and len(value) == 0)


def switch_on(settings: Any, declaration: OutputDeclaration) -> None:
    """Switch `declaration` on in `settings` (Q6): its `enabled_by` flag to true, or any other
    setting to the field's default. A `ValueError` when that does not switch it on (a setting
    whose default is unset, and which is no flag)."""
    assert declaration.enabled_by is not None
    info = type(settings).model_fields[declaration.enabled_by]
    default = info.get_default(call_default_factory=True)
    annotation = info.annotation
    boolean = annotation is bool or (
        get_origin(annotation) in (Union, UnionType)
        and set(get_args(annotation)) == {bool, type(None)}
    )
    value = True if boolean else deepcopy(default)
    if value is None:
        raise ValueError(f"give `{declaration.enabled_by}` a value")
    try:
        setattr(settings, declaration.enabled_by, value)
    except ValueError as e:  # pydantic's ValidationError is one
        raise ValueError(f"give `{declaration.enabled_by}` a value") from e
    if not output_enabled(settings, declaration):
        raise ValueError(f"give `{declaration.enabled_by}` a value")
