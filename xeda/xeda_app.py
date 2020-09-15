# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from datetime import datetime
from genericpath import exists
from pathlib import Path
import sys
import argparse
from .debug import DebugLevel
from .flow_runner import DefaultFlowRunner, LwcFmaxRunner, LwcVariantsRunner

import coloredlogs
import logging

import pkg_resources

logger = logging.getLogger()
logger.setLevel(logging.INFO)



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

        # TODO this should be dynamically setup during runner registeration
        self.registered_runner_cmds = {
            'run': DefaultFlowRunner,
            'run_variants': LwcVariantsRunner,
            'run_fmax': LwcFmaxRunner
        }

    # TODO
    def check_settings(self):
        if "design" in self.settings:
            ds = self.settings["design"]
            assert "generics" not in ds or isinstance(ds["generics"], dict), "design.generics must be a dict"
            assert "tbgenerics" not in ds or isinstance(
                ds["tb_generics"], dict), "design.tb_generics must be a dict"

    def main(self):
        args = self.args = self.parse_args()
        
        if args.debug:
            logger.setLevel(logging.DEBUG)


        runner_cls = self.registered_runner_cmds.get(args.command)
        if runner_cls:
            xeda_run_dir = Path(args.xeda_run_dir)

            xeda_run_dir.mkdir(exist_ok=True, parents=True)

            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]

            logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")

            fileHandler = logging.FileHandler(xeda_run_dir / f"xeda_{timestamp}.log")
            fileHandler.setFormatter(logFormatter)
            logger.addHandler(fileHandler)


            coloredlogs.install('INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)

            runner = runner_cls(self.args)
        else:
            sys.exit(f"Runner for {args.command} is not implemented")

        runner.launch()


    #TODO FIXME
    def register_plugin_parsers(self):
        #TODO FIXME
        for runner_plugin in self.registered_runner_cmds.values():
            runner_plugin.register_subparser(self.subparsers)
        

    def parse_args(self, args=None):
        parser = self.parser
        parser.add_argument(
            '--debug',
            type=int,
            default=DebugLevel.NONE,
            help=f'Set debug level. Values of DEBUG_LEVEL correspond to: {list(DebugLevel)}'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Be verbose. Print everything to stdout.'
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            help="Be as quiet as possible. Never print out output from command executions"
        )
        parser.add_argument(
            '--force-run-dir',
            help='USE ONLY FOR DEBUG PURPOSES.',
            # default=None
        )
        parser.add_argument(
            '--xeda-run-dir',
            help='Directory where the flows are executed and intermediate and result files reside.',
            default='xeda_run'
        )

        subparsers = parser.add_subparsers(dest='command', help='Commands Help')
        subparsers.required = True
        self.subparsers = subparsers

        # TODO add validators for valid flow names and add back to help!

        self.register_plugin_parsers()
        
        return parser.parse_args(args)
