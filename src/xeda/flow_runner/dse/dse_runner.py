import logging
import multiprocessing
from copy import deepcopy
from datetime import datetime
from inspect import isclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, ClassVar, Dict, List, Literal, Optional, Tuple, Type, Union

import psutil
from attrs import define
from pydantic import ValidationError

from ...cancel import CancelToken
from ...dataclass import Field, NoReplacement, XedaBaseModel, validation_errors
from ...deliver import deliverable_locations
from ...design import Design
from ...flow import Flow, FlowSettingsError, FlowSettingsException, flowrun_hash
from ...frozen import thaw
from ...outcomes import Outcome
from ...records import ErrorInfo, RunRecord
from ...run_dir import RunDirectory
from ...spec import Spec
from ...utils import (
    Timer,
    dump_json,
    load_class,
    semantic_hash,
    settings_to_dict,
)
from ... import log_context
from ...workspace import Workspace
from ..chains import FlowRequest, parse_request
from ..default_runner import (
    FlowLauncher,
    add_file_logger,
    get_flow_class,
    remove_file_logger,
)
from ..scrub import run_directory_names
from ..settings_layers import (
    compose_flow_settings,
    merge_flow_sections,
    merge_layers,
    read_command_line_text,
    registered_flow,
)
from ..resolver import SHARED_SETTINGS, Plan, planned_design_of
from ..run_lock import refuse_if_held, run_dir_lock
from ..trace import as_recorded
from ...types import UNSET

log = logging.getLogger(__name__)


OPENROAD_DSE_REFUSAL = (
    "Fmax exploration of the OpenROAD flows is not supported yet: the clock is a setting of "
    "openroad_place, which the optimizer does not reach. Run `xeda dse` on `yosys`, or vary "
    "`flows.openroad_place.clocks` yourself."
)


def _refuse_openroad(optimizer: "Optimizer", flow: Union[str, Type[Flow], FlowRequest]) -> None:
    """Refuse an optimizer that writes clocks, for a request of one of the OpenROAD flows.

    Such an optimizer puts its candidate periods into the `clocks` of the requested flow. The
    clock of an OpenROAD flow chain is a setting of `openroad_place`, so the periods do not reach
    it. An optimizer that varies other settings is not refused. The test is by class: every flow
    built on `OpenroadStage` counts, whatever its name."""
    from ...flows.openroad import OpenroadStage

    if not optimizer.writes_clocks:
        return
    if isinstance(flow, str):
        flow = parse_request(flow)
    requested = flow.requested if isinstance(flow, FlowRequest) else flow
    if isclass(requested) and issubclass(requested, OpenroadStage):
        raise FlowSettingsException(OPENROAD_DSE_REFUSAL)


def _purge_run(run_path: Path, run_root: Path, flow_name: str) -> None:
    """Delete the run of a point that did not improve, with every name of it.

    The names come from `run_directory_names`, so none is left leading nowhere. The deletion
    waits until readers finish, and it checks ownership."""
    with run_dir_lock(run_path, run_root):
        RunDirectory.claimed(run_path, run_root).delete(
            *run_directory_names(run_path, flow_name, run_root)
        )


@define
class FlowOutcome:
    """What an optimizer gets of one point: the settings of the point, the results of its run, and
    the record of the run (`results.json`). `results.success` is true for a run that passed. A
    run that was rejected for its timing keeps the results that it had, `fmax` among them.
    `run_path` is the run directory, or `None` when the exploration deleted it."""

    settings: Flow.Settings
    results: Flow.Results
    run_path: Optional[Path]
    record: Optional[RunRecord] = None


@define(frozen=True)
class DsePoint:
    """One point of an exploration: how its run ended, and where. `error` is `None` for a run
    that passed. `run_dir` is `None` when the exploration deleted the run directory."""

    outcome: Outcome
    error: Optional[ErrorInfo]
    run_dir: Optional[Path]

    def as_json_value(self) -> Dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "error": None if self.error is None else self.error.as_json_value(),
            "run_dir": None if self.run_dir is None else str(self.run_dir),
        }


