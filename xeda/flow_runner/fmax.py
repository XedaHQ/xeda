import copy
import os
from typing import Dict, Any, List
from types import SimpleNamespace
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool
import random
import logging
from concurrent.futures import CancelledError, TimeoutError
import time
from pathlib import Path
import json
import traceback
# a heavy overkill, but have plans with in the future
import numpy
from math import ceil

from pydantic.error_wrappers import ValidationError, display_errors

from ..utils import unique
from .default_runner import FlowRunner, print_results
from ..flows.design import Design, DesignError
from ..flows.flow_gen import FlowGen
from ..flows.flow import Flow, FlowFatalException, NonZeroExit


logger = logging.getLogger()


class Best:
    def __init__(self, freq, results, settings):
        self.freq = freq
        self.results = copy.deepcopy(results)
        self.settings = copy.deepcopy(settings)


def run_flow_fmax(arg):
    idx: int
    flow: Flow
    idx, flow = arg
    try:
        flow.run()
        flow.parse_reports()
        flow.dump_results()
        flow.results['timestamp'] = flow.timestamp
        flow.results['design.name'] = flow.settings.design['name']
        flow.results['flow.name'] = flow.name
        flow.results['flow.run_hash'] = flow.xedahash
        max_luts = flow.settings.flow.get('max_luts')
        lut = flow.results.get('lut')
        if max_luts and lut and int(lut) > int(max_luts):
            flow.results['exceeds_max_luts'] = True
            idx = None
        return idx, flow.results, flow.settings, flow.flow_run_dir

    except FlowFatalException as e:
        logger.warning(
            f'[Run Thread] Fatal exception during flow run in {flow.flow_run_dir}: {e}')
        traceback.print_exc()
        logger.warning(f'[Run Thread] Continuing')
    except KeyboardInterrupt as e:
        logger.exception(
            f'[Run Thread] KeyboardInterrupt received during flow run in {flow.flow_run_dir}')
        raise e
    except NonZeroExit as e:
        logger.warning(f'[Run Thread] {e}')
    except Exception as e:
        logger.exception(f"Exception: {e}")

    return None, None, flow.settings, flow.flow_run_dir


