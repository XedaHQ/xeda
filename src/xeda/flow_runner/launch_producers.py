"""The producers stage of a launch: run what the plan says feeds a flow, and hand its files over.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes in what it needs, and `LaunchFlow` and `ReadLease` type the
two calls that it makes back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..deliver import DeliveryError, ReadInputs
from ..design import Design
from ..flow import Flow, FlowDependencyFailure
from ..flow.io import declared_inputs, declared_outputs, selected_types
from ..records import INTERNAL_ERROR, ErrorInfo
from ..run_dir import RunDirectoryError
from .bindings import NodeKey
from .error_codes import error_info
from .launch_run import refuse_inputs_inside
from .outputs import handed_design, handed_entries, handed_over
from .resolver import Plan, PlanNode
from .run_lock import CompletedRun, run_dir_read_lock
from .trace import BoundProducer, DeclaredInputRecord, OutputEntry
from .trace_inputs import declared_input_files

log = logging.getLogger(__name__)


class LaunchFlow(Protocol):
    """The type of `FlowLauncher.launch_flow`, the one procedure that every flow goes through.

    The launcher uses it to launch a producer."""

    def __call__(
        self,
        flow_class: str | type[Flow],
        design: Design,
        flow_settings: dict[str, Any] | Flow.Settings | None,
        *,
        depender: Flow | None = None,
        consumer: str | None = None,
        all_flows_settings: dict[str, Any] | None = None,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
    ) -> Flow: ...


class ReadLease(Protocol):
    """The type of `FlowLauncher._producer_read_lease`.

    It gives a lease on a completed producer, held until the launch ends."""

    def __call__(self, producer: Flow) -> AbstractContextManager[Flow]: ...


def _is_blocked(error: Exception) -> bool:
    """Return whether `error` ended a flow that did not run because a producer did not pass: that
    flow wrote no document."""
    return isinstance(error, FlowDependencyFailure) and error.producer is not None


def recorded_error(flow: Flow) -> ErrorInfo:
    """Return the error that the run of `flow` recorded, which a consumer quotes.

    A failed run always records one. A document without a valid one is a bug of Xeda, and the
    consumer says so."""
    try:
        return ErrorInfo.from_json(flow.results.get("error"))  # type: ignore[arg-type]
    except ValueError:
        return ErrorInfo(
            code=INTERNAL_ERROR,
            message=f"`{flow.name}` did not pass and recorded no valid error",
            node=flow.name,
            exit_status=None,
            log=None,
        )


@contextmanager
def producer_read_lease(
    producer: Flow, run_root: Path, completed_runs: Mapping[Path, tuple[Flow, CompletedRun]]
) -> Iterator[Flow]:
    """Take a shared read lock on the run directory of a producer, and verify the completed run.

    A consumer may read the files of the producer only after that."""
    with ExitStack() as lease:
        try:
            lease.enter_context(run_dir_read_lock(producer.run_path, run_root))
        except (OSError, RunDirectoryError) as error:
            raise FlowDependencyFailure(
                f"Cannot acquire shared read lock for producer {producer.name} "
                f"at {producer.run_path}: {error}"
            ) from error
        try:
            _completed, token = completed_runs[producer.run_path.resolve()]
            token.verify()
        except (KeyError, OSError, ValueError, RunDirectoryError) as error:
            raise FlowDependencyFailure(
                f"{producer.name} changed before acquiring its read lease: {error}"
            ) from error
        yield producer


@dataclass(frozen=True)
class DesignHandOver:
    """What the design stage of a launch hands to the consumer: the converter that ran (or was
    reused), the design it derived, and the record of the `design` input."""

    producer: Flow
    design: Design
    record: DeclaredInputRecord


def conversion_note(producer: str, consumer: str, reason: str) -> str:
    """Return why the launch runs `producer` for `consumer`, for the log and for a failure."""
    return f"{producer} converts the design for {consumer}: {reason}"


def _produced(
    producer_node: PlanNode,
    design: Design,
    plan: Plan,
    sections: dict[str, Any] | None,
    read_leases: ExitStack,
    consumed_from: list[Flow],
    *,
    depender: Flow | None,
    consumer: str | None,
    note: str,
    launch_flow: LaunchFlow,
    read_lease: ReadLease,
    completed: dict[tuple[int, NodeKey], Flow],
    entered: Callable[[Path], bool],
) -> Flow:
    """Return the completed producer for `producer_node`, launching it once per plan.

    The producer must have succeeded. Its read lease is held in `read_leases` until the launch
    ends. `note` follows a failure of the producer: it says why the launch needed it."""
    key = (id(plan), producer_node.node_key)
    producer = completed.get(key)
    if producer is None:
        if note:
            log.info("%s", note)
        try:
            producer = launch_flow(
                producer_node.flow_class,
                design,
                producer_node.settings,
                depender=depender,
                consumer=consumer,
                all_flows_settings=sections,
                plan=plan,
                plan_node=producer_node.node_key,
            )
        except Exception as error:
            if isinstance(error, DeliveryError) and error.before_run:
                raise  # refused before the producer's tool ran: not its failure
            cause = error_info(error, node=producer_node.name)
            raise FlowDependencyFailure(
                f"producer {producer_node.name} failed ({cause.code}): {cause.message}"
                + (
                    f"; see {producer_node.run_path / 'results.json'}"
                    if entered(producer_node.run_path) and not _is_blocked(error)
                    else ""
                )
                + (f" ({note})" if note else ""),
                producer=producer_node.name,
            ) from error
        completed[key] = producer
    if not producer.succeeded:
        cause = recorded_error(producer)
        raise FlowDependencyFailure(
            f"producer {producer.name} failed ({cause.code}): {cause.message}; "
            f"see {producer.run_path / 'results.json'}" + (f" ({note})" if note else ""),
            producer=producer.name,
        )
    if producer not in consumed_from:
        read_leases.enter_context(read_lease(producer))
        consumed_from.append(producer)
    return producer


def run_design_producer(
    flow_class: type[Flow],
    design: Design,
    node: PlanNode,
    plan: Plan,
    sections: dict[str, Any] | None,
    read_leases: ExitStack,
    *,
    launch_flow: LaunchFlow,
    read_lease: ReadLease,
    completed: dict[tuple[int, NodeKey], Flow],
    entered: Callable[[Path], bool],
) -> DesignHandOver | None:
    """Launch the producer of the `design` input of `node` and hand the derived design over.

    The launcher calls this under the lock of the consumer and before the consumer exists: the
    derived design decides what the consumer is built with. There is nothing to do for a node
    whose `design` input is unbound (None). `design` is the loaded design, which the converter
    reads. The hand-over verifies the record of the converter under its read lease
    (`outputs.handed_design`), so the derived design is the same whether the converter ran now
    or an earlier run is reused."""
    selected = next((item for item in node.inputs if item.name == "design"), None)
    if selected is None:
        return None
    (reference,) = selected.references
    producer_node = plan.node(reference.node)
    note = (
        conversion_note(producer_node.name, node.name, selected.reason) if selected.reason else ""
    )
    consumed_from: list[Flow] = []
    producer = _produced(
        producer_node,
        design,
        plan,
        sections,
        read_leases,
        consumed_from,
        depender=None,
        consumer=node.name,
        note=note,
        launch_flow=launch_flow,
        read_lease=read_lease,
        completed=completed,
        entered=entered,
    )
    if not declared_outputs(producer_node.flow_class)[reference.output].is_design:
        raise FlowDependencyFailure(
            f"{node.name}.{selected.name}: {producer.name}.{reference.output} "
            "has no compatible output under the selected settings"
        )
    derived, entries = handed_design(producer, reference.output, design)
    record = DeclaredInputRecord(
        name=selected.name,
        origin=selected.origin,
        producer=selected.producer,
        output=selected.output,
        producer_hash=producer.flow_hash,
        producer_path=str(producer.run_path),
        paths=tuple(entry.path for entry in entries),
        references=(
            BoundProducer(
                producer=reference.node,
                output=reference.output,
                producer_hash=producer.flow_hash,
                producer_path=str(producer.run_path),
                entries=entries,
            ),
        ),
        binding_origin=selected.binding_origin,
        binding_location=selected.binding_location,
        equivalence=flow_class.input_equivalence(node.settings, selected.name),
    )
    return DesignHandOver(producer, derived, record)


def run_producers(
    flow: Flow,
    design: Design,
    node: PlanNode,
    plan: Plan,
    sections: dict[str, Any] | None,
    read_leases: ExitStack,
    *,
    launch_flow: LaunchFlow,
    read_lease: ReadLease,
    completed: dict[tuple[int, NodeKey], Flow],
    read_inputs: ReadInputs,
    entered: Callable[[Path], bool],
    design_handover: DesignHandOver | None = None,
) -> list[Flow]:
    """Launch the producers of the planned inputs and hand their files to `flow`.

    This function materializes exactly the planned inputs and never chooses another producer.
    It returns the producers whose files the flow was handed, each once, in the order they
    completed. The `design` input is no part of this: the design stage handed it over already
    (`run_design_producer`, `design_handover`). Its record comes first among the records of the
    flow, as the input comes first among its declarations, and its producer first among the
    producers returned."""
    consumed_from: list[Flow] = [design_handover.producer] if design_handover else []
    records = [design_handover.record] if design_handover else []
    declarations = declared_inputs(type(flow))
    for selected in node.inputs:
        declaration = declarations[selected.name]
        if declaration.is_design:
            continue
        producers: list[Flow] = []
        entries: list[tuple[OutputEntry, ...]] = []
        paths = list(selected.sources)
        # The ordered references of the plan: each producer launched once per plan,
        # its files handed over by the frozen output key, concatenated in reference order.
        for reference in selected.references:
            producer_node = plan.node(reference.node)
            producer = _produced(
                producer_node,
                design,
                plan,
                sections,
                read_leases,
                consumed_from,
                depender=flow,
                consumer=None,
                note="",
                launch_flow=launch_flow,
                read_lease=read_lease,
                completed=completed,
                entered=entered,
            )
            made = declared_outputs(producer_node.flow_class)[reference.output]
            # The kind of value decides first: a design input takes a design output only, and
            # an input of files never takes one.
            if made.is_design != declaration.is_design:
                raise FlowDependencyFailure(
                    f"{flow.name}.{selected.name}: {producer.name}.{reference.output} "
                    "has no compatible output under the selected settings"
                )
            accepted = selected_types(type(flow), node.settings, selected.name)
            produced = selected_types(
                producer_node.flow_class, producer_node.settings, reference.output, output=True
            )
            if not produced or not set(produced) <= set(accepted):
                raise FlowDependencyFailure(
                    f"{flow.name}.{selected.name}: {producer.name}.{reference.output} "
                    "has no compatible output under the selected settings"
                )
            paths.extend(handed_over(producer, reference.output))
            entries.append(handed_entries(producer, reference.output))
            producers.append(producer)
        first_producer = producers[0] if producers else None
        bound_producers = tuple(
            BoundProducer(
                producer=reference.node,
                output=reference.output,
                producer_hash=made_by.flow_hash,
                producer_path=str(made_by.run_path),
                entries=handed,
            )
            for reference, made_by, handed in zip(selected.references, producers, entries)
        )
        if (declaration.required and not paths) or (
            declaration.cardinality != "many" and len(paths) > 1
        ):
            raise FlowDependencyFailure(
                f"{flow.name}'s input `{selected.name}` has {len(paths)} paths; "
                f"expected {declaration.cardinality}"
            )
        for path in paths:
            if not path.is_file():
                raise FlowDependencyFailure(
                    f"{flow.name}'s input `{selected.name}` is missing: {path}"
                )
        value = paths if declaration.cardinality == "many" else (paths[0] if paths else None)
        setattr(flow.inputs, selected.name, value)
        records.append(
            DeclaredInputRecord(
                name=selected.name,
                origin=selected.origin,
                producer=selected.producer,
                output=selected.output,
                producer_hash=first_producer.flow_hash if first_producer else None,
                producer_path=str(first_producer.run_path) if first_producer else None,
                paths=tuple(map(str, paths)),
                references=bound_producers,
                binding_origin=selected.binding_origin,
                binding_location=selected.binding_location,
                equivalence=type(flow).input_equivalence(node.settings, selected.name),
            )
        )
    flow.declared_input_records = tuple(records)
    selected_files = declared_input_files(flow)
    read_inputs.add(selected_files)
    refuse_inputs_inside(flow.run_path, flow.name, selected_files)
    return consumed_from