@define(frozen=True)
class DseResult:
    """What an exploration did: its best run, if any, and every point that it tried, in order.
    `canceled` is true when a cancellation ended it."""

    best: Optional[FlowOutcome]
    points: Tuple[DsePoint, ...]
    canceled: bool = False


#: The results of a run that an exploration reports.
RESULTS_SHOWN = [
    "fmax",
    "lut",
    "ff",
    "slice",
    "latch",
    "bram_tile",
    "dsp",
]


class Optimizer:
    default_variations: Dict[str, Dict[str, List[Any]]] = {}
    #: True for an optimizer that puts candidate clock periods into the `clocks` of the requested
    #: flow, as the Fmax search does.
    writes_clocks: ClassVar[bool] = False

    class Settings(XedaBaseModel):
        pass

    def __init__(self, max_workers: int, settings: Optional[Settings] = None, **kwargs) -> None:
        assert max_workers > 0
        self.max_workers: int = max_workers
        self.max_failed_iters: int = 2
        self.base_settings: Flow.Settings = Flow.Settings()
        self.flow_class: Optional[Type[Flow]] = None
        self._variations: Dict[str, List[Any]] = {}

        self.settings = settings if settings else self.Settings(**kwargs)
        self.improved_idx: Optional[int] = None  # ATM only used as a bool
        self.failed_fmax: Optional[float] = None  # failed due to negative slack
        self.best: Optional[FlowOutcome] = None

    @property
    def variations(self) -> Dict[str, List[Any]]:
        """The choices that the search tries for each setting, best first.

        The search promotes the choices of a better run in place. It owns these lists: they are a
        copy of what it was given (the default table of the class, the mapping of the caller),
        which stay as they were."""
        return self._variations

    @variations.setter
    def variations(self, value: Dict[str, List[Any]]) -> None:
        self._variations = deepcopy(value)

    def next_batch(self) -> Union[None, List[Dict[str, Any]]]: ...

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        ...
        return True


def deep_hash(s) -> str:
    return semantic_hash(s)


