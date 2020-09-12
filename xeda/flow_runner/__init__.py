import copy
import multiprocessing
import os
import logging
import random
import time
from xeda.plugins.lwc import LwcCheckTimingHook
import pkg_resources
from pathlib import Path
import json
import multiprocessing as mp

from ..flows.settings import Settings
from ..flows.flow import DesignSource, Flow, my_print
from ..utils import load_class, dict_merge

logger = logging.getLogger()

def run_func(f: Flow):
    f.run()
    
class FlowRunner():
    @classmethod
    def register_subparser(cls, subparsers):
        raise NotImplementedError

    def __init__(self, args) -> None:
        self.args = args

    
    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string('xeda', "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            self.fatal(f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}")

    def fatal(self, msg):
        raise Exception(msg)

    def get_design_settings(self, json_path):

        settings = self.get_default_settings()

        try:
            with open(json_path) as f:
                design_settings = json.load(f)
                settings = dict_merge(settings, design_settings)
                logger.info(f"Using design settings from {json_path}")
        except FileNotFoundError as e:
            self.fatal(f'Cannot open design settings: {json_path}\n {e}')
        except IsADirectoryError as e:
            self.fatal(f'The specified design json is not a regular file.\n {e}')

        return settings

    # should not override
    def post_run(self, flow):



        # plugin_clss = [LwcSim]
        
        # for pcls in plugin_clss:
        #     assert issubclass(pcls, Plugin)
        #     # create plugin instances
        #     plugin = pcls(logger)
        #     if isinstance(plugin, PostRunPlugin):
        #         self.post_run_hooks.append(plugin.post_run_hook)
        #     if isinstance(plugin, PostResultsPlugin):
        #         self.post_results_hooks.append(plugin.post_results_hook)

        # Run post-run hooks
        for hook in flow.post_run_hooks:
            logger.info(f"Running post-run hook from from {hook.__self__.__class__.__name__}")
            hook(flow)

        flow.reports_dir = flow.run_dir / flow.reports_subdir_name
        if not flow.reports_dir.exists():
            flow.reports_dir.mkdir(parents=True)

        flow.results = dict()  # ???
        flow.parse_reports()
        flow.results['timestamp'] = flow.timestamp

        if flow.results:  # always non empty?
            flow.print_results()
            flow.dump_results()

        # Run post-results hooks
        for hook in flow.post_results_hooks:
            logger.info(f"Running post-results hook from {hook}")
            hook(flow)


    def setup_flow(self, settings, args, flow_name, max_threads=None):
        if not max_threads:
            max_threads = multiprocessing.cpu_count()
        # settings is a ref to dict and it's data can change, take a snapshot
        settings = copy.deepcopy(settings)

        try:
            flow_cls = load_class(flow_name, ".flows")
        except AttributeError as e:
            self.fatal(f"Could not find Flow class corresponding to {flow_name}. Make sure it's typed correctly.")
        
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
                raise Exception(f'Entry `{src}` in `sources` needs to be a string or a DesignSource JSON dictionary but is {type(src)}')
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

        return flow

class DefaultFlowRunner(FlowRunner):
    def launch(self):
        args = self.args

        json_path = args.design_json if args.design_json else Path.cwd() / 'design.json'

        settings = self.get_design_settings(json_path)

        flow = self.setup_flow(settings, args, args.flow)
        flow.run()
        self.post_run(flow)


