import logging
import multiprocessing
import traceback
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from datetime import datetime
from inspect import isclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Type, Union

import psutil
from attrs import define
from pebble.common import ProcessExpired  # type: ignore
from pebble.pool.process import ProcessPool
from pydantic import ValidationError

from ...dataclass import Field, XedaBaseModel, validation_errors
from ...deliver import deliverable_locations
from ...design import Design
from ...flow import Flow, FlowFatalError, FlowSettingsError, flowrun_hash
from ...run_dir import RunDirectory
from ...tool import NonZeroExitCode
from ...utils import (
    Timer,
    dump_json,
    load_class,
    semantic_hash,
    settings_to_dict,
)
from ..default_runner import (
    FlowLauncher,
    add_file_logger,
    get_flow_class,
    print_results,
    run_directory_names,
)
from ..settings_layers import merge_layers
from ..resolver import Plan
from ..run_lock import run_dir_lock
from ..trace import as_recorded

log = logging.getLogger(__name__)


def _purge_run(run_path: Path, run_root: Path, flow_name: str) -> None:
    """Delete a non-improved worker outcome only after readers finish, checking ownership, with
    every name of it (`run_directory_names`), so none is left leading nowhere."""
    with run_dir_lock(run_path, run_root):
        RunDirectory.claimed(run_path, run_root).delete(
            *run_directory_names(run_path, flow_name, run_root)
        )


# Slotted, the `attrs` default: an outcome crosses a process boundary (the worker builds it,
# `pool.map` returns it) and is written to `best.json`, and it does both without an instance
# `__dict__` -- `attrs` generates `__getstate__`/`__setstate__` for pickling, and JSON goes
# through `as_json_value()` below. `slots=False` was there to leave a `__dict__` for a
# serializer to read, which is not a serialization format: it left `run_path` a `Path` for the
# encoder to stringify by luck, and nothing stopped a private attribute joining it.
@define
class FlowOutcome:
    settings: Flow.Settings
    results: Flow.Results
    timestamp: Optional[str]
    run_path: Optional[Path]
    # Worker provenance for promotion; excluded from the public best-run document.
    variation: dict[str, Any] | None = None

    def as_json_value(self) -> Dict[str, Any]:
        """This outcome as JSON: what a design-space exploration records as its best run.

        Written out rather than left to whatever its `__dict__` happens to hold -- `run_path` is
        a `Path`, and the two places that serialize an outcome (the `best.json` a search writes
        as it improves, and the `--json` document the CLI prints) have to agree.
        """
        return {
            "settings": self.settings,
            "results": dict(self.results),
            "timestamp": self.timestamp,
            "run_path": str(self.run_path) if self.run_path else None,
        }


class Optimizer:
    default_variations: Dict[str, Dict[str, List[Any]]] = {}

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
        """The choices the search tries for each setting, best first: the search promotes the
        choices of a better run in place. It owns these lists: they are a copy of what it was
        given (the class's default table, the caller's mapping), which stay as they were."""
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
    """The optimizer's changed leaves; unchanged model defaults are not contributions."""
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


def linspace(a: float, b: float, n: int) -> Tuple[List[float], float]:
    if n < 2:
        return [b], 0
    step = (b - a) / (n - 1)
    return [step * i + a for i in range(n)], step


class Executioner:
    def __init__(
        self,
        launcher: FlowLauncher,
        design: Design,
        flow_class,
        all_flows_settings: Optional[Dict[str, Any]] = None,
        candidate_base: dict[str, Any] | None = None,
        candidate_input: dict[str, Any] | None = None,
    ):
        self.launcher = launcher
        self.design = design
        self.flow_class = flow_class
        self.all_flows_settings = all_flows_settings
        self.candidate_base = candidate_base
        self.candidate_input = candidate_input
        self.candidate_changes: dict[str, Any] = {}

    def __call__(self, args: Tuple[int, Dict[str, Any]]) -> Tuple[Optional[FlowOutcome], int]:
        idx, flow_settings = args
        try:
            delta = merge_layers(
                self.candidate_changes,
                _variation_delta(flow_settings, self.candidate_base or {}),
                settings_cls=self.flow_class.Settings,
            )
            flow_settings = merge_layers(
                self.candidate_input or {}, delta, settings_cls=self.flow_class.Settings
            )
            request = self.launcher._request_context
            api = merge_layers(
                request.api_overrides if request else {}, {self.flow_class.name: delta}
            )
            plan = self.launcher.resolve(
                self.flow_class,
                self.design,
                flow_settings,
                self.all_flows_settings,
                origins=request.origins if request else (),
                command_line=request.command_line if request else None,
                api_overrides=api,
            )
            flow = self.launcher.launch_flow(
                self.flow_class,
                self.design,
                flow_settings,
                all_flows_settings=self.all_flows_settings,
                plan=plan,
            )
            return (
                FlowOutcome(
                    settings=deepcopy(flow.settings),  # type: ignore[call-arg]
                    results=flow.results,
                    timestamp=flow.timestamp,
                    run_path=flow.run_path,
                    variation=delta,
                ),
                idx,
            )
        except KeyboardInterrupt:
            log.exception("KeyboardInterrupt received during the execution of flow")
        except FlowFatalError as e:
            log.warning("Fatal exception during execution of flow: %s", e)
            traceback.print_exc()
            raise e
        except NonZeroExitCode as e:
            log.warning("%s", e)
        except Exception as e:  # noqa: BLE001 - one failed candidate must not stop the search
            log.error(
                "Received exception during the execution of flow, but will continue: %s",
                e,
            )
            traceback.print_exc()
        return None, idx