def _variation_delta(candidate: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Return the leaves that the optimizer changed.

    Unchanged model defaults are not contributions."""
    candidate, base = as_recorded(candidate), as_recorded(base)
    delta = {}
    for key, value in candidate.items():
        previous = base.get(key)
        if isinstance(value, dict) and isinstance(previous, dict):
            nested = _variation_delta(value, previous)
            if nested:
                delta[key] = nested
        elif value != previous:
            delta[key] = deepcopy(value)
    return delta


def _spread_shared(
    optimizer: Mapping[str, Mapping[str, Any]], command_line: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Return the shared settings that a node of `optimizer` gives, for each other node that
    `command_line` gives the same setting and `optimizer` gives none.

    A point puts the contributions of the optimizer in the same layer as the command line. A
    shared setting that the optimizer gives to one node would then meet the value that the command
    line gave to another node in the same layer, and the two would disagree. An API override wins
    over the command line, so the value of the optimizer replaces that value."""
    spread: dict[str, dict[str, Any]] = {}
    for name, section in optimizer.items():
        for shared in SHARED_SETTINGS:
            if shared not in section:
                continue
            for node, given in command_line.items():
                if node != name and shared in given and shared not in optimizer.get(node, {}):
                    spread.setdefault(node, {}).setdefault(shared, deepcopy(section[shared]))
    return spread


def _point_settings(
    command_line: Mapping[str, Any],
    api: Mapping[str, Any],
    delta: Mapping[str, Any],
    flow_name: str,
) -> dict[str, dict[str, Any]]:
    """Return the settings of one point: the layers of the request, with the delta of the point
    on top. The contributions of the optimizer (`api`, `delta`) outrank the command line, as
    API overrides do, also across the nodes that share a setting."""
    optimizer = merge_flow_sections(api, {flow_name: delta}, flow_class_for=registered_flow)
    return merge_flow_sections(
        command_line,
        _spread_shared(optimizer, command_line),
        optimizer,
        flow_class_for=registered_flow,
    )


class _RoundToken(CancelToken):
    """The token of one round: it expires after `timeout` seconds, and it is canceled when `outer`
    is."""

    def __init__(self, timeout: float, outer: Optional[CancelToken]) -> None:
        super().__init__(timeout=timeout)
        self._outer = outer

    @property
    def canceled(self) -> bool:
        return super().canceled or (self._outer is not None and self._outer.canceled)


def linspace(a: float, b: float, n: int) -> Tuple[List[float], float]:
    if n < 2:
        return [b], 0
    step = (b - a) / (n - 1)
    return [step * i + a for i in range(n)], step


class Dse(FlowLauncher):
    accepts_bindings = False  # chains and input bindings are local `xeda run` requests

    class Settings(FlowLauncher.Settings):
        max_runtime_minutes: int = Field(
            12 * 60,
            description="Maximum total running time, in minutes. After that, no new flow is "
            "launched. Flows that are already launched continue until they complete or time out.",
        )
        keep_optimal_run_dirs: bool = False

        max_failed_iters: int = 6
        max_failed_iters_with_best: int = 4
        max_workers: int = Field(
            psutil.cpu_count(logical=False) or 2,
            description="Number of parallel executions.",
        )
        timeout: int = Field(
            90 * 60,
            gt=0,
            description="Time limit of one round, in seconds. When it passes, the points of the "
            "round that have not finished end as canceled, and the exploration goes on with the "
            "next round.",
        )
        variations: Optional[Dict[str, List[Any]]] = None
        #: Always true. Each variant keeps its own directory. Variants (and producers) with
        #: identical settings share one, locked while a launch uses it, so the second reuses what
        #: the first ran.
        hashed_run_dirs: Literal[True] = True

        #: An exploration reads none of these settings of a launcher.
        removed_inputs: ClassVar[Mapping[str, str]] = {
            "debug": NoReplacement("an exploration does not read it"),
            "dump_settings_json": NoReplacement("an exploration does not read it"),
            "dump_results_json": NoReplacement("an exploration does not read it"),
            "display_results": NoReplacement("an exploration does not read it"),
            "backups": NoReplacement("an exploration does not read it"),
            "prune_variants": NoReplacement("an exploration never removes earlier runs"),
            "outputs_to": NoReplacement("an exploration delivers nothing"),
            "overwrite_outputs": NoReplacement("an exploration delivers nothing"),
        }

    def __init__(
        self,
        optimizer_class: Union[str, Type[Optimizer]],
        optimizer_settings: Union[Dict[str, Any], Optimizer.Settings] = {},
        run_root: Union[str, Path, None] = None,
        **kwargs,
    ) -> None:
        """Configure the optimizer and the run root (default `./xeda_run`) of a DSE launcher.

        The text of `KEY=VALUE` items in `kwargs` and in `optimizer_settings` (as the command line
        gives them) is read as the type of each setting."""
        super().__init__(run_root, **read_command_line_text(kwargs, self.Settings))
        assert isinstance(self.settings, self.Settings)

        # update settings
        if self.settings.keep_optimal_run_dirs:
            self.settings.post_cleanup = False
            self.settings.post_cleanup_purge = False

        if isinstance(optimizer_class, str):
            cls = load_class(optimizer_class, __package__)
            assert cls and issubclass(cls, Optimizer)
            optimizer_class = cls
        if not isinstance(optimizer_settings, Optimizer.Settings):
            assert isinstance(optimizer_settings, dict)
            optimizer_settings = read_command_line_text(
                optimizer_settings, optimizer_class.Settings
            )
            try:
                optimizer_settings = optimizer_class.Settings(**optimizer_settings)
            except ValidationError as e:
                # Reported as the settings of a flow are, naming each field. This replaces the
                # error of pydantic itself, which is what `xeda dse` printed without its
                # frequency bounds.
                raise FlowSettingsError(
                    validation_errors(e.errors()), optimizer_class.Settings
                ) from None
        self._optimizer_class = optimizer_class
        self._optimizer_settings = optimizer_settings
        #: The optimizer of the last run, or of none yet. Every run makes its own, so no state of
        #: one run reaches the next.
        self.optimizer: Optimizer = self._new_optimizer()
        #: Called after each iteration that has a best run, with that run's outcome. The library
        #: prints nothing: the command line passes a callback that shows the results.
        self.best_so_far: Optional[Callable[[FlowOutcome], None]] = None
        #: Stops the exploration when it says so: the points that run end as `canceled`.
        self.cancel: Optional[CancelToken] = None
        self._local_settings: Any = None
        #: What the last run did. It is `None` before a run starts, and after an exception ended
        #: a run it holds the points and the best run that the run had until then.
        self.result: Optional[DseResult] = None

    def _new_optimizer(self) -> Optimizer:
        assert isinstance(self.settings, self.Settings)
        return self._optimizer_class(
            max_workers=self.settings.max_workers,
            settings=self._optimizer_settings.model_copy(deep=True),
        )

    def run(self, *args: Any, **kwargs: Any) -> DseResult:  # type: ignore[override]
        """Explore the settings of a flow for a design, as `FlowLauncher.run` takes its request.

        Return what the exploration did, even when no run passed. An exception of the optimizer
        ends the exploration with that exception, and `result` holds what it did until then."""
        self.result = None
        result = super().run(*args, **kwargs)
        assert isinstance(result, DseResult)
        return result

    def _value_overlay(
        self,
        flow_class: Type[Flow],
        design: Design,
        flow_settings: Dict[str, Any],
        request: Any,
    ) -> Dict[str, Any]:
        """Return what the settings of the request (`flow_settings`) set beyond the layers that
        the request kept: the leaves where a settings value of the caller overrides the files.

        A request keeps only what the caller wrote in a settings value, but a value wins as a
        whole over the design and the project. Without a request, there are none."""
        if request is None:
            return {}
        bare = compose_flow_settings(
            flow_class,
            [
                *(section for _, section in request.origins),
                request.command_line,
                request.api_overrides,
            ],
        )
        if as_recorded(bare) == as_recorded(flow_settings):
            return {}
        context = dict(design_root=design.root, runner_cwd=self._start)
        return _variation_delta(
            as_recorded(flow_class.Settings.from_input(flow_settings, **context)),
            as_recorded(flow_class.Settings.from_input(bare, **context)),
        )

    def resolve_run(  # type: ignore[override]
        self,
        flow: Any,
        design: Any = None,
        flow_settings: Any = [],
        flow_overrides: Any = [],
        target: Any = UNSET,
        local_settings: Any = None,
    ) -> Any:
        """Resolve the request, as a launcher does, and keep the private settings that it gets:
        the sweeps of the exploration plan every point with them."""
        self._local_settings = local_settings
        return super().resolve_run(
            flow,
            design,
            flow_settings,
            flow_overrides,
            target=target,
            local_settings=local_settings,
        )

    def _refuse_unaccepted_request(
        self, flow, flow_settings, flow_overrides, xedaproject, design=None
    ) -> None:
        """Refuse a clock-writing optimizer on an OpenROAD flow or a chain that ends in one, then
        what a chain or a binding brings, before anything is loaded."""
        _refuse_openroad(self.optimizer, flow)
        super()._refuse_unaccepted_request(flow, flow_settings, flow_overrides, xedaproject, design)

    @log_context.with_launch_context
    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
        *,
        depender: Optional[Flow] = None,
        all_flows_settings: Optional[Dict[str, Any]] = None,
        plan: Plan | None = None,
    ):
        """Explore variations of the flow settings and return the best run.

        The exploration holds no lock while its workers run: a thread that holds one is refused."""
        refuse_if_held()
        assert isinstance(self.settings, self.Settings)
        _refuse_openroad(self.optimizer, flow_class)
        timer = Timer()

        optimizer = self.optimizer = self._new_optimizer()

        optimizer.max_failed_iters = self.settings.max_failed_iters

        # Consecutive "unsuccessful" iterations where in all runs success == False.
        consecutive_failed_iters = 0
        num_iterations = 0
        canceled = False
        points: List[DsePoint] = []

        results_sub = RESULTS_SHOWN

        successful_results: List[Dict[str, Any]] = []
        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)

        assert isclass(flow_class) and issubclass(flow_class, Flow)
        if flow_settings is None:
            flow_settings = {}
        if isinstance(flow_settings, Flow.Settings):
            flow_settings = flow_settings.model_dump()
        assert isinstance(flow_settings, dict)
        if plan is not None:
            self._validate_plan(plan, flow_class, design, flow_settings, all_flows_settings)

        if self.settings.variations is not None:
            optimizer.variations = self.settings.variations
        else:
            optimizer.variations = optimizer.default_variations[flow_class.name]

        possible_variations = 1
        for v in optimizer.variations.values():
            if v:
                possible_variations *= len(v)

        log.info("Number of possible setting variations: %d", possible_variations)

        base_variation = settings_to_dict(
            {k: v[0] for k, v in optimizer.variations.items() if v},
            hierarchical_keys=True,
        )
        request = self._request_context
        # What a settings value of the caller sets beyond the layers of the request: its full
        # values win over the design, and the request keeps only what the caller wrote.
        overlay = self._value_overlay(flow_class, design, flow_settings, request)
        flow_settings = merge_layers(
            flow_settings,
            base_variation,
            settings_cls=flow_class.Settings,
        )

        # Base agreement happens before Logs, best records or any sweep exist.
        base_plan = self.resolve(
            flow_class,
            design,
            flow_settings,
            all_flows_settings,
            origins=request.origins if request else (),
            command_line=request.command_line if request else None,
            api_overrides=merge_layers(
                request.api_overrides if request else {}, {flow_class.name: base_variation}
            ),
        )
        base_settings = base_plan.node(flow_class.name).settings

        # Check once, here. Checked for each point instead, a missing setting (or a design that
        # the flow cannot run) would fail every run of the search separately, and be reported
        # only as "no successful run".
        flow_class.check_required_settings(base_settings)
        # The design the flow is planned to read: a Bluespec design that bsc turns into Verilog is
        # checked as the Verilog it will be, as a launch does.
        flow_class.check_design_supported(
            planned_design_of(base_plan, base_plan.node(flow_class.name), design, self.run_path_of),
            base_settings,
        )
        # Every candidate runs in a sibling of this directory, under the same run root.
        flow_class.check_run_directory(
            base_settings,
            self.run_path_of(
                design.name,
                flow_class.name,
                flowrun_hash(flow_class.name, base_settings, design.name),
                target=design.target,
            ),
        )
        # Many variants would deliver to one path, so an exploration delivers nothing.
        located = deliverable_locations(base_settings)
        if located:
            reason = "an exploration runs many variants, so nothing is delivered"
            raise FlowSettingsError(
                [
                    (
                        key,
                        f"`{key}` names a location ({path}): {reason}; give a name",
                        None,
                        "value_error",
                    )
                    for key, path in located
                ],
                flow_class.Settings,
            )
        if base_settings.nthreads and base_settings.nthreads > 1:
            max_nthreads = max(2, multiprocessing.cpu_count() // self.settings.max_workers)
            base_settings.nthreads = min(base_settings.nthreads, max_nthreads)

        adjustments = {"nthreads": base_settings.nthreads}
        candidate_input = merge_layers(flow_settings, adjustments, settings_cls=flow_class.Settings)
        adjusted_plan = self.resolve(
            flow_class,
            design,
            candidate_input,
            all_flows_settings,
            origins=request.origins if request else (),
            command_line=request.command_line if request else None,
            api_overrides=merge_layers(
                request.api_overrides if request else {},
                {flow_class.name: merge_layers(base_variation, adjustments)},
            ),
        )
        base_settings = adjusted_plan.node(flow_class.name).settings
        optimizer.flow_class = flow_class
        optimizer.base_settings = base_settings
        # Every point is a spec of the design as the caller gave it. What all points share is the
        # request's own layers, the base variation and the thread limit. What differs is the
        # delta of the point to the base settings. The command line is one layer. The API
        # overrides of the request, the values of the caller, the base variation, the thread
        # limit and the delta are the layer above it (`_point_settings`).
        own = merge_layers(overlay, base_variation, adjustments, settings_cls=flow_class.Settings)
        if request is not None:
            declared = request.declared
            command_line = request.command_line
            api = merge_flow_sections(
                request.api_overrides, {flow_class.name: own}, flow_class_for=registered_flow
            )
        else:  # a caller that has no request: the layers as they were given
            declared = design
            command_line = all_flows_settings or {}
            api = {
                flow_class.name: merge_layers(flow_settings, own, settings_cls=flow_class.Settings)
            }
        base_recorded = as_recorded(base_settings)
        workspace = Workspace(
            self._run_root,
            hashed_run_dirs=True,
            tool_output="log",
            local_settings=self._local_settings,
        )
        # `post_cleanup_purge` means here that the exploration deletes the runs that did not
        # improve, itself, once their outcome is in. A sweep purges its run directories only when
        # `post_cleanup` asks for a clean-up too, as it did before the purge implied one.
        # Otherwise the best run would go as well.
        purge_with_sweep = self.settings.post_cleanup and self.settings.post_cleanup_purge

        # The exploration's log and its best-run record go into the run root, which is made here,
        # once the settings have validated and no deliverable names a location: an exploration
        # that fails at its input leaves none.
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        best_json_path = self.run_root / f"fmax_{design.name}_{flow_class.name}_{timestamp}.json"
        flow_setting_hashes = set()
        num_cpus = psutil.cpu_count() or multiprocessing.cpu_count() or 1
        # Nothing between opening the log and the `try` that closes it.
        file_handler = add_file_logger(self.run_root / "Logs", timestamp)

        def outer_canceled() -> bool:
            return self.cancel is not None and self.cancel.canceled

        try:
            log.info("Best results are saved to %s", best_json_path)
            while True:
                if outer_canceled():
                    canceled = True
                    break
                cpu_usage = tuple((ld / num_cpus) * 100 for ld in psutil.getloadavg())
                ram_usage = psutil.virtual_memory()[2]

                log.info(
                    "CPU load over (1, 5, 15) minutes: %d%%, %d%%, %d%%    RAM usage: %d%%",
                    cpu_usage[0],
                    cpu_usage[1],
                    cpu_usage[2],
                    ram_usage,
                )

                if consecutive_failed_iters > self.settings.max_failed_iters:
                    log.info(
                        "Stopping after %d unsuccessful iterations.",
                        consecutive_failed_iters,
                    )
                    break
                if (
                    optimizer.best
                    and consecutive_failed_iters > self.settings.max_failed_iters_with_best
                ):
                    log.info(
                        "Stopping after %d unsuccessful iterations (max_failed_iters_with_best=%d)",
                        consecutive_failed_iters,
                        self.settings.max_failed_iters_with_best,
                    )
                    break

                if timer.minutes > self.settings.max_runtime_minutes:
                    log.warning(
                        "Total execution time (%d minutes) exceed 'max_runtime_minutes'=%d",
                        timer.minutes,
                        self.settings.max_runtime_minutes,
                    )
                    break
                batch_settings = optimizer.next_batch()
                if not batch_settings:
                    break

                # Each point keeps its position in the batch of the optimizer, whatever the points
                # before it did: a point that an earlier round ran is not run again.
                this_batch = []
                for position, s in enumerate(batch_settings):
                    hash_value = deep_hash(s)
                    if hash_value not in flow_setting_hashes:
                        this_batch.append((position, s))
                        flow_setting_hashes.add(hash_value)
                    else:
                        log.info(
                            "Skipping flow settings with hash %s, already executed in this run.",
                            hash_value,
                        )
                batch_len = len(batch_settings)
                batch_len = min(batch_len, self.settings.max_workers)
                if batch_len < self.settings.max_workers:
                    log.warning(
                        "Only %d (out of %d) workers will be utilized.",
                        batch_len,
                        self.settings.max_workers,
                    )

                log.info(
                    "Starting iteration #%d with %d parallel executions.",
                    num_iterations,
                    batch_len,
                )

                have_success = False
                improved = False
                if this_batch:
                    # A point is its settings, as the delta to the base settings that the
                    # candidates grew from.
                    specs = [
                        Spec(
                            flow_class.name,
                            declared,
                            settings=_point_settings(
                                command_line,
                                api,
                                _variation_delta(s, base_recorded),
                                flow_class.name,
                            ),
                        )
                        for _, s in this_batch
                    ]
                    swept = workspace.sweep(
                        specs,
                        max_workers=self.settings.max_workers,
                        cancel=_RoundToken(self.settings.timeout, self.cancel),
                        rebuild_all=self.settings.rebuild_all,
                        clean=self.settings.clean,
                        post_cleanup=self.settings.post_cleanup,
                        post_cleanup_purge=purge_with_sweep,
                    )
                    if swept.canceled:
                        if outer_canceled():
                            canceled = True
                        else:
                            log.critical(
                                "Flow run took longer than %d seconds. Canceled the remaining runs.",
                                self.settings.timeout,
                            )
                    # Every point of the round is a point of the result before the optimizer reads
                    # the first of them: an exception of the optimizer loses none.
                    ran: List[Tuple[int, int, Dict[str, Any], RunRecord]] = []
                    for (position, candidate), point in zip(this_batch, swept):
                        result = point.result
                        node = result.nodes[result.roots[0]] if result.roots else None
                        record = None if node is None else node.record
                        run_dir = None if node is None else node.run_dir
                        points.append(DsePoint(result.outcome, result.error, run_dir))
                        if record is None or record.outcome is Outcome.CANCELED:
                            log.warning("Flow #%d: no results (%s).", position, result.outcome)
                            continue
                        ran.append((position, len(points) - 1, candidate, record))
                    for position, index, candidate, record in ran:
                        results = Flow.Results(thaw(record.metrics))
                        results.success = record.passed
                        # The settings the point started from: a flow may change its copy while
                        # it runs (an output path made absolute), and the next round grows from these.
                        outcome = FlowOutcome(
                            settings=flow_class.Settings.from_input(
                                candidate, design_root=design.root
                            ),
                            results=results,
                            run_path=points[index].run_dir,
                            record=record,
                        )
                        improved = optimizer.process_outcome(outcome, position)
                        if improved:
                            log.info("Writing improved result to %s", best_json_path)
                            dump_json(
                                dict(
                                    best=(
                                        None if outcome.record is None else outcome.record.to_dict()
                                    ),
                                    successful_results=successful_results,
                                    total_time=timer.timedelta,
                                    optimizer_settings=optimizer.settings,
                                    num_iterations=num_iterations,
                                    consecutive_failed_iters=consecutive_failed_iters,
                                    design=design,
                                ),
                                best_json_path,
                                backup=False,
                            )
                        if outcome.results.success:
                            have_success = True
                            r = {k: outcome.results.get(k) for k in results_sub}
                            successful_results.append(r)
                        if (
                            self.settings.post_cleanup_purge
                            and not improved
                            and (have_success or num_iterations > 0)
                        ):
                            p = outcome.run_path
                            if p and p.exists():
                                log.debug("Deleting non-improved run directory: %s", p)
                                try:
                                    _purge_run(p, self.run_root, flow_class.name)
                                except (OSError, ValueError) as e:
                                    log.warning("Could not delete %s: %s", p, e)
                                outcome.run_path = None
                                points[index] = DsePoint(
                                    points[index].outcome, points[index].error, None
                                )

                if not have_success:
                    consecutive_failed_iters += 1
                else:
                    consecutive_failed_iters = 0

                num_iterations += 1
                log.info(f"End of iteration #{num_iterations}. Execution time: {timer.timedelta}")
                if optimizer.best:
                    log.debug(
                        "Best so far: %s",
                        {k: optimizer.best.results.get(k) for k in results_sub},
                    )
                    if self.best_so_far is not None:
                        self.best_so_far(optimizer.best)
                else:
                    log.info("No results to report.")
                if canceled:
                    break
        finally:
            try:
                if optimizer.best:
                    log.info("The best result was written to %s", best_json_path)
                else:
                    log.error("No successful runs!")
                log.info(
                    "Total execution time: %s  Number of iterations: %d",
                    timer.timedelta,
                    num_iterations,
                )
            finally:
                # The log of the search belongs to the search: a later search of the process has its
                # own, and this runs however the clean-up above ends.
                remove_file_logger(file_handler)
                # An exception ends the search here too: what it had found stays in `result`.
                self.result = DseResult(optimizer.best, tuple(points), canceled or outer_canceled())
        return self.result
