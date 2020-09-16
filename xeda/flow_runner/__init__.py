import copy
import multiprocessing
from multiprocessing import cpu_count
from pebble.pool.process import ProcessPool
import os
import logging
import random
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
    except FlowFatalException as e:
        logger.critical(f'Fatal exception during flow run in {f.run_dir}: {e}')
        traceback.print_exc()
    except KeyboardInterrupt as e:
        logger.critical(f'KeyboardInterrupt recieved during flow run in {f.run_dir}: {e}')
        traceback.print_exc()
        # raise e?


class FlowRunner():
    @classmethod
    def register_subparser(cls, subparsers):
        raise NotImplementedError

    def __init__(self, args) -> None:
        self.args = args
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
    def post_run(self, flow: Flow):

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

    def setup_flow(self, settings, args, flow_name_or_class, max_threads=None):
        if not max_threads:
            max_threads = multiprocessing.cpu_count()
        # settings is a ref to a dict and its data can change, take a snapshot
        settings = copy.deepcopy(settings)

        if isinstance(flow_name_or_class, Flow):
            flow_cls = flow_name_or_class
        else:
            flow_cls = self.load_flow_class(flow_name_or_class)

        flow_settings = Settings()
        # default for optional design settings
        flow_settings.design['generics'] = {}
        flow_settings.design['tb_generics'] = {}

        # specific flow defaults
        flow_settings.flow.update(**flow_cls.default_settings)

        # override sections
        flow_settings.design.update(settings['design'])

        # override entire section if available in settings
        if flow_name_or_class in settings['flows']:
            flow_settings.flow.update(settings['flows'][flow_name_or_class])
            logger.info(f"Using {flow_name_or_class} settings")
        else:
            logger.warning(f"No settings found for {flow_name_or_class}")

        flow_settings.nthreads = max(1, max_threads)

        flow: Flow = flow_cls(flow_settings, args)

        # self.replicated_settings = []
        # for hook in self.replicator_hooks:
        #     repl_settings = hook(self.settings)
        #     logger.info(f'Generated {len(repl_settings)} setting(s) from {hook.__self__.__class__.__name__}')
        #     self.replicated_settings.extend(repl_settings)

        # for active_settings in flow.replicated_settings:
        #     print(2)
        #     flow.settings = active_settings

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
            flow.set_parallel_run(None)

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
class LwcVariantsRunner(DefaultFlowRunner):
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
            help='Use multiprocessing to run in parallel'
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
            '--no-timing',
            action='store_true',
            help='disable timing mode'
        )
        plug_parser.add_argument(
            '--variants-subset',
            nargs='+',
            help='The list of variant IDs to run from all available variants loaded from variants.json.'
        )

    def launch(self):
        args = self.args
        self.parallel_run = args.parallel_run

        if args.debug >= DebugLevel.MEDIUM:
            args.parallel_run = False
            logger.info("parallel_run disable due to the debug level")
        else:
            logger.info(f"parallel_run={self.parallel_run}")

        total = 0
        num_success = 0

        variants_json = Path(args.variants_json).resolve()
        variants_json_dir = os.path.dirname(variants_json)

        logger.info(f'LwcVariantsRunner: loading variants data from {variants_json}')
        with open(variants_json) as vjf:
            variants = json.load(vjf)

        if args.variants_subset:
            variants = {vid: vdat for vid, vdat in variants.items() if vid in args.variants_subset}

        flows_to_run = []

        nproc = max(1, multiprocessing.cpu_count() // 4)

        common_kats = ['kats_for_verification', 'generic_aead_sizes_new_key']
        if not args.no_reuse_key:
            common_kats += ['generic_aead_sizes_reuse_key']

        hash_kats = ['basic_hash_sizes', 'blanket_hash_test']

        def add_flow(settings, variant_id, variant_data):
            flow = self.setup_flow(settings, args, args.flow, max_threads=multiprocessing.cpu_count() // nproc // 2)
            if not args.no_timing:
                flow.post_results_hooks.append(LwcCheckTimingHook(variant_id, variant_data))
            flows_to_run.append(flow)

        for variant_id, variant_data in variants.items():
            logger.info(f"LwcVariantsRunner: running variant {variant_id}")
            # path is relative to variants_json

            design_json_path = Path(variants_json_dir) / variant_data["design"]  # TODO also support inline design
            settings = self.get_design_settings(design_json_path)

            if self.parallel_run:
                args.quiet = True

            if not args.no_timing:
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

        proc_timeout_seconds = 3600

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
    def __init__(self, period=None, results=None, rundir=None, wns=None):
        self.period = period
        self.wns = wns
        self.results = results
        self.rundir = rundir


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
            '--start-period',
            default=None, type=float,
            help='Starting clock period.'
        )

    def launch(self):
        merit_period = True

        small_improvement_threshold = 0.1
        # max successful runs after first success where improvements is < small_improvement
        max_small_improvements = 20
        wns_threshold = 0.001
        improvement_threshold = 0.002
        error_margin = 0.001
        ####
        failed_runs = 0
        num_small_improvements = 0

        class Best:
            def __init__(self, period=None, results=None, rundir=None, wns=None):
                self.period = period
                self.wns = wns
                self.results = results
                self.rundir = rundir
        best = Best()
        rundirs = []

        args = self.args

        json_path = args.design_json if args.design_json else Path.cwd() / 'design.json'

        settings = self.get_design_settings(json_path)

        next_period = None

        flow_name = args.flow

        total_runs = 0
        improvement = None

        state_time = time.monotonic()

        tried_periods = []
        same_period = 0

        success = None
        wns = None

        if args.start_period:
            next_period = args.start_period

        try:
            while True:

                if next_period:
                    assert next_period > 0.001
                    if best.period:
                        assert next_period < best.period
                    settings['flows'][flow_name]['clock_period'] = next_period

                flow = self.setup_flow(settings, args, flow_name)

                set_period = flow.settings.flow['clock_period']

                if set_period in tried_periods:
                    same_period += 1
                    if same_period > 5:
                        logger.warning(
                            f'[DSE] repeating periods for {same_period} times!')
                        break
                    if success:  # previous was success
                        next_period -= wns / 2 * random.random() - error_margin
                    else:
                        next_period += abs(wns) / 2 * random.random() - error_margin
                    continue
                else:
                    tried_periods.append(set_period)

                logger.info(f'[DSE] Trying clock_period = {set_period:0.3f}ns')
                # fresh directory for each run
                flow.run()
                total_runs += 1
                self.post_run(flow)

                rundirs.append(flow.run_dir)
                wns = flow.results['wns']
                success = flow.results['success'] and wns >= 0
                period = flow.results['clock_period']

                next_period = set_period - wns - error_margin - min(0.006, abs(wns) / 3 * random.random())

                if success:
                    failed_runs = 0

                    def merit():
                        if merit_period:
                            return best.period > period
                        else:
                            return best.period - best.wns > period - wns
                    has_merit = merit()
                    if not best.period or has_merit:
                        if best.period:
                            improvement = best.period - period
                            if wns <= wns_threshold:
                                logger.warning(
                                    f'[DSE] Stopping attempts as wns={wns} is lower than the flow\'s improvement threshold: {wns_threshold}')
                                break
                        best = Best(period, {**flow.results}, flow.run_dir, wns)

                        if improvement and improvement < small_improvement_threshold:
                            num_small_improvements += 1
                            if num_small_improvements > max_small_improvements:
                                logger.warning(
                                    f'[DSE] Number of improvements less than {small_improvement_threshold} reached {max_small_improvements}')

                                break
                        else:
                            # reset to 0?
                            num_small_improvements = max(0, num_small_improvements - 2)

                else:
                    if best.period:
                        failed_runs += 1
                        next_period = (best.period + set_period) / 2

                        max_failed = self.args.max_failed_runs
                        if failed_runs >= max_failed:
                            logger.warning(
                                f'[DSE] Stopping attempts as number of FAILED runs has reached maximum allowed value of {max_failed}.'
                            )
                            break

                # worse or not worth it
                if best.period and (best.period - next_period) < improvement_threshold:
                    logger.warning(
                        f'[DSE] Stopping attempts as expected improvement of period is less than the improvement threshold of {improvement_threshold}.'
                    )
                    break

                logger.info(f'[DSE] best.period: {best.period}ns run_dir: {best.rundir}')
                logger.info(
                    f'[DSE] total_runs={total_runs} failed_runs={failed_runs} num_small_improvements={num_small_improvements} improvement={improvement} total time={time.monotonic() - state_time}')
        finally:

            logger.info(f'[DSE] best.period = {best.period}')
            logger.info(f'[DSE] best.rundir = {best.rundir}')
            logger.info(f'[DSE] total time = {int(time.monotonic() - state_time) // 60} minutes')
            logger.info(f'[DSE] total runs = {total_runs}')
            my_print(f'---- Results with optimal frequency: ----')
            flow.print_results(best.results)

            logger.info(f'Run directories: {" ".join([str(os.path.relpath(d, Path.cwd())) for d in rundirs])}')
            logger.info(f'Tried periods: {tried_periods}')