class FmaxRunner(FlowRunner):
    def __init__(self, args: SimpleNamespace, xeda_project: Dict[str, Any]) -> None:
        if args.force_run_dir:
            logger.warning("force_run_dir will be disabled in FmaxRunner")
            args.force_run_dir = None
        super().__init__(args, xeda_project)
        self.xeda_run_dir = self.args.xeda_run_dir / "fmax"


    def launch(self, flow_name: str, force_run: bool) -> List[Flow]:
        """
        runs the flow and returns the completed flow object
        """

        if force_run:
            logger.info(f"Forced re-run of {flow_name}")

        design_settings = self.get_design(self.selected_design)

        logger.info(f"design_settings={design_settings}")



        try:
            design: Design = Design(**design_settings)
        except ValidationError as e:
            errors = e.errors()
            raise DesignError(f"{len(errors)} errors while parsing `design` settings:\n\n{display_errors(errors)}\n") from None



        start_time = time.monotonic()

        flow_settings = self.flows.get(flow_name, {})
        
        flow_gen = FlowGen(self.flows)
        flow: Flow = flow_gen.generate(flow_name, ".", design, self.xeda_run_dir, completed_dependencies = [])


        # compatibility with previous key name
        lo_freq = float(flow_settings.get(
            'fmax_low', flow_settings.get('fmax_low_freq', 10.0)))
        hi_freq = float(flow_settings.get(
            'fmax_high', flow_settings.get('fmax_high_freq', 500.0)))

        flow_settings['fmax_low'] = lo_freq
        flow_settings['fmax_high'] = hi_freq

        assert lo_freq < hi_freq, "fmax_low_freq should be less than fmax_high_freq"
        resolution = 0.09
        delta_increment = resolution / 2

        ONE_THOUSAND = 1000.0

        nthreads = int(flow_settings.get('nthreads', 4))

        max_workers = max(2, args.max_cpus // nthreads)
        logger.info(f'nthreads={nthreads} num_workers={max_workers}')
        args.quiet = True

        best = None
        flow_run_dirs = []
        successful_results = []
        future = None
        num_iterations = 0
        pool = None
        no_improvements = 0

        previously_tried_frequencies = set()
        # can be different due to rounding errors, TODO only keep track of periods?
        previously_tried_periods = set()

        # TODO adaptive tweeking of timeout?
        proc_timeout_seconds = flow_settings.get('timeout', 3600)
        logger.info(f'[Fmax] Timeout set to: {proc_timeout_seconds} seconds.')

        error_retries = 0

        def round_freq_to_ps(freq: float) -> float:
            period = round(ONE_THOUSAND / freq, 3)
            return ONE_THOUSAND / period
        try:
            with ProcessPool(max_workers=max_workers) as pool:
                while hi_freq - lo_freq >= resolution:

                    finder_retries = 0
                    while True:
                        frequencies_to_try, freq_step = numpy.linspace(
                            lo_freq, hi_freq, num=max_workers, dtype=float, retstep=True)

                        frequencies_to_try = unique([round_freq_to_ps(
                            f) for f in frequencies_to_try if f not in previously_tried_frequencies])

                        clock_periods_to_try = []
                        frequencies = []
                        for freq in frequencies_to_try:
                            clock_period = round(ONE_THOUSAND / freq, 3)
                            if clock_period not in previously_tried_periods:
                                clock_periods_to_try.append(clock_period)
                                frequencies.append(freq)
                        frequencies_to_try = frequencies

                        min_required = (
                            max_workers - max(2, max_workers / 4)) if finder_retries > 20 else max_workers

                        if len(frequencies_to_try) >= max(1, min_required):
                            break
                        hi_freq += random.random() * delta_increment
                        min_lo_freq = best.freq + delta_increment if best else lo_freq
                        lo_freq = max(min_lo_freq, lo_freq -
                                      0.1 * random.random())
                        finder_retries += 1

                    logger.info(
                        f"[Fmax] Trying following frequencies (MHz): {[f'{freq:.2f}' for freq in frequencies_to_try]}")

                    # TODO Just keep clock_periods!
                    previously_tried_frequencies.update(frequencies_to_try)
                    previously_tried_periods.update(clock_periods_to_try)

                    flows_to_run = []
                    for clock_period in clock_periods_to_try:
                        flow_settings['clock_period'] = clock_period
                        flow_settings['nthreads'] = nthreads
                        flow = self.setup_flow(
                            flow_settings, design_settings, flow_name)

                        flow.no_console = True
                        flows_to_run.append(flow)

                    future = pool.map(run_flow_fmax, enumerate(
                        flows_to_run), timeout=proc_timeout_seconds)
                    num_iterations += 1

                    improved_idx = None

                    try:
                        iterator = future.result()
                        if not iterator:
                            logger.error("iterator is None! Retrying")
                            if error_retries < 4:
                                error_retries += 1
                                continue  # retry
                            else:
                                logger.error("error_retries > MAX, exiting")
                                break
                        while True:
                            try:
                                idx, results, fs, flow_run_dir = next(iterator)
                                flow_run_dirs.append(flow_run_dir)
                                if idx and results:
                                    freq = frequencies_to_try[idx]
                                    if results['success']:
                                        error_retries = 0
                                        r = {k: results.get(k) for k in (
                                            'clock_period', 'clock_frequency', 'wns', 'lut', 'ff', 'slice')}
                                        r['flow_run_dir'] = flow_run_dir
                                        successful_results.append(r)
                                        if not best or freq > best.freq:
                                            best = Best(freq, results, fs)
                                            improved_idx = idx
                            except StopIteration:
                                break  # inner while True
                            except TimeoutError as e:
                                logger.critical(
                                    f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks.")
                                future.cancel()
                            except ProcessExpired as e:
                                logger.critical(
                                    f"{e}. Exit code: {e.exitcode}")
                    except CancelledError:
                        logger.warning("[Fmax] CancelledError")
                    except KeyboardInterrupt:
                        pool.stop()
                        pool.join()
                        raise

                    if freq_step < resolution * 0.5:
                        logger.info(
                            f"Stopping: freq_step={freq_step} is below the limit")
                        break

                    if not best or improved_idx is None:
                        no_improvements += 1
                        if no_improvements > 4:
                            logger.info(
                                f"Stopping no viable frequencies found after {no_improvements} tries.")
                            break
                        logger.info(f"No improvements during this iteration.")
                        if not best:
                            hi_freq = lo_freq + resolution
                            shrink_factor = 0.7 + no_improvements
                            lo_freq /= shrink_factor
                        else:
                            if no_improvements > 2 and freq_step < no_improvements * resolution:
                                logger.info(
                                    f"Stopping as there were no improvements in {no_improvements} consecutive iterations.")
                                break
                            hi_freq = best.freq + 1.5 * \
                                max(freq_step, 1 + max_workers * resolution)
                            lo_freq = (lo_freq + best.freq) / 2
                    else:
                        lo_freq = best.freq + delta_increment + delta_increment * random.random()
                        no_improvements = 0
                        # last or one before last
                        if improved_idx >= (len(frequencies_to_try) // 2) or frequencies_to_try[-1] - best.freq <= freq_step:
                            min_plausible_period = (
                                ONE_THOUSAND / best.freq) - best.results['wns'] - 0.001
                            lo_point_choice = frequencies_to_try[1] if len(
                                frequencies_to_try) > 4 else frequencies_to_try[0]
                            hi_freq = max(best.freq + min(max_workers * 1.0, best.freq -
                                                          lo_point_choice),  ceil(ONE_THOUSAND / min_plausible_period))
                        else:
                            hi_freq = (hi_freq + best.freq + freq_step) / 2

                        hi_freq += 1

                    hi_freq = ceil(hi_freq)

                    logger.info(f'[Fmax] End of iteration #{num_iterations}')
                    logger.info(
                        f'[Fmax] Execution Time so far: {int(time.monotonic() - start_time) // 60} minute(s)')
                    if best and best.results:
                        print_results(best.results, title='Best so far', subset=[
                                      'clock_period', 'clock_frequency', 'wns', 'lut', 'ff', 'slice'])
                    logger.info(
                        f"Next iteration: [{lo_freq} ... {hi_freq}] resolution={resolution}")

        except KeyboardInterrupt:
            logger.exception('Received Keyboard Interrupt')
        except Exception as e:
            logger.exception(f'Received exception: {e}')
            traceback.print_exc()
        finally:
            if future and not future.cancelled():
                future.cancel()
            if pool:
                pool.close()
                pool.join()
            runtime_minutes = int(time.monotonic() - start_time) // 60
            if best:
                best.iterations = num_iterations
                best.runtime_minutes = runtime_minutes
                print_results(best.results, title='Best Results', subset=[
                    'clock_period', 'clock_frequency', 'lut', 'ff', 'slice'])
                best_json_path = Path(args.xeda_run_dir) / \
                    f'fmax_{settings["design"]["name"]}_{flow_name}_{self.timestamp}.json'
                logger.info(f"Writing best result to {best_json_path}")

                with open(best_json_path, 'w') as f:
                    json.dump(dict(best=best, successful_results=successful_results, flow_run_dirs=flow_run_dirs), f, default=lambda x: x.__dict__ if hasattr(
                        x, '__dict__') else str(x), indent=4)
            else:
                logger.warning("No successful results.")
            logger.info(
                f'[Fmax] Total Execution Time: {runtime_minutes} minute(s)')
            logger.info(f'[Fmax] Total Iterations: {num_iterations}')
