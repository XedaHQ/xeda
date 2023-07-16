import logging
import random
from typing import Any, Dict, List, Optional, Set, Union

from ...dataclass import validator
from ...utils import unique
from ..default_runner import settings_to_dict
from ..dse.dse_runner import FlowOutcome, Optimizer, deep_hash, linspace

log = logging.getLogger(__name__)


class FmaxOptimizer(Optimizer):
    default_variations: Dict[str, Dict[str, List[Any]]] = {
        "vivado_synth": {
            "synth.steps.synth_design.args.flatten_hierarchy": ["full"],
            # "synth.steps.synth_design.args.NO_LC": [False, True],
            "synth.strategy": [
                "Flow_AlternateRoutability",
                "Flow_AreaOptimized_high",
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
                "Area_ExploreWithRemap",
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

    class Settings(Optimizer.Settings):
        init_freq_low: float
        init_freq_high: float
        max_luts: Optional[int] = None
        init_num_variations: int = 1

        delta: float = 0.001
        resolution: float = 0.2
        min_freq_step: float = 0.02

        # min improvement inf frequency before increasing variations
        variation_min_improv: float = 2.0

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
        self.improved_idx: Optional[int] = None
        self.failed_fmax: Optional[float] = None

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
                self.last_improvement and self.last_improvement < self.settings.variation_min_improv
            ):
                self.num_variations = self.num_variations + 1
                log.info("Increased number of variations to %d", self.num_variations)

            elif (
                self.improved_idx > (self.max_workers + 1) // 2
                or self.last_improvement > 2 * self.settings.variation_min_improv
            ):
                if self.num_variations > 1:
                    self.num_variations -= 1
                    log.info("Decreased number of variations to to %d", self.num_variations)
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
                        self.hi_freq += ((max_workers + 1) * resolution) // self.num_variations
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

                self.lo_freq = self.failed_fmax / (self.no_improvements * random.random() + 1)
                self.hi_freq = (
                    self.lo_freq + max_workers * resolution * random.uniform(0.75, 1) + delta
                )

                log.info("Lowering bounds to [%0.2f, %0.2f]", self.lo_freq, self.hi_freq)
        else:  # -> improvement during last iteration
            # sanity check, best_freq was set before in case of a successful run
            assert best_freq, f"best_freq was None, while improved_idx={self.improved_idx}"

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
                self.hi_freq = (self.hi_freq + best_freq) / 2 + self.num_variations * resolution
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
            choice_max = round(((vlist_len - 1) * var + random.random()) / self.num_variations)
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
                self.hi_freq += random.uniform(self.settings.delta, self.settings.resolution) / 2

            frequencies, freq_step = linspace(
                self.lo_freq,
                self.hi_freq,
                n,
            )
            self.freq_step = freq_step
            batch_frequencies.extend(frequencies)

            remove_frequencies = []

            for freq in frequencies:
                clock_period = round(1000.0 / freq, 3)
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
                        hierarchical_keys=True,
                    ),
                }
                h = deep_hash(settings)
                if h in self.batch_hashes:
                    remove_frequencies.append(freq)
                else:
                    self.variation_choices.append(choice_indices)
                    batch_settings.append(settings)
                    self.batch_hashes.add(h)
                    if len(batch_settings) >= self.max_workers:
                        stop = True
                        break
            for freq in remove_frequencies:
                frequencies.remove(freq)
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
                    log.info("Flow #%d failed, but Fmax=%0.2f was suggested.", idx, fmax)
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
