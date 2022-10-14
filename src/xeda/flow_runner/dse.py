import json
import logging
import os
import random
import time
import traceback
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool
from pydantic import validator

from ..dataclass import XedaBaseModel, asdict
from ..design import Design
from ..flows.flow import Flow, FlowFatalError, FlowSettingsError, Results, SynthFlow
from ..tool import NonZeroExitCode
from ..utils import unique
from . import FlowLauncher, get_flow_class, print_results

log = logging.getLogger(__name__)


class Optimizer:
    class Settings(XedaBaseModel):
        max_workers: int = 8  # >= 2
        timeout: int = 3600  # in seconds
        resolution: float = 0.2
        delta_increment: float = 0.1

    def __init__(self, **kwargs) -> None:
        self.settings = self.Settings(**kwargs)
        self.improved_idx: Optional[int] = None

        self.best: Optional[Results] = None

    def next_batch(self, base_settings: Flow.Settings) -> List[Dict[str, Any]]:
        return []

    def process_results(self, results: Results, idx: int) -> bool:
        return False


ONE_THOUSAND = 1000.0


def linspace(a: float, b: float, n: int) -> Tuple[List[float], float]:
    if n < 2:
        return [b], 0
    step = (float(b) - a) / (n - 1)
    return [step * i + a for i in range(n)], step


