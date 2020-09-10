# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from datetime import datetime
from genericpath import exists
from pathlib import Path
import sys
import argparse
from xeda.flow_runner import DefaultFlowRunner, LwcFmaxRunner, LwcVariantsRunner

import coloredlogs
import logging

import pkg_resources

xeda_run_dir = Path('xeda_run')

xeda_run_dir.mkdir(exist_ok=True, parents=True)

logger = logging.getLogger()

logger.setLevel(logging.INFO)

timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S%f")[:-3]

logFormatter = logging.Formatter("%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s")

fileHandler = logging.FileHandler(xeda_run_dir / f"xeda_{timestamp}.log")
fileHandler.setFormatter(logFormatter)
logger.addHandler(fileHandler)


coloredlogs.install('INFO', fmt='%(asctime)s %(levelname)s %(message)s', logger=logger)


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
        self.logger = logger

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

        # FIXME this should be dynamically setup during runner registeration
        registered_runner_cmds = {
            'run': DefaultFlowRunner,
            'run_variants': LwcVariantsRunner,
            'run_fmax': LwcFmaxRunner
        }
        runner_cls = registered_runner_cmds.get(args.command)
        if runner_cls:
            runner = runner_cls(self.args)
        else:
            sys.exit(f"Runner for {args.command} is not implemented")

        runner.launch()
        


    #TODO FIXME
    def register_plugin_parsers(self):
        #TODO FIXME
        for runner_plugin in [LwcVariantsRunner, LwcFmaxRunner]:
            runner_plugin.register_subparser(self.subparsers)
        

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
            # help='Change top directory where the all the runs of a flow is run from `<FLOW_NAME>_run` ',
            # default=None
        )

        subparsers = parser.add_subparsers(dest='command', help='Commands Help')
        subparsers.required = True
        self.subparsers = subparsers

        # TODO FIXME add as validator!
        registered_flows = []
        ### FIXME FIXME FIXME


        ############################
        init_parser = subparsers.add_parser('init', help='Generate a design.json for running xeda flows') 
        ############################
        run_parser = subparsers.add_parser('run', help='Run a flow')
        run_parser.add_argument('flow', metavar='FLOW_NAME',
                                help=f'Flow name. Supported flows are: {registered_flows}')
        run_parser.add_argument(
            '--design-json',
            help='Path to design JSON file.'


    def generate_design_json(self):
        default_json = {"design" : {}}
        default_json["design"]["name"] = input("Enter a name for the design: ")
        default_json["design"]["description"] = input("(Optional) Enter the design description: ")
        default_json["design"]["author"] = [x.strip() for x in input("Enter the names of the primary author(s), separated by commas: ").split(",")]
        default_json["design"]["url"] = input("(Optional) Enter the URL for the design: ")
        default_json["design"]["sources"] = []
        sources_path = input("Enter the relative path of the directory with the sources_list.txt file: ")

        if sources_path[-1] != '/':
            sources_path = sources_path + '/'
        # Consider adding recursive source file search instead of source_list.txt
        try:
            with open(sources_path+'source_list.txt', 'r') as s:
                for line in s:
                    if not sources_path in line:
                        default_json["design"]["sources"].append((sources_path+line).strip())
                    else:
                        default_json["design"]["sources"].append(line.strip())
        except FileNotFoundError as e:
            sys.exit(f' Cannot find source_list.txt in {sources_path}. Please make sure it exists! \n {e}.')


        default_json["design"]["vhdl_std"] = "02"
        default_json["design"]["vhdl_synopsys"] = True
        default_json["design"]["clock_port"] = "clk"
        default_json["design"]["tb_top"] = "LWC_TB"
        default_json["design"]["tb_generics"] = {}
        default_json["design"]["generics"] = {}
        default_json["design"]["variant_id"] = "v1"
        default_json["design"]["flows"] = {}

        self.logger.info("Creating design.json with provided and default values. Please review them before running a design flow!")

        with open('design.json', 'w') as outfile:
            json.dump(default_json, outfile, indent=2)



        )

        self.register_plugin_parsers()
        
        return parser.parse_args(args)
