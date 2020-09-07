# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import sys
import os
import argparse
import json
from pathlib import Path
from xeda.flow_runner import DefaultFlowRunner, LwcVariantsRunner

from .utils import load_class

import coloredlogs
import logging

import pkg_resources


try:
    __version__ = pkg_resources.get_distribution(__package__).version
except pkg_resources.DistributionNotFound:
    __version__ = '(N/A - Local package)'



class XedaApp:
    def __init__(self):
        self.parser = argparse.ArgumentParser(
            prog=__package__,
            description=f'{__package__}: Simulate And Synthesize Hardware! Version {__version__}')
        self.args = None
        self.logger = logging.getLogger(__package__)

    # TODO
    def check_settings(self):
        if "design" in self.settings:
            ds = self.settings["design"]
            assert "generics" not in ds or isinstance(ds["generics"], dict), "design.generics must be a dict"
            assert "tbgenerics" not in ds or isinstance(
                ds["tb_generics"], dict), "design.tb_generics must be a dict"

    def main(self):
        args = self.args = self.parse_args()

        coloredlogs.install(level='DEBUG' if args.debug else 'INFO',
                            fmt='%(asctime)s %(levelname)s %(message)s', logger=self.logger)

        if args.command == 'run':
            runner = DefaultFlowRunner(self.logger, self.args)
        elif args.command == 'run_variants':
            runner = LwcVariantsRunner(self.logger, self.args)
        else:
            sys.exit(f"Ruuner for {args.command} is not implemented")

        runner.run_flow()


    #TODO FIXME
    def register_plugin_parsers(self):
        plug_parser = self.subparsers.add_parser('run_variants', help='Run All LWC variants in variants.json')
        plug_parser.add_argument('flow', metavar='SUITE_NAME[:FLOW_NAME]', help=f'Flow name.')
        plug_parser.add_argument(
            '--variants-json',
            default='variants.json',
            help='Path to LWC variants JSON file.'
        )
        

    def parse_args(self, args=None):
        parser = self.parser
        parser.add_argument(
            '--debug',
            action='store_true',
            help='Print debug info'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Be verbose. Print everything to stdout'
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            help="Be as quiet as possible. Never print out output from command executions"
        )
        parser.add_argument(
            '--force-run-dir',
            # help='Force set run directory where the tools are run.',
            # default=None
        )
        parser.add_argument(
            '--all-runs-dir',
            # help='Change top directory where the all the runs of a flow is run from `flow/suite_run` ',
            # default=None
        )

        subparsers = parser.add_subparsers(dest='command', help='Commands Help')
        subparsers.required = True
        self.subparsers = subparsers

        # TODO FIXME add as validator!
        registered_flows = []
        ### FIXME FIXME FIXME

        self.register_plugin_parsers()


        ############################
        run_parser = subparsers.add_parser('run', help='Run a flow')
        run_parser.add_argument('flow', metavar='SUITE_NAME[:FLOW_NAME]',
                                help=f'Flow name. Supported flows are: {registered_flows}')
        run_parser.add_argument(
            '--design-json',
            help='Path to design JSON file.'
        )
        ############################
        fmax_parser = subparsers.add_parser(
            'dse', help='Design Space Exploration: Run `synth` flow of a suite several times, sweeping over clock_period constraint to find the maximum frequency of the design for the current settings')
        fmax_parser.add_argument('flow', metavar='SUITE_NAME[:FLOW_NAME]',
                                 help=f'Name of the suite to execute. Supported flows are: {registered_flows}')
        fmax_parser.add_argument('--max-failed-runs', type=int, default=40,
                                 help=f'Maximum number of consecutive runs that did not improve F_max. Search stops afterwards')

        return parser.parse_args(args)

    def find_fmax(self):
        wns_threshold = 0.002
        improvement_threshold = 0.002
        failed_runs = 0
        best_period = None
        best_results = None
        best_rundir = None
        rundirs = set()

        suite, flow_name = self.get_suite_flow(flow_name='synth')
        while True:

            set_period = suite.settings.flow['clock_period']
            self.logger.info(f'[DSE] Trying clock_period = {set_period:0.3f}ns')
            # fresh directory for each run
            suite.run('synth')
            rundirs.add(suite.run_dir)
            wns = suite.results['wns']
            success = suite.results['success'] and wns >= 0
            period = suite.results['clock_period']

            next_period = set_period - wns - improvement_threshold/4

            if success:
                if best_period:
                    # if wns < wns_threshold:
                    #     self.logger.warning(
                    #         f'[DSE] Stopping attempts as wns={wns} is lower than the flow\'s improvement threshold: {wns_threshold}')
                    #     break
                    max_failed = self.args.max_failed_runs
                    if failed_runs >= max_failed:
                        self.logger.warning(
                            f'[DSE] Stopping attempts as number of FAILED runs has reached maximum allowed value of {max_failed}.'
                        )
                        break
                if not best_period or period < best_period:
                    best_period = period
                    best_rundir = suite.run_dir
                    best_results = {**suite.results}
            else:
                if best_period:
                    failed_runs += 1
                    next_period = (best_period + set_period) / 2 - improvement_threshold/2

            # worse or not worth it
            if best_period and (best_period - next_period) < improvement_threshold:
                self.logger.warning(
                    f'[DSE] Stopping attempts as expected improvement of period is less than the improvement threshold of {improvement_threshold}.'
                )
                break
            suite.settings.flow['clock_period'] = next_period

        self.logger.info(f'[DSE] best_period = {best_period}')
        self.logger.info(f'[DSE] best_rundir = {best_rundir}')
        print(f'---- Results with optimal frequency: ----')
        suite.print_results(best_results)

        self.logger.info(f'Run directories: {" ".join([str(os.path.relpath(d, Path.cwd())) for d in rundirs])}')