class Dse(FlowLauncher):
    accepts_bindings = False  # chains and input bindings are local `xeda run` requests

    class Settings(FlowLauncher.Settings):
        max_runtime_minutes: int = Field(
            12 * 3600,
            description="Maximum total running time in minutes, after which no new flow execution will be launched. Flows all ready launched will continue to completion or their timeout.",
        )
        keep_optimal_run_dirs: bool = False

        max_failed_iters: int = 6
        max_failed_iters_with_best: int = 4
        max_workers: int = Field(
            psutil.cpu_count(logical=False) or 2,
            description="Number of parallel executions.",
        )
        timeout: int = 90 * 60  # in seconds
        variations: Optional[Dict[str, List[Any]]] = None
        #: always: each variant keeps its own directory. Variants (and dependencies) with
        #: identical settings share one, locked while a launch uses it, so the second reuses what
        #: the first ran.
        hashed_run_dirs: Literal[True] = True

    def __init__(
        self,
        optimizer_class: Union[str, Type[Optimizer]],
        optimizer_settings: Union[Dict[str, Any], Optimizer.Settings] = {},
        run_root: Union[str, Path, None] = None,
        **kwargs,
    ) -> None:
        """Configure the optimizer and run root (default `./xeda_run`) for a DSE runner."""
        super().__init__(run_root, **kwargs)
        assert isinstance(self.settings, self.Settings)

        # update settings
        if self.settings.keep_optimal_run_dirs:
            self.settings.post_cleanup = False
            self.settings.post_cleanup_purge = False
        self.settings.display_results = False

        if isinstance(optimizer_class, str):
            cls = load_class(optimizer_class, __package__)
            assert cls and issubclass(cls, Optimizer)
            optimizer_class = cls
        if not isinstance(optimizer_settings, Optimizer.Settings):
            assert isinstance(optimizer_settings, dict)
            try:
                optimizer_settings = optimizer_class.Settings(**optimizer_settings)
            except ValidationError as e:
                # Reported as a flow's settings are, naming each field -- not as pydantic's own
                # error, which is what `xeda dse` without its frequency bounds printed.
                raise FlowSettingsError(
                    validation_errors(e.errors()), optimizer_class.Settings
                ) from None
        self.optimizer: Optimizer = optimizer_class(
            max_workers=self.settings.max_workers, settings=optimizer_settings
        )

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
        """Explore flow setting variations and return the best run."""
        assert isinstance(self.settings, self.Settings)
        timer = Timer()

        optimizer = self.optimizer

        optimizer.max_failed_iters = self.settings.max_failed_iters

        # consecutive "unsuccessful" iterations where in all runs success == False
        consecutive_failed_iters = 0
        num_iterations = 0
        future = None
        pool = None

        results_sub = [
            "Fmax",
            "lut",
            "ff",
            "slice",
            "latch",
            "bram_tile",
            "dsp",
        ]

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
        flow_settings = merge_layers(
            flow_settings,
            base_variation,
            settings_cls=flow_class.Settings,
        )

        request = self._request_context
        # Base agreement happens before Logs, best records or the process pool exist.
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

        # Once, here: launched in each worker instead, a missing setting (or a design the flow
        # cannot run) failed every run of the search separately and was reported only as "no
        # successful run".
        flow_class.check_required_settings(base_settings)
        flow_class.check_design_supported(design)
        # every candidate runs in a sibling of this directory, under the same run root
        flow_class.check_run_directory(
            base_settings,
            self.run_path_of(
                design.name,
                flow_class.name,
                flowrun_hash(flow_class.name, base_settings, design.name),
                target=design.target,
            ),
        )
        # many variants would deliver to one path, so an exploration delivers nothing
        located = deliverable_locations(base_settings)
        if located or self.settings.outputs_to is not None:
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
                ]
                or [
                    (
                        "outputs_to",
                        f"{reason}: the best run directory is in its result",
                        None,
                        "value_error",
                    )
                ],
                flow_class.Settings,
            )
        base_settings.redirect_stdout = True
        base_settings.print_commands = False

        if base_settings.nthreads and base_settings.nthreads > 1:
            max_nthreads = max(2, multiprocessing.cpu_count() // self.settings.max_workers)
            base_settings.nthreads = min(base_settings.nthreads, max_nthreads)

        if (
            not base_settings.timeout_seconds
            or base_settings.timeout_seconds > self.settings.timeout
        ):
            base_settings.timeout_seconds = self.settings.timeout

        adjustments = {
            key: getattr(base_settings, key)
            for key in (
                "redirect_stdout",
                "print_commands",
                "nthreads",
                "timeout_seconds",
            )
        }
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
        # `post_cleanup_purge` means here that the exploration deletes the runs that did not
        # improve, itself, once their outcome is in. A launch purges its own run directory only
        # when `post_cleanup` asks for a clean-up too, as before the purge implied one: else the
        # best run would go as well.
        worker = FlowLauncher(
            self._run_root,
            **{
                **{
                    name: getattr(self.settings, name)
                    for name in FlowLauncher.Settings.model_fields
                },
                "post_cleanup_purge": self.settings.post_cleanup
                and self.settings.post_cleanup_purge,
            },
        )
        worker._request_context = self._request_context
        worker._launch_inputs = list(self._launch_inputs)
        executioner = Executioner(
            worker,
            design,
            flow_class,
            all_flows_settings,
            as_recorded(base_settings),
            candidate_input,
        )

        # The exploration's log and its best-run record go into the run root, which is made here,
        # once the settings have validated and no deliverable names a location: an exploration
        # that fails at its input leaves none.
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        add_file_logger(self.run_root / "Logs", timestamp)
        best_json_path = self.run_root / f"fmax_{design.name}_{flow_class.name}_{timestamp}.json"
        log.info("Best results are saved to %s", best_json_path)

        flow_setting_hashes = set()

        num_cpus = psutil.cpu_count() or multiprocessing.cpu_count() or 1
        iterate = True
        try:
            with ProcessPool(max_workers=optimizer.max_workers) as pool:
                while iterate:
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
                    # Compare with the current promoted baseline, not the initial plan.
                    executioner.candidate_base = as_recorded(optimizer.base_settings)
                    batch_settings = optimizer.next_batch()
                    if not batch_settings:
                        break

                    this_batch = []
                    for s in batch_settings:
                        hash_value = deep_hash(s)
                        if hash_value not in flow_setting_hashes:
                            this_batch.append(s)
                            flow_setting_hashes.add(hash_value)
                        else:
                            log.info(
                                "Skipping flow settings with hash %s, already executed in this run.",
                                hash_value,
                            )
                    batch_len = len(batch_settings)
                    batch_len = min(batch_len, self.settings.max_workers)
                    batch_settings = batch_settings[:batch_len]
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

                    future = pool.map(
                        executioner,
                        enumerate(this_batch),
                        timeout=self.settings.timeout,
                    )

                    have_success = False
                    improved = False
                    try:
                        iterator = future.result()
                        if not iterator:
                            log.error("Process result iterator is None!")
                            break
                        while True:
                            try:
                                idx: int
                                outcome: Optional[FlowOutcome]
                                outcome, idx = next(iterator)
                                if outcome is None:
                                    log.error("Flow outcome is None!")
                                    iterate = False
                                    continue
                                improved = optimizer.process_outcome(outcome, idx)
                                if optimizer.base_settings == outcome.settings:
                                    executioner.candidate_changes = deepcopy(
                                        outcome.variation or {}
                                    )
                                if improved:
                                    log.info("Writing improved result to %s", best_json_path)
                                    dump_json(
                                        dict(
                                            best=optimizer.best,
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
                                        log.debug(
                                            "Deleting non-improved run directory: %s",
                                            p,
                                        )
                                        try:
                                            _purge_run(p, self.run_root, flow_class.name)
                                        except (OSError, ValueError) as e:
                                            log.warning("Could not delete %s: %s", p, e)
                                        outcome.run_path = None
                            except StopIteration:
                                break  # next(iterator) finished
                            except TimeoutError as e:
                                log.critical(
                                    f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks."
                                )
                                future.cancel()
                            except ProcessExpired as e:
                                log.critical("%s. Exit code: %d", e, e.exitcode)
                    except CancelledError:
                        log.warning("CancelledError")
                    except KeyboardInterrupt as e:
                        pool.stop()
                        pool.join()
                        raise e from None

                    if not have_success:
                        consecutive_failed_iters += 1
                    else:
                        consecutive_failed_iters = 0

                    num_iterations += 1
                    log.info(
                        f"End of iteration #{num_iterations}. Execution time: {timer.timedelta}"
                    )
                    if optimizer.best:
                        print_results(
                            results=optimizer.best.results,
                            title="Best so far",
                            subset=results_sub,
                            skip_if_false=True,
                        )
                    else:
                        log.info("No results to report.")

        except KeyboardInterrupt:
            log.critical("Received Keyboard Interrupt")
        except Exception as e:
            log.exception("Received exception: %s", e)
            traceback.print_exc()
        finally:
            if pool:
                pool.close()
                pool.join()
            if optimizer.best:
                print_results(
                    results=optimizer.best.results,
                    title="Best results",
                    subset=results_sub,
                )
                log.info("Best result were written to %s", best_json_path)
            else:
                log.error("No successful runs!")
            log.info(
                "Total execution time: %s  Number of iterations: %d",
                timer.timedelta,
                num_iterations,
            )
        return optimizer.best
