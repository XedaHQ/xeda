import copy
import multiprocessing
from multiprocessing import cpu_count
from typing import List
from pebble.common import ProcessExpired
from pebble.pool.process import ProcessPool
import os
import logging
from concurrent.futures import TimeoutError
import time
import pkg_resources
from pathlib import Path
import json
import multiprocessing as mp
import traceback
# heavy, but will probably become handy down the road
import numpy

from ..debug import DebugLevel
from ..plugins.lwc import LwcCheckTimingHook
from ..flows.settings import Settings
from ..flows.flow import DesignSource, Flow, FlowFatalException, my_print
from ..utils import load_class, dict_merge, try_convert

logger = logging.getLogger()


def run_flow(f: Flow):
    try:
        f.run()
        return f.results
    except FlowFatalException as e:
        logger.critical(f'Fatal exception during flow run in {f.run_dir}: {e}')
        traceback.print_exc()
    except KeyboardInterrupt as e:
        logger.critical(f'KeyboardInterrupt recieved during flow run in {f.run_dir}: {e}')
        traceback.print_exc()


def run_flow_fmax(arg):
    idx, f = arg
    try:
        f.run()
        return idx
    except FlowFatalException as e:
        logger.critical(f'Fatal exception during flow run in {f.run_dir}: {e}')
        traceback.print_exc()
    except KeyboardInterrupt as e:
        logger.critical(f'KeyboardInterrupt recieved during flow run in {f.run_dir}: {e}')
        traceback.print_exc()


class FlowRunner():
    @classmethod
    def register_subparser(cls, subparsers):
        raise NotImplementedError

    def __init__(self, args, timestamp) -> None:
        self.args = args
        self.timestamp = timestamp
        # in case super().add_common_args(plug_parser) was not called in a subclass
        if not hasattr(args, 'override_settings'):
            self.args.override_settings = None

        self.parallel_run = None

    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string('xeda', "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            self.fatal(f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}", e)

    def fatal(self, msg, exception=None):
        logger.critical(msg)
        if exception:
            raise exception
        else:
            raise Exception(msg)

    def validate_settings(self, settings):
        assert 'design' in settings
        design = settings['design']
        assert 'sources' in design
        assert 'vhdl_std' in design
        if design['vhdl_std'] == 8:
            design['vhdl_std'] = "08"
        elif design['vhdl_std'] == 2:
            design['vhdl_std'] = "02"

        return settings

    def get_design_settings(self, json_path=None):
        if not json_path:
            json_path = self.args.design_json if self.args.design_json else Path.cwd() / 'design.json'

        settings = self.get_default_settings()

        try:
            with open(json_path) as f:
                design_settings = json.load(f)
                settings = dict_merge(settings, design_settings)
                logger.info(f"Using design settings from {json_path}")
        except FileNotFoundError as e:
            self.fatal(
                f'Cannot open default design settings path: {json_path}. Please specify correct path using --design-json', e)
        except IsADirectoryError as e:
            self.fatal(f'The specified design json is not a regular file.', e)

        if self.args.override_settings:
            for override in self.args.override_settings:
                key, val = override.split('=')
                hier = key.split('.')
                patch = dict()
                current_dict = patch
                for field in hier[:-1]:
                    new_dict = dict()
                    current_dict[field] = new_dict
                    current_dict = new_dict
                print(f'val={val}')
                current_dict[hier[-1]] = try_convert(val, convert_lists=True)
                settings = dict_merge(settings, patch, True)

        return self.validate_settings(settings)

    # should not override
    def post_run(self, flow: Flow, print_failed=True):
        # Run post-run hooks
        for hook in flow.post_run_hooks:
            logger.info(f"Running post-run hook from from {hook.__self__.__class__.__name__}")
            hook(flow)

        flow.reports_dir = flow.run_dir / flow.reports_subdir_name
        if not flow.reports_dir.exists():
            flow.reports_dir.mkdir(parents=True)

        flow.parse_reports()
        flow.results['timestamp'] = flow.timestamp
        flow.results['design.name'] = flow.settings.design['name']
        flow.results['flow.name'] = flow.name
        flow.results['flow.run_hash'] = flow.run_hash

        if print_failed or flow.results.get('success'):
            flow.print_results()
        flow.dump_results()

        # Run post-results hooks
        for hook in flow.post_results_hooks:
            logger.info(f"Running post-results hook from {hook}")
            hook(flow)

    def load_flow_class(self, flow_name):
        try:
            return load_class(flow_name, ".flows")
        except AttributeError as e:
            self.fatal(f"Could not find Flow class corresponding to {flow_name}. Make sure it's typed correctly.", e)

    def setup_flow(self, settings, args, flow_name, max_threads=None):
        if not max_threads:
            max_threads = multiprocessing.cpu_count()
        # settings is a ref to a dict and its data can change, take a snapshot
        settings = copy.deepcopy(settings)

        # def is_flow_class(cls):
        #     try:
        #         return issubclass(flow_name, Flow)
        #     except:
        #         return False

        flow_cls = self.load_flow_class(flow_name)

        flow_settings = Settings()
        # default for optional design settings
        flow_settings.design['generics'] = {}
        flow_settings.design['tb_generics'] = {}

        # specific flow defaults
        flow_settings.flow.update(**flow_cls.default_settings)

        # override sections
        flow_settings.design.update(settings['design'])

        # override entire section if available in settings
        if flow_name in settings['flows']:
            flow_settings.flow.update(settings['flows'][flow_name])
            logger.info(f"Using {flow_name} settings")
        else:
            logger.warning(f"No settings found for {flow_name}")

        flow_settings.nthreads = max(1, max_threads)

        flow: Flow = flow_cls(flow_settings, args)

        if not isinstance(flow.settings.design['sources'], list):
            self.fatal('`sources` section of the settings needs to be a list')

        for i, src in enumerate(flow.settings.design['sources']):
            if isinstance(src, str):
                src = {"file": src}
            if not DesignSource.is_design_source(src):
                raise Exception(
                    f'Entry `{src}` in `sources` needs to be a string or a DesignSource JSON dictionary but is {type(src)}')
            flow.settings.design['sources'][i] = DesignSource(**src).mk_relative(flow.run_dir)

        for gen_type in ['generics', 'tb_generics']:
            for gen_key, gen_val in flow.settings.design[gen_type].items():
                if isinstance(gen_val, dict) and "file" in gen_val:
                    p = gen_val["file"]
                    assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
                    gen_val = flow.conv_to_relative_path(p.strip())
                    logger.info(f'Converting generic `{gen_key}` marked as `file`: {p} -> {gen_val}')
                    flow.settings.design[gen_type][gen_key] = gen_val

        # flow.check_settings()
        flow.dump_settings()

        if self.parallel_run:
            flow.set_parallel_run()

        return flow

    def add_common_args(parser):
        # TODO add list of supported flows in help
        parser.add_argument('flow', metavar='FLOW_NAME', help=f'Flow name.')
        parser.add_argument('--override-settings', nargs='+',
                            help='Override certain setting value. Use <hierarchy>.key=value format'
                            'example: --override-settings flows.vivado_run.stop_time=100us')


class DefaultFlowRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        run_parser = subparsers.add_parser('run', help='Run a flow')
        super().add_common_args(run_parser)
        run_parser.add_argument(
            '--design-json',
            help='Path to design JSON file.'
        )

    def launch(self):
        args = self.args
        settings = self.get_design_settings()
        flow = self.setup_flow(settings, args, args.flow)
        flow.run()
        self.post_run(flow)


# TODO as a plugin
class LwcVariantsRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        plug_parser = subparsers.add_parser('run_variants', help='Run All LWC variants in variants.json')
        super().add_common_args(plug_parser)
        plug_parser.add_argument(
            '--variants-json',
            default='variants.json',
            help='Path to LWC variants JSON file.'
        )
        # TODO optionally get nproc from user
        plug_parser.add_argument(
            '--parallel-run',
            action='store_true',
            help='Use multiprocessing to run multiple flows in parallel'
        )
        plug_parser.add_argument(
            '--gmu-kats',
            action='store_true',
            help='Run simulation with different GMU KAT files'
        )
        plug_parser.add_argument(
            '--auto-copy',
            action='store_true',
            help='In gmu-kats mode, automatically copy files. Existing files will be silently REPLACED, so use with caution!'
        )
        plug_parser.add_argument(
            '--no-reuse-key',
            action='store_true',
            help='Do not inlucde reuse-key testvectors'
        )
        plug_parser.add_argument(
            '--variants-subset',
            nargs='+',
            help='The list of variant IDs to run from all available variants loaded from variants.json.'
        )

    def launch(self):
        args = self.args
        self.parallel_run = args.parallel_run
        if args.parallel_run and args.debug >= DebugLevel.MEDIUM:
            self.parallel_run = False
            logger.warning("parallel_run disabled due to the debug level")

        total = 0
        num_success = 0

        variants_json = Path(args.variants_json).resolve()
        variants_json_dir = os.path.dirname(variants_json)

        logger.info(f'LwcVariantsRunner: loading variants data from {variants_json}')
        with open(variants_json) as vjf:
            variants = json.load(vjf)

        if args.variants_subset:
            variants = {vid: vdat for vid, vdat in variants.items() if vid in args.variants_subset}

        flows_to_run: List[Flow] = []

        nproc = max(1, multiprocessing.cpu_count() // 4)

        common_kats = ['kats_for_verification', 'generic_aead_sizes_new_key']
        if not args.no_reuse_key:
            common_kats += ['generic_aead_sizes_reuse_key']

        hash_kats = ['basic_hash_sizes', 'blanket_hash_test']

        def add_flow(settings, variant_id, variant_data):
            flow = self.setup_flow(settings, args, args.flow, max_threads=multiprocessing.cpu_count() // nproc // 2)
            flow.post_results_hooks.append(LwcCheckTimingHook(variant_id, variant_data))
            flows_to_run.append(flow)

        for variant_id, variant_data in variants.items():
            logger.info(f"LwcVariantsRunner: running variant {variant_id}")
            # path is relative to variants_json

            design_json_path = Path(variants_json_dir) / variant_data["design"]  # TODO also support inline design
            settings = self.get_design_settings(design_json_path)

            if self.parallel_run:
                args.quiet = True

            settings['design']['tb_generics']['G_TEST_MODE'] = 4
            settings['design']['tb_generics']['G_FNAME_TIMING'] = f"timing_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_TIMING_CSV'] = f"timing_{variant_id}.csv"
            settings['design']['tb_generics']['G_FNAME_RESULT'] = f"result_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_FAILED_TVS'] = f"failed_test_vectors_{variant_id}.txt"
            settings['design']['tb_generics']['G_FNAME_LOG'] = f"lwctb_{variant_id}.log"

            if args.gmu_kats:
                kats = common_kats
                if "HASH" in variant_data["operations"]:
                    kats += hash_kats
                for kat in kats:
                    settings["design"]["tb_generics"]["G_FNAME_DO"] = {"file": f"KAT_GMU/{variant_id}/{kat}/do.txt"}
                    settings["design"]["tb_generics"]["G_FNAME_SDI"] = {"file": f"KAT_GMU/{variant_id}/{kat}/sdi.txt"}
                    settings["design"]["tb_generics"]["G_FNAME_PDI"] = {"file": f"KAT_GMU/{variant_id}/{kat}/pdi.txt"}

                    this_variant_data = copy.deepcopy(variant_data)
                    this_variant_data["kat"] = kat
                    add_flow(settings, variant_id, this_variant_data)
            else:
                add_flow(settings, variant_id, variant_data)

        if not flows_to_run:
            self.fatal("flows_to_run is empty!")

        proc_timeout_seconds = flows_to_run[0].settings.flow.get('timeout')
        if not proc_timeout_seconds:
            proc_timeout_seconds = flows_to_run[0].timeout

        if self.parallel_run:
            try:
                with mp.Pool(processes=min(nproc, len(flows_to_run))) as p:
                    p.map_async(run_flow, flows_to_run).get(proc_timeout_seconds)
            except KeyboardInterrupt as e:
                logger.critical(f'KeyboardInterrupt recieved parallel execution of runs: {e}')
                traceback.print_exc()
                logger.warning("trying to recover completed flow results...")
        else:
            for flow in flows_to_run:
                flow.run()

        try:
            for flow in flows_to_run:
                self.post_run(flow)
                total += 1
                if flow.results.get('success'):
                    num_success += 1
        except Exception as e:
            logger.critical("Exception during post_run")
            raise e
        finally:
            for flow in flows_to_run:
                logger.info(f"Run: {flow.run_dir} {'[PASS]' if flow.results.get('success') else '[FAIL]'}")
            logger.info(f'{num_success} out of {total} runs succeeded.')


class Best:
    def __init__(self, freq, results):
        self.freq = freq
        self.results = results


class LwcFmaxRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        # command should be set automatically from top and using class help, etc
        plug_parser = subparsers.add_parser('run_fmax', help='find fmax')
        super().add_common_args(plug_parser)
        plug_parser.add_argument(
            '--design-json',
            help='Path to design JSON file.'
        )
        plug_parser.add_argument(
            '--max-failed-runs',
            default=10, type=int,
            help='Maximum consequetive failed runs allowed. Give up afterwards.'
        )
        plug_parser.add_argument(
            '--start-max-freq',
            default=600, type=float,
        )
        plug_parser.add_argument(
            '--max-cpus',
            default=max(1, cpu_count()), type=int,
        )

    def launch(self):
        start_time = time.monotonic()

        args = self.args
        settings = self.get_design_settings()

        flow_name = args.flow

        flow_settings = settings['flows'].get(flow_name)

        # won't try lower
        lo_freq = 4.0
        # can go higher
        hi_freq = flow_settings.get('hi_freq')
        if not hi_freq:
            hi_freq = 200.0
        accuracy = 0.1
        delta_increment = 0.05

        Mega = 1000.0
        # TODO get from settings/args
        nthreads = 4
        num_workers = max(2, args.max_cpus // nthreads)
        self.parallel_run = True
        args.quiet = True

        best = None
        rundirs = []
        all_results = []
        future = None
        num_iterations = 0
        try:
            with ProcessPool(max_workers=num_workers) as pool:
                while hi_freq - lo_freq >= accuracy:
                    frequencies_to_try, freq_step = numpy.linspace(
                        lo_freq, hi_freq, num=num_workers, dtype=float, retstep=True)

                    logger.info(f"trying frequencies: {frequencies_to_try} MHz")

                    flows_to_run = []
                    for freq in frequencies_to_try:
                        flow_settings['clock_period'] = Mega / freq
                        flow = self.setup_flow(settings, args, flow_name, max_threads=nthreads)
                        flow.set_parallel_run()
                        flows_to_run.append(flow)

                    proc_timeout_seconds = flow_settings.get('timeout')
                    if not proc_timeout_seconds:
                        proc_timeout_seconds = flows_to_run[0].timeout

                    logger.info(f'Timeout set to: {proc_timeout_seconds} seconds.')

                    future = pool.map(run_flow_fmax, enumerate(flows_to_run), timeout=proc_timeout_seconds)
                    num_iterations += 1

                    iterator = future.result()
                    improved_idx = None
                    while True:
                        try:
                            idx = next(iterator)
                            flow = flows_to_run[idx]
                            freq = frequencies_to_try[idx]
                            self.post_run(flow, print_failed=False)
                            results = flow.results
                            rundirs.append(flow.run_dir)
                            if results['success'] and (not best or freq > best.freq):
                                all_results.append(results)
                                best = Best(freq, results)
                                improved_idx = idx
                        except StopIteration:
                            break
                        except TimeoutError as e:
                            logger.critical(
                                f"Flow run took longer than {e.args[1]} seconds. Cancelling remaining tasks.")
                            future.cancel()
                        except ProcessExpired as e:
                            logger.critical(f"{e}. Exit code: {e.exitcode}")
                    if not best or improved_idx is None:
                        break
                    if freq_step < accuracy:
                        break
                    lo_freq = best.freq + delta_increment
                    # last or one before last
                    if improved_idx == num_workers - 1 or frequencies_to_try[-1] - best.freq <= freq_step:
                        min_plausible_period = (Mega / best.freq) - best.results['wns']
                        hi_freq = max(frequencies_to_try[-1] + freq_step,  Mega / min_plausible_period) + accuracy / 2
                    else:
                        hi_freq = frequencies_to_try[improved_idx + 1] + accuracy

                    logger.info(f'[DSE] Execution Time: {int(time.monotonic() - start_time) // 60} minutes')
                    logger.info(f'[DSE] Number of Iterations: {num_iterations}')

        except KeyboardInterrupt:
            logger.exception('Received Keyboard Interrupt')
            if future and not future.cancelled():
                future.cancel()
        except:
            logger.exception('Received exception')
            raise
        finally:
            logger.info(f'[DSE] best = {best}')
            logger.info(f'[DSE] Total Execution Time: {int(time.monotonic() - start_time) // 60} minutes')
            logger.info(f'[DSE] Total Iterations: {num_iterations}')

            best_json_path = Path(args.xeda_run_dir) / \
                f'fmax_{settings["design"]["name"]}_{flow_name}_{self.timestamp}.json'
            logger.info(f"Writing best result to {best_json_path}")
            with open(best_json_path, 'w') as f:
                json.dump(best, f, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else str(x), indent=4)
            if future and not future.cancelled():
                future.cancel()
