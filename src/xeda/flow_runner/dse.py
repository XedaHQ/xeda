import logging
import multiprocessing
import os
import random
import shutil
import traceback
from concurrent.futures import CancelledError, TimeoutError
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type, Union

import psutil
from attr import define
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool

from ..dataclass import Field, XedaBaseModel, validator
from ..design import Design
from ..flows.flow import Flow, FlowFatalError
from ..tool import NonZeroExitCode
from ..utils import Timer, dump_json, load_class, unique
from . import (
    FlowLauncher,
    add_file_logger,
    get_flow_class,
    print_results,
    semantic_hash,
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
        pass

    def __init__(
        self, max_workers: int, settings: Optional[Settings] = None, **kwargs
    ) -> None:
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


flow_settings_variations: Dict[str, Dict[str, List[Any]]] = {
    "vivado_synth": {
        "synth.steps.synth_design.args.flatten_hierarchy": ["full"],
        "synth.steps.synth_design.args.NO_LC": [False, True],
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
            "Performance_ExplorePostRoutePhysOpt",  # slow
            "Performance_Retiming",
            "Performance_RefinePlacement",
            "Performance_ExploreWithRemap",
            "Performance_NetDelay_high",  # slow
            # "Flow_RuntimeOptimized", # fast
        ],
    },
    "vivado_alt_synth": {
        "synth.strategy": [
            "ExtraTiming",
            "ExtraTimingAlt",
            "Timing",
        ],
        "impl.strategy": [
            "TimingAutoPlace1",  # only available since Vivado 2022.1
            "ExtraTimingCongestion",
            "TimingAutoPlace2",  # only available since Vivado 2022.1
            "ExtraTimingAltRouting",
        ],
    },
}


