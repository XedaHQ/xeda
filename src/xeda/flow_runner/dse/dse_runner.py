from inspect import isclass
import logging
import multiprocessing
import os
import shutil
import traceback
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type, Union

import psutil
from attrs import define
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool

from ...dataclass import Field, XedaBaseModel
from ...design import Design
from ...flow import Flow, FlowFatalError
from ...tool import NonZeroExitCode
from ...utils import Timer, dump_json, load_class
from ..default_runner import (
    FlowLauncher,
    add_file_logger,
    get_flow_class,
    print_results,
    settings_to_dict,
)

log = logging.getLogger(__name__)


@define(slots=False)
class FlowOutcome:
    settings: Flow.Settings
    results: Flow.Results
    timestamp: Optional[str]
    run_path: Optional[Path]


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
        self.variations: Dict[str, List[str]] = {}

        self.settings = settings if settings else self.Settings(**kwargs)
        self.improved_idx: Optional[int] = None  # ATM only used as a bool
        self.failed_fmax: Optional[float] = None  # failed due to negative slack
        self.best: Optional[FlowOutcome] = None

    def next_batch(self) -> Union[None, List[Dict[str, Any]]]:
        ...

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        ...
        return True


# way more light weight than semantic_hash
def deep_hash(s) -> int:
    def freeze(d):
        if isinstance(d, (str, int, bool, tuple, frozenset)):
            return d
        if isinstance(d, (list, tuple)):
            return tuple(freeze(value) for value in d)
        if isinstance(d, dict):
            return frozenset((key, freeze(value)) for key, value in d.items())
        if isinstance(d, XedaBaseModel) or hasattr(d, "__dict__"):
            return freeze(dict(d))
        return d

    return hash(freeze(s))


def linspace(a: float, b: float, n: int) -> Tuple[List[float], float]:
    if n < 2:
        return [b], 0
    step = (b - a) / (n - 1)
    return [step * i + a for i in range(n)], step


class Executioner:
    def __init__(self, launcher: FlowLauncher, design: Design, flow_class):
        self.launcher = launcher
        self.design = design
        self.flow_class = flow_class

    def __call__(self, args: Tuple[int, Dict[str, Any]]) -> Tuple[Optional[FlowOutcome], int]:
        idx, flow_settings = args
        try:
            flow = self.launcher._launch_flow(self.flow_class, self.design, flow_settings)
            return (
                FlowOutcome(
                    settings=deepcopy(flow.settings),  # type: ignore[call-arg]
                    results=flow.results,
                    timestamp=flow.timestamp,
                    run_path=flow.run_path,
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
        except Exception as e:
            log.error(
                "Received exception during the execution of flow, but will continue: %s",
                e,
            )
            traceback.print_exc()
        return None, idx


class Dse(FlowLauncher):
    class Settings(FlowLauncher.Settings):
        max_runtime_minutes: int = Field(
            12 * 3600,
            description="Maximum total running time in minutes, after which no new flow execution will be launched. Flows all ready launched will continue to completion or their timeout.",
        )
        keep_optimal_run_dirs: bool = False

        max_failed_iters: int = 6
        max_failed_iters_with_best: int = 4
        max_workers: int = Field(
            psutil.cpu_count(logical=False),
            description="Number of parallel executions.",
        )
        timeout: int = 90 * 60  # in seconds
        variations: Optional[Dict[str, List[Any]]] = None

    def __init__(
        self,
        optimizer_class: Union[str, Type[Optimizer]],
        optimizer_settings: Union[Dict[str, Any], Optimizer.Settings] = {},
        xeda_run_dir: Union[str, os.PathLike] = "xeda_run_dse",
        **kwargs,
    ) -> None:
        super().__init__(
            xeda_run_dir,
            **kwargs,
        )
        assert isinstance(self.settings, self.Settings)

        # update settings
        if self.settings.keep_optimal_run_dirs:
            self.settings.post_cleanup = False
            self.settings.post_cleanup_purge = False
        self.settings.display_results = False
        self.settings.incremental = False

        if isinstance(optimizer_class, str):
            cls = load_class(optimizer_class, __package__)
            assert cls and issubclass(cls, Optimizer)
            optimizer_class = cls
        if not isinstance(optimizer_settings, Optimizer.Settings):
            optimizer_settings = optimizer_class.Settings(**optimizer_settings)
        self.optimizer: Optimizer = optimizer_class(
            max_workers=self.settings.max_workers, settings=optimizer_settings
        )

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
    ):
        assert isinstance(self.settings, self.Settings)
        timer = Timer()

        optimizer = self.optimizer

        optimizer.max_failed_iters = self.settings.max_failed_iters

        # consecutive "unsuccessfull" iterations where in all runs success == False
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
        executioner = Executioner(self, design, flow_class)

        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)

        assert isclass(flow_class) and issubclass(flow_class, Flow)

        if flow_settings is None:
            flow_settings = {}

        if isinstance(flow_settings, Flow.Settings):
            flow_settings = dict(flow_settings)

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
        flow_settings = {**flow_settings, **base_variation}

        base_settings = flow_class.Settings(**flow_settings)
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

        optimizer.flow_class = flow_class
        optimizer.base_settings = base_settings

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        add_file_logger(Path.cwd(), timestamp)
        best_json_path = Path.cwd() / f"fmax_{design.name}_{flow_class.name}_{timestamp}.json"
        log.info("Best results are saved to %s", best_json_path)

        flow_setting_hashes: Set[int] = set()

        num_cpus = psutil.cpu_count()
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
                            "Stopping after %d unsuccessfull iterations.",
                            consecutive_failed_iters,
                        )
                        break
                    if (
                        optimizer.best
                        and consecutive_failed_iters > self.settings.max_failed_iters_with_best
                    ):
                        log.info(
                            "Stopping after %d unsuccessfull iterations (max_failed_iters_with_best=%d)",
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

                    this_batch = []
                    for s in batch_settings:
                        hash_value = deep_hash(s)
                        if hash_value not in flow_setting_hashes:
                            this_batch.append(s)
                            flow_setting_hashes.add(hash_value)

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
                                outcome: FlowOutcome
                                outcome, idx = next(iterator)
                                if outcome is None:
                                    log.error("Flow outcome is None!")
                                    iterate = False
                                    continue
                                improved = optimizer.process_outcome(outcome, idx)
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
                                        shutil.rmtree(p, ignore_errors=True)
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
