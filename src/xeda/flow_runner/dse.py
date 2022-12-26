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
from attrs import define
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool

from ..dataclass import Field, XedaBaseModel, validator
from ..design import Design
from ..flow import Flow, FlowFatalError
from ..tool import NonZeroExitCode
from ..utils import Timer, dump_json, load_class, unique
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

    def next_batch(self) -> Union[None, List[Dict[str, Any]]]:
        ...

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        ...
        return True


ONE_THOUSAND = 1000.0


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


default_variations: Dict[str, Dict[str, List[Any]]] = {
    "vivado_synth": {
        "synth.steps.synth_design.args.flatten_hierarchy": ["full"],
        # "synth.steps.synth_design.args.NO_LC": [False, True],
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
        init_num_variations: int = 1

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
        self.last_improvement: float = 0.0
        self.num_iterations: int = 0
        self.last_best_freq: float = 0

        # TODO: duplicate
        self.batch_hashes: Set[int] = set()

        # array of {key -> choice} choices, indexed by flow idx
        self.variation_choices: List[Dict[str, int]] = []

        assert isinstance(self.settings, self.Settings)
        assert self.settings.init_freq_high > self.settings.init_freq_low
        self.hi_freq = self.settings.init_freq_high
        self.lo_freq = self.settings.init_freq_low
        self.num_variations = self.settings.init_num_variations
        assert self.settings.resolution > 0.0

    @staticmethod
    def get_result_value(res):
        return res.get("Fmax")

    @property
    def best_freq(self) -> Optional[float]:
        return self.get_result_value(self.best.results) if self.best else None

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
            if self.improved_idx is None or (
                self.last_improvement
                and self.last_improvement < self.settings.variation_min_improv
            ):
                self.num_variations = self.num_variations + 1
                log.info("Increased number of variations to %d", self.num_variations)

            elif (
                self.improved_idx > (self.max_workers + 1) // 2
                or self.last_improvement > 2 * self.settings.variation_min_improv
            ):
                if self.num_variations > 1:
                    self.num_variations -= 1
                    log.info(
                        "Decreased number of variations to to %d", self.num_variations
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
                    if self.num_variations > 1 and self.no_improvements < 3:
                        self.hi_freq += (
                            (max_workers + 1) * resolution
                        ) // self.num_variations
                    else:
                        # no variations or too many failures, just binary search
                        self.hi_freq = (self.hi_freq + best_freq) / 2 + delta
                        log.info("Lowering hi_freq to %0.2f", self.hi_freq)
                else:
                    self.hi_freq = best_freq + self.num_variations * resolution
                    log.warning(
                        "No Improvements, but still incrementing hi_freq to %0.2f (%d variations)",
                        self.hi_freq,
                        self.num_variations,
                    )
            else:
                if self.hi_freq <= resolution:
                    log.warning("hi_freq < resolution")
                    return False
                if not self.failed_fmax:
                    log.error(
                        "All runs in the previous iteration failed without reporting an Fmax! Please check the flow's logs to determine the reason."
                    )
                    return False

                self.lo_freq = self.failed_fmax / (
                    self.no_improvements * random.random() + 1
                )
                self.hi_freq = (
                    self.lo_freq
                    + max_workers * resolution * random.uniform(0.75, 1)
                    + delta
                )

                log.info(
                    "Lowering bounds to [%0.2f, %0.2f]", self.lo_freq, self.hi_freq
                )
        else:  # -> improvement during last iteration
            # sanity check, best_freq was set before in case of a successful run
            assert (
                best_freq
            ), f"best_freq was None, while improved_idx={self.improved_idx}"

            # reset no_improvements
            self.no_improvements = 0

            if self.last_best_freq:
                # sanity check
                assert (
                    best_freq >= self.last_best_freq
                ), f"best_freq={best_freq} < last_best_freq={self.last_best_freq}"

                self.last_improvement = best_freq - self.last_best_freq
                log.info(
                    "Fmax improvement during previous iteration: %0.2f MHz",
                    self.last_improvement,
                )
            self.last_best_freq = best_freq

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

    def next_batch(self) -> Union[None, List[Dict[str, Any]]]:
        assert isinstance(self.settings, self.Settings)

        if not self.update_bounds():
            return None

        n = self.max_workers
        if self.num_variations > 1:
            log.info("Generating %d variations", self.num_variations)
            n = (n + self.num_variations - 1) // self.num_variations  # ceiling

        if self.hi_freq <= 0 or self.lo_freq < 0:
            log.warning(
                "hi_freq(%0.2f) or lo_freq(%0.2f) were not positive!",
                self.hi_freq,
                self.lo_freq,
            )
            return None

        def rand_choice(vlist_len: int, var: int) -> int:
            # var is 1...self.num_variations, inclusive
            if self.num_variations <= 1 or vlist_len == 1:
                return 0
            choice_max = round(
                ((vlist_len - 1) * var + random.random()) / self.num_variations
            )
            return random.randrange(0, min(vlist_len - 1, choice_max) + 1)

        base_settings = dict(self.base_settings)
        max_var = 0
        stop = False
        batch_settings: List[Dict[str, Any]] = []
        batch_frequencies: List[float] = []
        while not stop:
            max_var += 1
            if max_var > self.num_variations:
                self.lo_freq += random.random() * self.settings.delta / 2
                self.hi_freq += (
                    random.uniform(self.settings.delta, self.settings.resolution) / 2
                )

            frequencies, freq_step = linspace(
                self.lo_freq,
                self.hi_freq,
                n,
            )
            self.freq_step = freq_step
            batch_frequencies.extend(frequencies)

            for freq in frequencies:
                clock_period = round(ONE_THOUSAND / freq, 3)
                choice_indices = {}
                variations = {}
                for k, v in self.variations.items():
                    if v:
                        choice = rand_choice(len(v), max_var)
                        choice_indices[k] = choice
                        variations[k] = v[choice]
                settings = {
                    **base_settings,
                    "clock_period": clock_period,
                    **settings_to_dict(
                        variations,
                        expand_dict_keys=True,
                    ),
                }
                h = deep_hash(settings)
                if h not in self.batch_hashes:
                    self.variation_choices.append(choice_indices)
                    batch_settings.append(settings)
                    self.batch_hashes.add(h)
                    if len(batch_settings) >= self.max_workers:
                        stop = True
                        break
        batch_frequencies = sorted(unique(batch_frequencies))
        log.info(
            "Trying following frequencies (MHz): %s",
            ", ".join(f"{freq:.2f}" for freq in batch_frequencies),
        )
        self.improved_idx = None
        self.num_iterations += 1
        return batch_settings

    def process_outcome(self, outcome: FlowOutcome, idx: int) -> bool:
        """returns True if this was the best result so far"""
        assert isinstance(self.settings, self.Settings)

        best_freq = self.best_freq

        fmax = self.get_result_value(outcome.results)

        if fmax and not outcome.results.success:
            if not best_freq or fmax > best_freq:
                # Failed due to negative slack
                # Keep the Fmax for next iter, if no other runs succeeded
                if not self.failed_fmax or fmax > self.failed_fmax:
                    log.info(
                        "Flow #%d failed, but Fmax=%0.2f was suggested.", idx, fmax
                    )
                    self.failed_fmax = fmax
            return False

        if fmax is None:
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
                    fmax,
                )
                return False

        def promote(lst: List[Any], idx: int):
            if idx > 0 and lst:
                lst.insert(0, lst.pop(idx))

        if best_freq is None or fmax > best_freq:
            log.info(
                "New maximum frequency: %0.2f MHz",
                fmax,
            )
            self.best = outcome
            self.base_settings = outcome.settings
            self.improved_idx = idx
            if self.num_variations > 1:
                var_choices = self.variation_choices[idx]
                for k, i in var_choices.items():
                    promote(self.variations[k], i)
            return True
        else:
            log.debug(
                "Reported Fmax (%0.2f) is lower than the current best (%0.2f)",
                fmax,
                best_freq,
            )
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
                ),  # type: ignore
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

        if self.settings.variations is not None:
            optimizer.variations = self.settings.variations
        else:
            optimizer.variations = default_variations[flow_class.name]

        possible_variations = 1
        for v in optimizer.variations.values():
            if v:
                possible_variations *= len(v)

        log.info("Number of possible setting variations: %d", possible_variations)

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

        flow_setting_hashes: Set[int] = set()

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
                                            design=design,
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
