import logging
import os
import random
import shutil
import traceback
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from attr import define
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool

from ..dataclass import XedaBaseModel, validator
from ..design import Design
from ..flows.flow import Flow, FlowFatalError, FlowSettingsError
from ..tool import NonZeroExitCode
from ..utils import Timer, dump_json, unique
from . import (
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
    class Settings(XedaBaseModel):
        max_workers: int = 8  # >= 2
        timeout: int = 3600  # in seconds

    def __init__(self, **kwargs) -> None:
        self.base_settings: Flow.Settings = Flow.Settings()
        self.flow_class: Optional[Type[Flow]] = None
        self.variations: Dict[str, List[str]] = {}

        self.settings = self.Settings(**kwargs)
        self.improved_idx: Optional[int] = None

        self.best: Optional[FlowOutcome] = None

    def next_batch(self) -> Union[None, List[Flow.Settings], List[Dict[str, Any]]]:
        ...

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        ...


ONE_THOUSAND = 1000.0


def linspace(a: float, b: float, n: int) -> Tuple[List[float], float]:
    if n < 2:
        return [b], 0
    step = (b - a) / (n - 1)
    return [step * i + a for i in range(n)], step


flow_settings_variations: Dict[str, Dict[str, List[str]]] = {
    "vivado_synth": {
        "synth.steps.synth_design.args.flatten_hierarchy": ["full"],
        "synth.strategy": [
            "Flow_AlternateRoutability",
            "Flow_PerfThresholdCarry",
            "Flow_PerfOptimized_high",
            "Flow_RuntimeOptimized",
        ],
        "impl.strategy": [
            "Flow_RunPostRoutePhysOpt",  # fast
            "Performance_ExtraTimingOpt",
            "Flow_RunPhysOpt",  # fast
            "Performance_NetDelay_low",
            "Performance_Explore",
            "Performance_NetDelay_high",  # slow
            "Performance_ExplorePostRoutePhysOpt",  # slow
            # "Flow_RuntimeOptimized", # fast
            "Performance_Retiming",
            "Performance_RefinePlacement",
        ],
    },
    "vivado_alt_synth": {
        "synth.strategy": [
            "ExtraTimingCongestion",
            "ExtraTimingAltRouting",
            "ExtraTiming",
            # "Timing",
        ],
    },
}


class FmaxOptimizer(Optimizer):
    class Settings(Optimizer.Settings):
        init_freq_low: float
        init_freq_high: float
        max_luts: Optional[int] = None
        max_finder_retries = 5

        delta_increment: float = 0.001
        resolution: float = 0.2
        min_freq_step: float = 0.05

        @validator("init_freq_high")
        def validate_init_freq(cls, value, values):
            assert (
                value > values["init_freq_low"]
            ), "init_freq_high should be more than init_freq_low"
            return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.no_improvements: int = 0
        self.prev_frequencies = []
        self.freq_step: float = 0.0
        self.num_variations = 1

        # can be different due to rounding errors, TODO only keep track of periods?
        self.previously_tried_periods = set()
        assert isinstance(self.settings, self.Settings)
        self.hi_freq = self.settings.init_freq_high
        self.lo_freq = self.settings.init_freq_low
        self.num_iterations: int = 0

    @property
    def best_freq(self) -> Optional[float]:
        return self.best.results.get("Fmax") if self.best else None

    def update_bounds(self) -> bool:
        """
        update low and high bounds based on previous results
        It's called _before_ calculation of each iteration batch
        return False -> stop iteration
        """
        assert isinstance(self.settings, self.Settings)

        if self.num_iterations == 0:  # first time
            return True
        resolution = self.settings.resolution
        max_workers = self.settings.max_workers
        delta_increment = self.settings.delta_increment

        max_failed_iters = 4
        max_failed_iters_with_best = 2

        best_freq = self.best_freq

        if self.hi_freq - self.lo_freq < resolution:
            if best_freq or self.no_improvements > 1:
                return False

        if self.improved_idx is None:
            self.no_improvements += 1
            if self.no_improvements > max_failed_iters:
                log.info(
                    "Stopping after %d unsuccessfull iterations (max_failed_iters=%d)",
                    self.no_improvements,
                    max_failed_iters,
                )
                return False
            if best_freq:
                self.num_variations = min(max_workers, self.num_variations + 1)
                log.info("Increased num_variations to %d", self.num_variations)
                if self.no_improvements > max_failed_iters_with_best:
                    log.debug(
                        f"no_improvements={self.no_improvements} > max_failed_iters_with_best({max_failed_iters_with_best})"
                    )
                    return False
            else:
                self.hi_freq = self.lo_freq - resolution
                if self.hi_freq <= resolution:
                    log.warning("hi_freq < resolution")
                    return False
                self.lo_freq = self.hi_freq / (0.7 + self.no_improvements)
        else:
            # sanity check, best_freq was set before in case of a successful run
            assert (
                best_freq
            ), f"best_freq was None, while improved_idx={self.improved_idx}"

            # reset no_improvements
            self.no_improvements = 0

            # set lo_freq to a bit above lo_freq
            self.lo_freq = best_freq + delta_increment
            # if best freq
            if best_freq >= self.hi_freq:
                self.hi_freq = best_freq + self.freq_step * max_workers
                log.debug("incrementing hi_freq to %0.2f", self.hi_freq)
            else:
                self.hi_freq = (self.hi_freq + best_freq) / 2 + resolution
                log.debug("decrementing hi_freq to %0.2f", self.hi_freq)
        return True

    def next_batch(self) -> Union[None, List[Flow.Settings], List[Dict[str, Any]]]:
        assert isinstance(self.settings, self.Settings)

        if not self.update_bounds():
            return None

        batch_settings = []
        finder_retries = 0

        n = self.settings.max_workers
        if self.num_variations > 1:
            log.info("Generating %d variations", self.num_variations)
            n = n // self.num_variations
        while True:
            if self.hi_freq <= 0 or self.lo_freq < 0:
                return None
            if self.hi_freq - self.lo_freq < self.settings.resolution:
                return None
            freq_candidates, freq_step = linspace(
                self.lo_freq,
                self.hi_freq,
                n,
            )
            self.freq_step = freq_step
            log.debug(
                "[try %d] lo_freq=%0.2f, hi_freq=%0.2f, freq_step=%0.2f",
                finder_retries,
                self.lo_freq,
                self.hi_freq,
                freq_step,
            )

            if self.freq_step < self.settings.min_freq_step:
                log.info(f"Stopping: freq_step={freq_step} is below the limit")
                return None

            clock_periods_to_try = []
            frequencies = []
            for freq in freq_candidates:
                clock_period = round(ONE_THOUSAND / freq, 3)
                if clock_period not in self.previously_tried_periods:
                    clock_periods_to_try.append(clock_period)
                    frequencies.append(freq)

            clock_periods_to_try = unique(clock_periods_to_try)

            if len(clock_periods_to_try) >= max(1, n - 1):
                break

            if finder_retries > self.settings.max_finder_retries:
                log.error("finder failed!")
                return None

            finder_retries += 1
            delta = finder_retries * random.random() + self.settings.delta_increment
            if self.best_freq:
                self.hi_freq += delta + n * (random.random() * self.settings.resolution)
                log.info("finder increased hi_freq to %0.2f", self.hi_freq)
            else:
                self.hi_freq -= delta
                self.lo_freq = max(0, self.lo_freq - delta)
                log.warning(
                    "finder updated range to [%0.2f, %0.2f]", self.lo_freq, self.hi_freq
                )

        self.previously_tried_periods.update(clock_periods_to_try)

        log.info(
            f"[DSE] Trying following frequencies (MHz): {[f'{freq:.2f}' for freq in frequencies]}"
        )

        if self.flow_class:
            assert isinstance(self.base_settings, self.flow_class.Settings)

        def rand_choice(lst, mx):
            mx = min(len(lst), mx)
            return random.choice(lst[:mx])

        base_settings = dict(self.base_settings)
        for i in range(self.num_variations):
            for clock_period in clock_periods_to_try:
                vv = settings_to_dict(
                    {
                        k: rand_choice(v, i + 1 + self.no_improvements)
                        for k, v in self.variations.items()
                    },
                    expand_dict_keys=True,
                )
                settings = {**base_settings, "clock_period": clock_period, **vv}
                batch_settings.append(settings)

        self.improved_idx = None
        if batch_settings:
            self.num_iterations += 1
        return batch_settings

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        """returns True if this was the best result so far"""
        assert isinstance(self.settings, self.Settings)

        if not outcome.results.success:
            return False

        freq_str = outcome.results.get("Fmax")
        if freq_str is None:
            log.warning("Fmax was None!")
            return False
        freq = float(freq_str)

        if self.settings.max_luts:
            lut = outcome.results.get("lut")
            if lut and int(lut) > self.settings.max_luts:
                log.info(
                    "Used LUTs %s larger than maximum allowed %s. Fmax: %s",
                    lut,
                    self.settings.max_luts,
                    freq,
                )
                outcome.results["exceeds_max_luts"] = True
                return False

        best_freq = self.best_freq
        if best_freq and freq > best_freq:
            impr = freq - best_freq
            log.info("New best frequency: %.2f MHz  Improvement:%.2f MHz", freq, impr)

        if best_freq is None or freq > best_freq:
            self.best = outcome
            self.base_settings = outcome.settings
            self.improved_idx = idx
            if self.num_variations > 1 and idx > self.settings.max_workers // 2:
                self.num_variations -= 1
            return True
        else:
            log.info(
                "Got lower Fmax: %.2f than the current best: %.2s", freq, best_freq
            )
            return False


class Executioner:
    def __init__(self, launcher: "Dse", design: Design, flow_class):
        self.launcher = launcher
        self.design = design
        self.flow_class = flow_class

    def __call__(
        self, args: Tuple[int, Dict[str, Any]]
    ) -> Tuple[Optional[FlowOutcome], int]:
        idx, flow_settings = args
        try:
            flow = self.launcher.launch_flow(
                self.flow_class, self.design, flow_settings
            )
            return (
                FlowOutcome(
                    settings=deepcopy(flow.settings),
                    results=flow.results,
                    timestamp=flow.timestamp,
                    run_path=flow.run_path,
                ),
                idx,
            )
        except FlowFatalError as e:
            log.warning(f"[Run Thread] Fatal exception during flow: {e}")
            traceback.print_exc()
            log.warning("[Run Thread] Continuing")
        except KeyboardInterrupt as e:
            log.exception("[Run Thread] KeyboardInterrupt received during flow")
            raise e
        except NonZeroExitCode as e:
            log.warning(f"[Run Thread] {e}")
        except Exception as e:
            log.exception(f"Exception: {e}")
        return None, idx


class Dse(FlowLauncher):
    def __init__(
        self,
        optimizer: Union[str, Optimizer],
        xeda_run_dir: Union[str, os.PathLike] = "xeda_run_dse",
    ) -> None:
        super().__init__(
            xeda_run_dir,
            debug=False,
            dump_settings_json=True,
            display_results=False,
            dump_results_json=True,
            cached_dependencies=True,
            run_in_existing_dir=False,
            cleanup=False,
        )
        if isinstance(optimizer, str):
            if optimizer == "fmax":
                optimizer = FmaxOptimizer()
            else:
                raise Exception(f"Unknown optimizer: {optimizer}")
        self.optimizer: Optimizer = optimizer

    def run_flow(
        self,
        flow_class: Union[str, Type[Flow]],
        design: Design,
        flow_settings: Union[None, Dict[str, Any], Flow.Settings] = None,
        cleanup_nonoptimal_runs: bool = True,
    ):
        timer = Timer()

        optimizer = self.optimizer

        unsuccessfull_iters = 0
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

        if flow_settings is None:
            flow_settings = {}

        if isinstance(flow_settings, Flow.Settings):
            flow_settings = dict(flow_settings)

        optimizer.variations = flow_settings_variations[flow_class.name]

        base_variation = settings_to_dict(
            {k: v[0] for k, v in optimizer.variations.items() if v},
            expand_dict_keys=True,
        )
        flow_settings = {**flow_settings, **base_variation}

        try:
            base_settings = flow_class.Settings(**flow_settings)
        except FlowSettingsError as e:
            log.error("[DSE] %s", e)
            exit(1)

        base_settings.redirect_stdout = True

        optimizer.flow_class = flow_class
        optimizer.base_settings = base_settings

        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]
        add_file_logger(Path.cwd(), timestamp)
        best_json_path = (
            Path.cwd() / f"fmax_{design.name}_{flow_class.name}_{timestamp}.json"
        )
        log.info("Final results will be saved to: %s", best_json_path)
        max_workers = optimizer.settings.max_workers
        log.info("max_workers=%d", max_workers)

        iterate = True
        try:
            with ProcessPool(max_workers=max_workers) as pool:
                while iterate:
                    settings_to_try = optimizer.next_batch()
                    if not settings_to_try:
                        break

                    future = pool.map(
                        executioner,
                        enumerate(settings_to_try),
                        timeout=optimizer.settings.timeout,
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
                                if cleanup_nonoptimal_runs and not improved:
                                    p = outcome.run_path
                                    if p and p.exists():
                                        log.debug(
                                            "Deleting non-improved run directory: %s",
                                            p,
                                        )
                                        shutil.rmtree(p, ignore_errors=True)
                                        outcome.run_path = None
                                if outcome.results.success:
                                    have_success = True
                                    r = {k: outcome.results.get(k) for k in results_sub}
                                    successful_results.append(r)
                            except StopIteration:
                                break  # next(iterator) finished
                            except TimeoutError as e:
                                log.critical(
                                    f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks."
                                )
                                future.cancel()
                            except ProcessExpired as e:
                                log.critical(f"{e}. Exit code: {e.exitcode}")
                    except CancelledError:
                        log.warning("[DSE] CancelledError")
                    except KeyboardInterrupt as e:
                        pool.stop()
                        pool.join()
                        raise e from None

                    if not have_success:
                        unsuccessfull_iters += 1

                    num_iterations += 1
                    log.info(
                        f"[DSE] End of iteration #{num_iterations}. Execution time: {timer.timedelta}"
                    )
                    if optimizer.best:
                        if have_success:
                            log.info(f"Writing current best result to {best_json_path}")
                            dump_json(
                                dict(
                                    best=optimizer.best,
                                    successful_results=successful_results,
                                    total_time=timer.timedelta,
                                    # flow_run_dirs=flow_run_dirs,
                                ),
                                best_json_path,
                                backup_previous=False,
                            )
                        print_results(
                            results=optimizer.best.results,
                            title="Best so far",
                            subset=results_sub,
                            skip_if_false=True,
                        )
                    else:
                        log.info("No results to report.")

        except KeyboardInterrupt:
            log.exception("Received Keyboard Interrupt")
            log.critical("future: %s pool: %s", future, pool)
            if pool:
                pool.join()
            if future and not future.cancelled():
                future.cancel()
            if pool:
                pool.close()
                pool.join()
        except Exception as e:
            log.exception(f"Received exception: {e}")
            traceback.print_exc()
        finally:
            if future and not future.cancelled():
                future.cancel()
            if pool:
                pool.close()
                pool.join()
            if optimizer.best:
                print_results(
                    results=optimizer.best.results,
                    title="Best Results",
                    subset=results_sub,
                )
                log.info(f"Best result are written to {best_json_path}")
                # dump_json(
                #     dict(
                #         best=optimizer.best,
                #         successful_results=successful_results,
                #         total_time_minutes=timer.minutes,
                #         # flow_run_dirs=flow_run_dirs,
                #     ),
                #     best_json_path,
                #     backup_previous=False,
                # )
            else:
                log.warning("No successful results.")
            log.info(f"[DSE] Total Execution Time: {timer.timedelta}")
            log.info(f"[DSE] Total Iterations: {num_iterations}")
        return optimizer.best