class FmaxOptimizer(Optimizer):
    class Settings(Optimizer.Settings):
        init_freq_low: float
        init_freq_high: float
        max_luts: Optional[int] = None
        max_finder_retries = 10

        delta: float = 0.001
        resolution: float = 0.2
        min_freq_step: float = 0.02

        # min improvement inf frequency before increasing variations
        variation_min_improv = 2.0

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
        self.last_improvement: float = 0.0
        self.num_iterations: int = 0

        # can be different due to rounding errors, TODO only keep track of periods?
        self.previously_tried_periods = set()

        # array of {key -> choice} choices, indexed by flow idx
        self.variation_choices: List[Dict[str, int]]

        assert isinstance(self.settings, self.Settings)
        assert self.settings.init_freq_high > self.settings.init_freq_low
        self.hi_freq = self.settings.init_freq_high
        self.lo_freq = self.settings.init_freq_low
        assert self.settings.resolution > 0.0

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
        max_workers = self.max_workers
        delta = self.settings.delta

        best_freq = self.best_freq

        if self.hi_freq - self.lo_freq < resolution:
            if self.no_improvements > 1:
                log.info(
                    "Stopping as span (=%0.2f) < %0.2f and %d iterations without improvement",
                    self.hi_freq - self.lo_freq,
                    resolution,
                    self.no_improvements,
                )
                return False

        # if we have a best_freq (or failed_fmax better than previous lo_freq)
        #  and little or no improvement during previous iteration, increment num_variations
        if best_freq or (self.failed_fmax and self.failed_fmax > self.lo_freq):
            if (
                self.improved_idx is None
                or self.last_improvement < self.settings.variation_min_improv
            ):
                if self.num_variations < max_workers:
                    self.num_variations = self.num_variations + 1
                    log.info(
                        "Increased number of variations to %d", self.num_variations
                    )
        if best_freq:
            # we have a best_freq, but no improvements this time
            # increment lo_freq by a small positive random value
            epsilon = random.uniform(
                delta,
                max(delta, resolution / (self.num_variations + 2)),
            )
            self.lo_freq = best_freq + epsilon

        if self.improved_idx is None:
            self.no_improvements += 1
            if best_freq:
                if best_freq < self.hi_freq:
                    self.hi_freq = (self.hi_freq + best_freq) / 2 + delta
                    if self.num_variations > 1 and self.no_improvements < 3:
                        self.hi_freq += (
                            (max_workers + 1) * resolution
                        ) // self.num_variations
                    else:
                        # no variations or too many failures, just binary search
                        log.info(
                            "No Improvements. Lowering hi_freq to %0.2f", self.hi_freq
                        )
                else:
                    self.hi_freq = best_freq + self.num_variations * resolution
                    log.warning(
                        "No Improvements, but still incrementing hi_freq to %0.2f (%d variations)",
                        self.hi_freq,
                        self.num_variations,
                    )
            else:
                self.hi_freq = self.lo_freq - resolution
                if self.hi_freq <= resolution:
                    log.warning("hi_freq < resolution")
                    return False
                if self.failed_fmax:
                    self.lo_freq = self.failed_fmax
                else:
                    log.error(
                        "All runs in the previous iteration failed without reporting an Fmax! Please check the flow's logs to determine the reason."
                    )
                    return False
                    # self.lo_freq = self.hi_freq / (0.7 + self.no_improvements)

                if self.hi_freq - self.lo_freq < resolution:
                    self.hi_freq = self.lo_freq + max_workers * resolution
                log.info(
                    "Lowering bounds to [%0.2f, %0.2f]", self.lo_freq, self.hi_freq
                )
        else:
            # sanity check, best_freq was set before in case of a successful run
            assert (
                best_freq
            ), f"best_freq was None, while improved_idx={self.improved_idx}"

            # reset no_improvements
            self.no_improvements = 0

            # if best freq
            if best_freq >= self.hi_freq:
                self.hi_freq = best_freq + max(resolution, self.freq_step) * max_workers
                log.debug("incrementing hi_freq to %0.2f", self.hi_freq)
            else:
                self.hi_freq = (
                    self.hi_freq + best_freq
                ) / 2 + self.num_variations * resolution
                log.debug("decrementing hi_freq to %0.2f", self.hi_freq)

        if best_freq:
            # sanity check
            assert (
                self.lo_freq > best_freq
            ), f"BUG! self.lo_freq ({self.lo_freq}) <= best_freq({best_freq})"
            assert (
                self.hi_freq > best_freq
            ), f"BUG! self.hi_freq ({self.hi_freq}) <= best_freq({best_freq})"

        log.debug("Bound set to [%0.2f, %0.2f]", self.lo_freq, self.hi_freq)
        return True

    def next_batch(self) -> Union[None, List[Flow.Settings], List[Dict[str, Any]]]:
        assert isinstance(self.settings, self.Settings)

        if not self.update_bounds():
            return None

        finder_retries = 0
        n = self.max_workers
        if self.num_variations > 1:
            log.info("Generating %d variations", self.num_variations)
            n = n // self.num_variations
        while True:
            if self.hi_freq <= 0 or self.lo_freq < 0:
                log.warning(
                    "hi_freq(%0.2f) or lo_freq(%0.2f) were not positive!",
                    self.hi_freq,
                    self.lo_freq,
                )
                return None
            freq_candidates, freq_step = linspace(
                self.lo_freq,
                self.hi_freq,
                n,
            )
            log.debug(
                "[try %d] lo_freq=%0.2f, hi_freq=%0.2f, freq_step=%0.2f",
                finder_retries,
                self.lo_freq,
                self.hi_freq,
                freq_step,
            )

            if self.best and n > 1 and freq_step < self.settings.min_freq_step:
                log.warning(
                    "Stopping: freq_step=%0.3f is below 'min_freq_step' (%0.3f)",
                    freq_step,
                    self.settings.min_freq_step,
                )
                return None

            self.freq_step = freq_step

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
                log.error("finder failed after %d retries!", finder_retries)
                return None

            finder_retries += 1
            if self.best_freq:
                self.hi_freq += self.settings.delta + (
                    random.random() * self.settings.resolution
                )
                log.debug("finder increased hi_freq to %0.2f", self.hi_freq)
            else:
                delta = finder_retries * random.random() + self.settings.delta
                self.hi_freq -= delta
                self.lo_freq = max(0, self.lo_freq - delta)
                log.debug(
                    "finder loop updated range to [%0.2f..%0.2f]",
                    self.lo_freq,
                    self.hi_freq,
                )

        self.previously_tried_periods.update(clock_periods_to_try)

        log.info(
            "Trying following frequencies (MHz): %s",
            ", ".join(f"{freq:.2f}" for freq in frequencies),
        )

        if self.flow_class:
            assert isinstance(self.base_settings, self.flow_class.Settings)

        self.variation_choices = [{} for _ in range(self.num_variations)]

        mfi = max(self.max_failed_iters, self.no_improvements)

        def rand_choice(k, val_list, var_idx, task_idx):
            """
            k        : settings key name
            val_list : list of settings value variations
            var_idx  : variation index
            task_idx : task index in batch_settings
            """
            n = len(val_list)
            assert n > 0
            if self.num_variations <= 1 or n == 1:
                return val_list[0]
            approx_max_score = self.max_workers + mfi
            score = (var_idx + 1 + self.no_improvements) / approx_max_score
            if score >= 1 or score <= 0:
                log.warning("[rand_choice] score=%0.3f", score)
            choice_max = min(n - 1, round(n * score))
            # 0 <= choice_max <= n-1
            choice = random.randrange(0, choice_max + 1)
            self.variation_choices[task_idx][k] = choice
            return val_list[choice]

        base_settings = dict(self.base_settings)
        batch_settings = []
        task_idx = 0
        for i in range(self.num_variations):
            for clock_period in clock_periods_to_try:
                vv = settings_to_dict(
                    {
                        k: rand_choice(k, v, i, task_idx)
                        for k, v in self.variations.items()
                        if v
                    },
                    expand_dict_keys=True,
                )
                settings = {**base_settings, "clock_period": clock_period, **vv}
                batch_settings.append(settings)
                task_idx += 1

        self.improved_idx = None
        self.num_iterations += 1
        return batch_settings

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        """returns True if this was the best result so far"""
        assert isinstance(self.settings, self.Settings)

        freq = outcome.results.get("Fmax")

        if not outcome.results.success:
            if freq and not self.best:
                # Failed due to negative slack
                # Keep the Fmax for next iter, if no other runs succeeded
                if not self.failed_fmax or self.failed_fmax < freq:
                    log.info(
                        "Flow #%d failed, but Fmax=%0.2f was suggested.", idx, freq
                    )
                    self.failed_fmax = freq
            return False

        if freq is None:
            log.error(
                "Flow #%d: No valid 'Fmax' in the results! run_path=%s",
                idx,
                outcome.run_path,
            )
            return False

        if self.settings.max_luts:
            lut = outcome.results.get("lut")
            if lut and int(lut) > self.settings.max_luts:
                log.warning(
                    "Used LUTs %s larger than maximum allowed %s. Fmax: %s",
                    lut,
                    self.settings.max_luts,
                    freq,
                )
                return False

        best_freq = self.best_freq
        if best_freq and freq > best_freq:
            self.last_improvement = freq - best_freq
            log.info(
                "New best frequency: %0.2f MHz  Improvement:%0.2f MHz",
                freq,
                self.last_improvement,
            )

        def promote(lst: List[Any], idx: int):
            if idx > 0 and lst:
                lst.insert(0, lst.pop(idx))

        if best_freq is None or freq > best_freq:
            self.best = outcome
            self.base_settings = outcome.settings
            self.improved_idx = idx
            if self.num_variations > 1:
                var_choices = self.variation_choices[idx]
                for k, i in var_choices.items():
                    promote(self.variations[k], i)
                if idx > (self.max_workers + 1) // 2:
                    self.num_variations -= 1
            return True
        else:
            log.debug("Lower Fmax: %0.2f than the current best: %0.2f", freq, best_freq)
            return False


