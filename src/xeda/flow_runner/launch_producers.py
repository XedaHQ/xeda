"""The producers stage of a launch: run what the plan says feeds a flow, and hand its files over.

The functions are free functions with explicit arguments. This module never imports
`default_runner`: the launcher passes in what it needs, and `LaunchFlow` and `ReadLease` type the
two calls that it makes back.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol

from ..deliver import DeliveryError, ReadInputs
from ..design import Design
from ..flow import Flow, FlowDependencyFailure
from ..flow.io import declared_inputs, selected_types
from ..run_dir import RunDirectoryError
from .bindings import NodeKey
from .launch_run import refuse_inputs_inside
from .outputs import handed_entries, handed_over
from .resolver import Plan, PlanNode
from .run_lock import CompletedRun, run_dir_read_lock
from .trace import BoundProducer, DeclaredInputRecord, OutputEntry
from .trace_inputs import declared_input_files


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
        all_flows_settings: dict[str, Any] | None = None,
        plan: Plan | None = None,
        plan_node: NodeKey | None = None,
    ) -> Flow: ...


class ReadLease(Protocol):
    """The type of `FlowLauncher._producer_read_lease`.

    It gives a lease on a completed producer, held until the launch ends."""

    def __call__(self, producer: Flow) -> AbstractContextManager[Flow]: ...


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
) -> list[Flow]:
    """Launch the producers of the planned inputs and hand their files to `flow`.

    This function materializes exactly the planned inputs and never chooses another producer.
    It returns the producers whose files the flow was handed, each once, in the order they
    completed."""
    consumed_from: list[Flow] = []
    records = []
    declarations = declared_inputs(type(flow))
    for selected in node.inputs:
        declaration = declarations[selected.name]
        producers: list[Flow] = []
        entries: list[tuple[OutputEntry, ...]] = []
        paths = list(selected.sources)
        # The ordered references of the plan: each producer launched once per plan,
        # its files handed over by the frozen output key, concatenated in reference order.
        for reference in selected.references:
            producer_node = plan.node(reference.node)
            key = (id(plan), producer_node.node_key)
            producer = completed.get(key)
            if producer is None:
                try:
                    producer = launch_flow(
                        producer_node.flow_class,
                        design,
                        producer_node.settings,
                        depender=flow,
                        all_flows_settings=sections,
                        plan=plan,
                        plan_node=producer_node.node_key,
                    )
                except Exception as error:
                    if isinstance(error, DeliveryError) and error.before_run:
                        raise  # refused before the producer's tool ran: not its failure
                    raise FlowDependencyFailure(
                        f"producer {producer_node.name} failed: {error}"
                        + (
                            f"; see {producer_node.run_path / 'results.json'}"
                            if entered(producer_node.run_path)
                            else ""
                        )
                    ) from error
                completed[key] = producer
            if not producer.succeeded:
                raise FlowDependencyFailure(
                    f"producer {producer.name} failed: {producer.results.get('error', '')}; "
                    f"see {producer.run_path / 'results.json'}"
                )
            if producer not in consumed_from:
                read_leases.enter_context(read_lease(producer))
                consumed_from.append(producer)
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
        producer = producers[0] if producers else None
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
                producer_hash=producer.flow_hash if producer else None,
                producer_path=str(producer.run_path) if producer else None,
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