# TODO as a plugin
class LwcVariantsRunner(DefaultFlowRunner):

    @classmethod
    def register_subparser(cls, subparsers):
        plug_parser = subparsers.add_parser('run_variants', help='Run All LWC variants in variants.json')
        plug_parser.add_argument('flow', metavar='FLOW_NAME', help=f'Flow name.')
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
        # TODO rename
        plug_parser.add_argument(
            '--gen-aead-timing',
            action='store_true',
            help='Generate AEAD timing results'
        )
        plug_parser.add_argument(
            '--gen-hash-timing',
            action='store_true',
            help='Generate HASH timing results'
        )
        plug_parser.add_argument(
            '--gen-aead-timing-path',
            help='Path for AEAD timing output cc'
        )
        #TODO implement
        # plug_parser.add_argument(
        #     '--variants_subset',
        #     help='Subset of variants to run'
        # )

    def launch(self):
        args = self.args
        self.parallel_run = args.parallel_run
        self.gen_aead_timing = args.gen_aead_timing
        self.gen_hash_timing = args.gen_hash_timing
        self.gen_aead_timing_path = args.gen_aead_timing_path
        logger.info(f"parallel_run={self.parallel_run}")

        total = 0
        num_success = 0

        variants_json = Path(args.variants_json).resolve()
        variants_json_dir = os.path.dirname(variants_json)

        with open(variants_json) as vjf:
            variants = json.load(vjf)

        flows_to_run = []

        nproc = max(1, multiprocessing.cpu_count() // 4)

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

            settings['design']['variant'] = variant_id 
            flow = self.setup_flow(settings, args, args.flow, max_threads=multiprocessing.cpu_count() // nproc // 2)
            
            if self.parallel_run:
                flow.set_parallel_run(None)

            flow.post_results_hooks.append(LwcCheckTimingHook(variant_id, variant_data, self.gen_aead_timing, self.gen_hash_timing))

            flows_to_run.append(flow)
        

        if self.parallel_run:

            with mp.Pool(processes=min(nproc, len(flows_to_run))) as p:
                p.map(run_func, flows_to_run)
        else:
            for flow in flows_to_run:
                flow.run()

        for flow in flows_to_run:
            self.post_run(flow)

            total += 1
            num_success += flow.results['success']

        logger.info(f'{num_success} out of {total} runs succeeded.')


class LwcFmaxRunner(FlowRunner):
    @classmethod
    def register_subparser(cls, subparsers):
        # command should be set automatically from top and using class help, etc
        plug_parser = subparsers.add_parser('run_fmax', help='find fmax')
        plug_parser.add_argument('flow', metavar='FLOW_NAME', help=f'Flow name.')
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
        small_improvement_threshold = 0.1
        # max successful runs after first success where improvements is < small_improvement
        max_small_improvements = 20
        wns_threshold = 0.002
        improvement_threshold = 0.002
        error_margin = 0.001
        ####
        failed_runs = 0
        num_small_improvements = 0
        best_period = None
        best_results = None
        best_rundir = None
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

        if args.start_period:
            next_period = args.start_period
        
        try:
            while True:

                if next_period:
                    assert next_period > 0.001
                    if best_period:
                        assert next_period < best_period
                    settings['flows'][flow_name]['clock_period'] = next_period


                flow = self.setup_flow(settings, args, flow_name)
                
                set_period = flow.settings.flow['clock_period']

                if set_period in tried_periods:
                    same_period += 1
                    if same_period > 5:
                        logger.warning(
                            f'[DSE] had already tried period={set_period} previously {same_period} times!')
                        break
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

                next_period = set_period - wns - error_margin -min(0.006, abs(wns) / 3 * random.random() )

                if success:
                    failed_runs = 0
                    if best_period:
                        if wns <= wns_threshold:
                            logger.warning(
                                f'[DSE] Stopping attempts as wns={wns} is lower than the flow\'s improvement threshold: {wns_threshold}')
                            break

                    if not best_period or period < best_period:
                        if best_period:
                            improvement = best_period - period
                        best_period = period
                        best_rundir = flow.run_dir
                        # deep copy
                        best_results = {**flow.results}

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
                    if best_period:
                        failed_runs += 1
                        next_period = (best_period + set_period) / 2

                        max_failed = self.args.max_failed_runs
                        if failed_runs >= max_failed:
                            logger.warning(
                                f'[DSE] Stopping attempts as number of FAILED runs has reached maximum allowed value of {max_failed}.'
                            )
                            break

                # worse or not worth it
                if best_period and (best_period - next_period) < improvement_threshold:
                    logger.warning(
                        f'[DSE] Stopping attempts as expected improvement of period is less than the improvement threshold of {improvement_threshold}.'
                    )
                    break


                logger.info(f'[DSE] best_period: {best_period}ns run_dir: {best_rundir}')
                logger.info(
                    f'[DSE] total_runs={total_runs} failed_runs={failed_runs} num_small_improvements={num_small_improvements} improvement={improvement} total time={time.monotonic() - state_time}')
        finally:

            logger.info(f'[DSE] best_period = {best_period}')
            logger.info(f'[DSE] best_rundir = {best_rundir}')
            logger.info(f'[DSE] total time = {int(time.monotonic() - state_time) // 60} minutes')
            logger.info(f'[DSE] total runs = {total_runs}')
            my_print(f'---- Results with optimal frequency: ----')
            flow.print_results(best_results)

            logger.info(f'Run directories: {" ".join([str(os.path.relpath(d, Path.cwd())) for d in rundirs])}')
            logger.info(f'Tried periods: {tried_periods}')
