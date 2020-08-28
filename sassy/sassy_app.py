# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import sys
import argparse
import json
from pathlib import Path

import coloredlogs
import logging



class SassyApp:
    def __init__(self):
        self.registered_suites = dict()
        self.parser = argparse.ArgumentParser()
        self.args = None
        self.logger = logging.getLogger(__package__)

    def register_suites(self, *flow_classes):
        for cls in flow_classes:
            self.registered_suites[cls.name] = cls

    def main(self):
        args = self.args = self.parse_args()

        coloredlogs.install(level='DEBUG' if args.debug else 'INFO',
                            fmt='%(asctime)s %(levelname)s %(message)s', logger=self.logger)

        settings = self.get_default_settings()

        json_path = args.design_json if args.design_json else Path.cwd() / 'design.json'

        try:
            with open(json_path) as f:
                design_settings = json.load(f)
                settings.update(design_settings)
        except FileNotFoundError as e:
            if args.design_json:
                sys.exit(f' Cannot open the specified design settings: {args.design_json}\n {e}')
            else:
                sys.exit(f' Cannot open default design settings (design.json) in the current directory.\n {e}')
        except IsADirectoryError as e:
            sys.exit(f' The specified design json file is a directory.\n {e}')

        def get_suite_flow(flow_name=None):
            splitted_flow_name = args.flow.split(':')
            suite_name = splitted_flow_name[0]
            if suite_name not in self.registered_suites:
                sys.exit(f"Suite `{suite_name}` is not currently supported. ")
            if len(splitted_flow_name) > 1:
                flow_name = splitted_flow_name[1]
            suite = self.registered_suites[suite_name](settings, args, self.logger)
            return suite, flow_name

        if args.command == 'run':
            suite, flow_name = get_suite_flow(flow_name=None)
            suite.run(flow_name)

        if args.command == 'fmax':
            suite, flow_name = get_suite_flow(flow_name='synth')
            assert flow_name == 'synth', f"Unsupported flow {flow_name}\n `fmax` command only supports `synth` flow supports "
            suite.find_fmax()

    def parse_args(self):
        parser = self.parser
        parser.add_argument(
            '--debug',
            action='store_true',
            help='Print debug info'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='print everything to stdout'
        )
        parser.add_argument(
            '--run-dir',
            help='The directory inside which the tools are run.',
            # default=None
        )
        parser.add_argument(
            '--design-json',
            help='print everything to stdout'
        )
        subparsers = parser.add_subparsers(required=True, dest='command', help='Commands Help')

        ## TODO FIXME add as validator!
        registered_flows = []
        for suite in self.registered_suites.values():
            for flow in suite.supported_flows:
                registered_flows.append(f'{suite.name}:{flow}')
        registered_flows = ', '.join(registered_flows)
        ############################
        run_parser = subparsers.add_parser('run', help='Run a flow')
        run_parser.add_argument('flow', metavar='SUITE_NAME[:FLOW_NAME]',
                                help=f'Flow name. Suuported flows are: {registered_flows}')
        ############################
        fmax_parser = subparsers.add_parser(
            'fmax', help='Run `synth` flow of a suite several times, sweeping over clock_period constraint to find the maximum frequency of the design for the current settings')
        fmax_parser.add_argument('flow', metavar='SUITE_NAME[:FLOW_NAME]',
                                 help=f'Name of the suite to execute. Suuported flows are: {registered_flows}')
        fmax_parser.add_argument('--max-failed-runs', type=int, default=40,
                                 help=f'Maximum number of consecutive runs that did not improve F_max. Search stops afterwards')

        return parser.parse_args()

    def get_default_settings(self):
        settings_dir = Path.home() / '.sassyn'

        default_settings_file = settings_dir / 'defaults.json'

        if not settings_dir.exists():
            settings_dir.mkdir(parents=True)

        assert settings_dir.exists and settings_dir.is_dir()

        if not default_settings_file.exists():
            default_settings = {
                'design': {
                    'top': 'LWC', 'tb_top': 'LWC_TB'
                },
                'flows': {
                    'diamond': {
                        'fpga_part': 'LFE5U-25F-6BG381C',
                        'clock_period': 10.0,
                        'synthesis_engine': 'synplify',
                        'base_strategy': "Timing",
                    }
                }
            }

            print(f"Default settings does not exists. Creating {default_settings_file}")
            for suite in self.registered_suites.keys():
                if suite not in default_settings['flows']:
                    default_settings['flows'][suite] = {}

            with open(default_settings_file, 'w') as f:
                json.dump(default_settings, f, indent=4)

        with open(default_settings_file) as f:
            default_settings = json.load(f)
        return default_settings
