import multiprocessing
import os
import logging
import pkg_resources
from pathlib import Path
import json
import sys
import multiprocessing as mp
import coloredlogs

from ..flows.flow import DesignSource, Flow
from ..utils import load_class, dict_merge

logger = logging.getLogger('flow_runner')
coloredlogs.install(level='DEBUG', fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

def run_func(f: Flow):
    f.run()
    
class FlowRunner():
    @classmethod
    def register_subparser(cls, subparsers):
        raise NotImplementedError

    def __init__(self, args) -> None:
        self.args = args
        #TODO
        self.parallel_run = True

    
    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string('xeda', "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            logger.critical(f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}")
            sys.exit(1)

    def get_design_settings(self, json_path):

        settings = self.get_default_settings()

        logger.info(f"Using design settings from {json_path}")

        try:
            with open(json_path) as f:
                design_settings = json.load(f)
                settings = dict_merge(settings, design_settings)
        except FileNotFoundError as e:
            sys.exit(f'Cannot open design settings: {json_path}\n {e}')
        except IsADirectoryError as e:
            sys.exit(f'The specified design json is not a regular file.\n {e}')

        return settings

    def post_run(self, flow):

        self.post_run_hooks = []
        self.post_results_hooks = []
        self.replicator_hooks = []

        # Run post-run hooks
        for hook in self.post_run_hooks:
            logger.info(f"Running post-run hook from from {hook.__self__.__class__.__name__}")
            hook(flow.run_dir, flow.settings)

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
        for hook in self.post_results_hooks:
            logger.info(f"Running post-results hook from {hook.__self__.__class__.__name__}")
            hook(flow.run_dir, flow.settings, flow.results)


    def setup_flow(self, settings, args, flow_name):
        try:
            flow_cls = load_class(flow_name, ".flows")
        except AttributeError as e:
            sys.exit(f"Could not find Flow class corresponding to {flow_name}. Make sure it's typed correctly.")
        flow: Flow = flow_cls(settings, args, logger)

        # for pcls in plugin_clss:
        #     assert issubclass(pcls, Plugin)
        #     # create plugin instances
        #     plugin = pcls(logger)
        #     if isinstance(plugin, ReplicatorPlugin):
        #         self.replicator_hooks.append(plugin.replicate_settings_hook)
        #     if isinstance(plugin, PostRunPlugin):
        #         self.post_run_hooks.append(plugin.post_run_hook)
        #     if isinstance(plugin, PostResultsPlugin):
        #         self.post_results_hooks.append(plugin.post_results_hook)

        # self.replicated_settings = []
        # for hook in self.replicator_hooks:
        #     repl_settings = hook(self.settings)
        #     logger.info(f'Generated {len(repl_settings)} setting(s) from {hook.__self__.__class__.__name__}')
        #     self.replicated_settings.extend(repl_settings)

        # for active_settings in flow.replicated_settings:
        #     print(2)
        #     flow.settings = active_settings

        if not isinstance(flow.settings.design['sources'], list):
            sys.exit('`sources` section of the settings needs to be a list')

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
                    # flow.logger.info(f'Converting generic `{gen_key}` marked as `file`: {p} -> {gen_val}')
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

    def launch(self):
        args = self.args

        total = 0
        num_success = 0

        variants_json = args.variants_json
        variants_json_dir = os.path.dirname(variants_json)

        with open(variants_json) as vjf:
            variants = json.load(vjf)

        flows_to_run = []

        nproc = max(1, multiprocessing.cpu_count() // 4)

        # TODO read logs from queue?
        queue = multiprocessing.Queue(-1)

        for variant_id, variant_data in variants.items():
            logger.info(f"LwcVariantsRunner: running variant {variant_id}")
            # path is relative to variants_json

            design_json_path = Path(variants_json_dir) / variant_data["design"]  # TODO also support inline design
            settings = self.get_design_settings(design_json_path)

            if self.parallel_run:
                args.quiet = True

            flow = self.setup_flow(settings, args, args.flow)
            flow.set_parallel_run(queue, nthreads_limit=multiprocessing.cpu_count() // nproc)

            flows_to_run.append(flow)
        

        if self.parallel_run:

            with mp.Pool(processes=nproc) as p:
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

    def launch(self):
        wns_threshold = 0.002
        improvement_threshold = 0.002
        failed_runs = 0
        best_period = None
        best_results = None
        best_rundir = None
        rundirs = set()

        args = self.args

        json_path = args.design_json if args.design_json else Path.cwd() / 'design.json'

        settings = self.get_design_settings(json_path)

        next_period = None

        while True:
            flow = self.setup_flow(settings, args, args.flow)
            
            
            if next_period:
                assert next_period > 0.001
                flow.settings.flow['clock_period'] = next_period
            set_period = flow.settings.flow['clock_period']
            logger.info(f'[DSE] Trying clock_period = {set_period:0.3f}ns')
            # fresh directory for each run
            flow.run()
            self.post_run(flow)

            rundirs.add(flow.run_dir)
            wns = flow.results['wns']
            success = flow.results['success'] and wns >= 0
            period = flow.results['clock_period']

            next_period = set_period - wns - improvement_threshold/4

            if success:
                failed_runs = 0
                if best_period:
                    if wns <= wns_threshold:
                        logger.warning(
                            f'[DSE] Stopping attempts as wns={wns} is lower than the flow\'s improvement threshold: {wns_threshold}')
                        break

                if not best_period or period < best_period:
                    best_period = period
                    best_rundir = flow.run_dir
                    best_results = {**flow.results}
            else:
                if best_period:
                    failed_runs += 1
                    next_period = (best_period + set_period) / 2 - improvement_threshold/2

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

        logger.info(f'[DSE] best_period = {best_period}')
        logger.info(f'[DSE] best_rundir = {best_rundir}')
        print(f'---- Results with optimal frequency: ----')
        flow.print_results(best_results)

        logger.info(f'Run directories: {" ".join([str(os.path.relpath(d, Path.cwd())) for d in rundirs])}')