class FmaxOptimizer(Optimizer):
    class Settings(Optimizer.Settings):
        init_freq_low: float
        init_freq_high: float
        max_luts: Optional[int] = None

        @validator("init_freq_high")
        def validate_init_freq(cls, value, values):
            assert (
                value > values["init_freq_low"]
            ), "init_freq_high should be more than init_freq_low"
            return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.no_improvements = 0
        self.prev_frequencies = []
        self.freq_step = 0

        self.previously_tried_frequencies = set()
        # can be different due to rounding errors, TODO only keep track of periods?
        self.previously_tried_periods = set()
        assert isinstance(self.settings, self.Settings)
        self.hi_freq = self.settings.init_freq_high
        self.lo_freq = self.settings.init_freq_low
        self.num_iterations = 0

    def update_bounds(self) -> bool:
        """update low and high bounds based on previous results"""
        resolution = self.settings.resolution
        max_workers = self.settings.max_workers
        prev_frequencies = self.prev_frequencies
        delta_increment = self.settings.delta_increment
        best_freq = float(self.best.results.get("Fmax")) if self.best else None

        if not best_freq or self.improved_idx is None:
            self.no_improvements += 1
            if self.no_improvements > 4:
                log.info(
                    f"Stopping no viable frequencies found after {self.no_improvements} tries."
                )
                return False
            log.info(f"No improvements during this iteration.")
            if not best_freq:
                self.hi_freq = self.lo_freq + resolution
                shrink_factor = 0.7 + self.no_improvements
                self.lo_freq /= shrink_factor
            else:
                if (
                    self.no_improvements > 2
                    and self.freq_step < self.no_improvements * resolution
                ):
                    log.info(
                        f"Stopping as there were no improvements in {self.no_improvements} consecutive iterations."
                    )
                    return False
                self.hi_freq = best_freq + 1.5 * max(
                    self.freq_step, 1 + max_workers * resolution
                )
                self.lo_freq = (self.lo_freq + best_freq) / 2
        else:
            self.lo_freq = (
                best_freq + delta_increment + delta_increment * random.random()
            )
            self.no_improvements = 0
            # last or one before last
            if (
                self.improved_idx >= (len(prev_frequencies) // 2)
                or prev_frequencies[-1] - best_freq <= self.freq_step
            ):
                min_plausible_period = (ONE_THOUSAND / best_freq) - 0.001
                lo_point_choice = (
                    prev_frequencies[1]
                    if len(prev_frequencies) > 4
                    else prev_frequencies[0]
                )
                self.hi_freq = max(
                    best_freq + min(max_workers * 1.0, best_freq - lo_point_choice),
                    ceil(ONE_THOUSAND / min_plausible_period),
                )
            else:
                self.hi_freq = (self.hi_freq + best_freq + self.freq_step) / 2

            self.hi_freq += 1

        self.hi_freq = ceil(self.hi_freq)
        return True

    def next_batch(self, base_settings: Flow.Settings):
        assert isinstance(base_settings, SynthFlow.Settings)

        if self.hi_freq - self.lo_freq < self.settings.resolution:
            return None

        best_freq = float(self.best.results.get("Fmax")) if self.best else None

        batch_settings = []
        finder_retries = 0
        while True:
            frequencies, freq_step = linspace(
                self.lo_freq,
                self.hi_freq,
                self.settings.max_workers,
            )

            if freq_step < self.settings.resolution / 2:
                log.info(f"Stopping: freq_step={freq_step} is below the limit")
                return None

            frequencies = unique(
                [
                    # filter tried frequency and round to picosecond accuracy
                    ONE_THOUSAND / round(ONE_THOUSAND / f, 3)
                    for f in frequencies
                    if f not in self.previously_tried_frequencies
                ]
            )

            clock_periods_to_try = []
            frequencies_unique = []
            for freq in frequencies:
                clock_period = round(ONE_THOUSAND / freq, 3)
                if clock_period not in self.previously_tried_periods:
                    clock_periods_to_try.append(clock_period)
                    frequencies_unique.append(freq)
            frequencies = frequencies_unique

            min_required = (
                (self.settings.max_workers - max(2, self.settings.max_workers / 4))
                if finder_retries > 20
                else self.settings.max_workers
            )

            if len(frequencies) >= max(1, min_required):
                break
            self.hi_freq += random.random() * self.settings.delta_increment
            low = best_freq if best_freq else self.lo_freq
            min_lo_freq = (
                low + self.settings.delta_increment if self.best else self.lo_freq
            )
            self.lo_freq = max(min_lo_freq, self.lo_freq - 0.1 * random.random())
            finder_retries += 1

        log.info(
            f"[Fmax] Trying following frequencies (MHz): {[f'{freq:.2f}' for freq in frequencies]}"
        )

        # TODO Just keep clock_periods!
        self.previously_tried_frequencies.update(frequencies)
        self.previously_tried_periods.update(clock_periods_to_try)

        for clock_period in clock_periods_to_try:
            settings = deepcopy(base_settings)
            settings.clock_period = clock_period
            batch_settings.append(settings)

        self.improved_idx = None
        self.prev_frequencies = frequencies
        if batch_settings:
            self.num_iterations += 1
        self.update_bounds()
        return batch_settings

    def process_results(self, results: Results, idx: int) -> bool:
        """returns True if this was the best result so far"""
        assert isinstance(self.settings, self.Settings)
        freq = results.get("Fmax")
        assert freq, "no valid Fmax in results"

        if self.settings.max_luts:
            lut = results.get("lut")
            if lut and int(lut) > self.settings.max_luts:
                results["exceeds_max_luts"] = True
                return False

        best_freq = float(self.best.results.get("Fmax")) if self.best else None
        if not best_freq or freq > best_freq:
            self.best = results
            self.improved_idx = idx
            return True
        return False


class Executioner:
    def __init__(self, launcher: "Dse", design: Design, flow_class):
        self.launcher = launcher
        self.design = design
        self.flow_class = flow_class

    def __call__(self, args: Tuple[int, Dict[str, Any]]):
        idx, flow_settings = args
        try:
            flow = self.launcher.launch_flow(
                self.flow_class, self.design, flow_settings
            )
            return flow.results, idx
        except FlowFatalError as e:
            log.warning(f"[Run Thread] Fatal exception during flow: {e}")
            traceback.print_exc()
            log.warning(f"[Run Thread] Continuing")
        except KeyboardInterrupt as e:
            log.exception(f"[Run Thread] KeyboardInterrupt received during flow")
            raise e
        except NonZeroExitCode as e:
            log.warning(f"[Run Thread] {e}")
        except Exception as e:
            log.exception(f"Exception: {e}")
        return None, idx


class Dse(FlowLauncher):
    def __init__(
        self,
        optimizer: Optimizer,
        xeda_run_dir: Union[str, os.PathLike] = "xeda_run_dse",
        debug: bool = False,
        dump_settings_json: bool = True,
        display_results: bool = True,
        dump_results_json: bool = True,
    ) -> None:
        super().__init__(
            xeda_run_dir,
            debug,
            dump_settings_json,
            display_results,
            dump_results_json,
            cached_dependencies=True,
            run_in_existing_dir=False,
        )
        self.optimizer: Optimizer = optimizer

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
    ) -> Optional[Results]:
        log.debug(f"run_flow {flow_class}")

        # flow_run_dirs = []

        start_time = time.monotonic()

        optimizer = self.optimizer

        # TODO adaptive tweeking of timeout?
        proc_timeout_seconds = optimizer.settings.timeout
        log.info(f"[Dse] Timeout set to: {proc_timeout_seconds} seconds.")

        error_retries = 0
        num_iterations = 0
        future = None
        pool = None
        results_sub = ["Fmax", "lut", "ff", "slice"]

        successful_results: List[Dict[str, Any]] = []
        executioner = Executioner(self, design, flow_class)

        if isinstance(flow_class, str):
            flow_class = get_flow_class(flow_class)

        if flow_settings is None:
            flow_settings = {}

        if isinstance(flow_settings, Flow.Settings):
            base_settings = flow_settings
        else:
            try:
                base_settings = flow_class.Settings(**flow_settings)
            except FlowSettingsError as e:
                log.error("%s", e)
                exit(1)

        try:
            max_workers = optimizer.settings.max_workers
            with ProcessPool(max_workers=max_workers) as pool:
                while True:
                    settings_to_try = optimizer.next_batch(base_settings)
                    if not settings_to_try:
                        break

                    future = pool.map(
                        executioner,
                        enumerate(settings_to_try),
                        timeout=proc_timeout_seconds,
                    )

                    try:
                        iterator = future.result()
                        if not iterator:
                            log.error("iterator is None! Retrying")
                            if error_retries < 4:
                                error_retries += 1
                                continue  # retry
                            else:
                                log.error("error_retries > MAX, exiting")
                                break
                        while True:
                            try:
                                # flow: Optional[Flow]
                                idx: int
                                results, idx = next(iterator)
                                if results:
                                    # flow_run_dirs.append(flow.run_path)
                                    if results.success:
                                        optimizer.process_results(results, idx)
                                        error_retries = 0
                                        # r = {
                                        #     k: flow.results.get(k) for k in results_sub
                                        # }
                                        # successful_results.append(r)
                            except StopIteration:
                                break  # inner while True
                            except TimeoutError as e:
                                log.critical(
                                    f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks."
                                )
                                future.cancel()
                            except ProcessExpired as e:
                                log.critical(f"{e}. Exit code: {e.exitcode}")
                    except CancelledError:
                        log.warning("[Fmax] CancelledError")
                    except KeyboardInterrupt:
                        pool.stop()
                        pool.join()
                        raise

                    log.info(f"[Fmax] End of iteration #{num_iterations}")
                    log.info(
                        f"[Fmax] Execution Time so far: {int(time.monotonic() - start_time) // 60} minute(s)"
                    )
                    num_iterations += 1
                    if optimizer.best:
                        print_results(
                            results=asdict(optimizer.best),
                            title="Best so far",
                            subset=results_sub,
                        )

        except KeyboardInterrupt:
            log.exception("Received Keyboard Interrupt")
        except Exception as e:
            log.exception(f"Received exception: {e}")
            traceback.print_exc()
        finally:
            if future and not future.cancelled():
                future.cancel()
            if pool:
                pool.close()
                pool.join()
            runtime = int(time.monotonic() - start_time)
            if optimizer.best:
                print_results(
                    results=optimizer.best.results,
                    title="Best Results",
                    subset=results_sub,
                )
                best_json_path = (
                    Path.cwd()
                    / f"fmax_{design.name}_{optimizer.best.name}_{optimizer.best.timestamp}.json"
                )
                log.info(f"Writing best result to {best_json_path}")

                with open(best_json_path, "w") as f:
                    json.dump(
                        dict(
                            best=optimizer.best,
                            successful_results=successful_results,
                            # flow_run_dirs=flow_run_dirs,
                        ),
                        f,
                        default=lambda x: x.__dict__
                        if hasattr(x, "__dict__")
                        else str(x),
                        indent=4,
                    )
            else:
                log.warning("No successful results.")
            log.info(f"[Fmax] Total Execution Time: {runtime} minute(s)")
            log.info(f"[Fmax] Total Iterations: {num_iterations}")
        return optimizer.best