class Executioner:
    def __init__(self, launcher: FlowLauncher, design: Design, flow_class):
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
    class Settings(FlowLauncher.Settings):
        max_runtime_minutes = Field(
            12 * 3600,
            description="Maximum total running time in minutes, after which no new flow execution will be launched. Flows all ready launched will continue to completion or their timeout.",
        )  # type: ignore
        keep_optimal_run_dirs: bool = True

        max_failed_iters = 6
        max_failed_iters_with_best = 4
        max_workers: int = Field(
            psutil.cpu_count(logical=False),
            description="Number of parallel executions.",
        )
        timeout: int = 90 * 60  # in seconds

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
        self.settings.cleanup &= not self.settings.keep_optimal_run_dirs
        self.settings.display_results = False

        if isinstance(optimizer_class, str):
            cls = load_class(optimizer_class, self.__module__)
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

        base_settings = flow_class.Settings(**flow_settings)
        base_settings.redirect_stdout = True

        if base_settings.nthreads > 1:
            max_nthreads = max(
                2, multiprocessing.cpu_count() // self.settings.max_workers
            )
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
        best_json_path = (
            Path.cwd() / f"fmax_{design.name}_{flow_class.name}_{timestamp}.json"
        )
        log.info("Best results are saved to %s", best_json_path)

        flow_setting_hashes: Set[str] = set()

        num_cpus = psutil.cpu_count()
        iterate = True
        try:
            with ProcessPool(max_workers=optimizer.max_workers) as pool:
                while iterate:
                    cpu_usage = tuple(
                        (ld / num_cpus) * 100 for ld in psutil.getloadavg()
                    )
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
                        and consecutive_failed_iters
                        > self.settings.max_failed_iters_with_best
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
                        hash = semantic_hash(s)
                        if hash not in flow_setting_hashes:
                            this_batch.append(s)
                            flow_setting_hashes.add(hash)

                    batch_len = len(batch_settings)
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
                                    log.info(
                                        "Writing improved result to %s", best_json_path
                                    )
                                    dump_json(
                                        dict(
                                            best=optimizer.best,
                                            successful_results=successful_results,
                                            total_time=timer.timedelta,
                                            optimizer_settings=optimizer.settings,
                                            num_iterations=num_iterations,
                                            consecutive_failed_iters=consecutive_failed_iters,
                                        ),
                                        best_json_path,
                                        backup_previous=False,
                                    )
                                if outcome.results.success:
                                    have_success = True
                                    r = {k: outcome.results.get(k) for k in results_sub}
                                    successful_results.append(r)
                                if (
                                    self.cleanup
                                    and self.settings.keep_optimal_run_dirs
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
                    title="Best Results",
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
