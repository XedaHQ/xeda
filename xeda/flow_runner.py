import pkg_resources
from datetime import datetime
from copy import copy
from pathlib import Path
import json
import sys
from .flows.flow import DesignSource, Flow, semantic_hash
from .utils import load_class, dict_merge, camelcase_to_snakecase, snakecase_to_camelcase

class FlowRunner():
    def __init__(self, logger, args) -> None:
        self.logger = logger
        self.args = args


class DefaultFlowRunner(FlowRunner):

    def get_default_settings(self):
        defaults_data = pkg_resources.resource_string(__name__, "defaults.json")
        try:
            return json.loads(defaults_data)
        except json.decoder.JSONDecodeError as e:
            self.logger.critical(f"Failed to parse defaults settings file (defaults.json): {' '.join(e.args)}")
            sys.exit(1)

    ##?????
    def check_settings(self):
        pass
            
    def run_flow(self):
        args = self.args
        settings = self.get_default_settings()
        self.check_settings()

        json_path = args.design_json if args.design_json else Path.cwd() / 'design.json'

        self.logger.info(f"Using design settings from {json_path}")

        try:
            with open(json_path) as f:
                design_settings = json.load(f)
                self.check_settings()
                settings = dict_merge(settings, design_settings)
        except FileNotFoundError as e:
            if args.design_json:
                sys.exit(f' Cannot open the specified design settings: {args.design_json}\n {e}')
            else:
                sys.exit(f' Cannot open default design settings (design.json) in the current directory.\n {e}')
        except IsADirectoryError as e:
            sys.exit(f' The specified design json is not a regular file.\n {e}')

        # self.check_settings()

        print(settings)

        if args.command == 'run':
            flow_cls = load_class(args.flow, ".flows")
            
            flow: Flow = flow_cls(settings, args, self.logger)


        # if args.command == 'dse':

        #     # assert flow_name == 'synth', f"Unsupported flow {flow_name}\n `dse` command only supports `synth` flow supports "
        #     self.find_fmax()

            # TODO simplify logic
            # orig_settings = copy.deepcopy(settings)


            # for pcls in plugin_clss:
            #     assert issubclass(pcls, Plugin)
            #     # create plugin instances
            #     plugin = pcls(self.logger)
            #     if isinstance(plugin, ReplicatorPlugin):
            #         self.replicator_hooks.append(plugin.replicate_settings_hook)
            #     if isinstance(plugin, PostRunPlugin):
            #         self.post_run_hooks.append(plugin.post_run_hook)
            #     if isinstance(plugin, PostResultsPlugin):
            #         self.post_results_hooks.append(plugin.post_results_hook)

            # self.replicated_settings = []
            # for hook in self.replicator_hooks:
            #     repl_settings = hook(self.settings)
            #     self.logger.info(f'Generated {len(repl_settings)} setting(s) from {hook.__self__.__class__.__name__}')
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
                    sys.exit(f'Entry `{src}` in `sources` needs to be a string or a DesignSource JSON dictionary')
                flow.settings.design['sources'][i] = DesignSource(**src).mk_relative(flow.run_dir)

            for gen_type in ['generics', 'tb_generics']:
                for gen_key, gen_val in flow.settings.design[gen_type].items():
                    if isinstance(gen_val, dict) and "file" in gen_val:
                        p = gen_val["file"]
                        assert isinstance(p, str), "value of `file` should be a relative or absolute path string"
                        gen_val = flow.conv_to_relative_path(p.strip())
                        flow.logger.info(f'Converting generic `{gen_key}` marked as `file`: {p} -> {gen_val}')
                        flow.settings.design[gen_type][gen_key] = gen_val

            # flow.check_settings()
            flow.dump_settings()

            flow.run()

                # # Run post-run hooks
                # for hook in flow.post_run_hooks:
                #     flow.logger.info(f"Running post-run hook from from {hook.__self__.__class__.__name__}")
                #     hook(flow.run_dir, flow.settings)

                # flow.reports_dir = flow.run_dir / flow.reports_subdir_name
                # if not flow.reports_dir.exists():
                #     flow.reports_dir.mkdir(parents=True)

                # flow.results = dict()
                # flow.parse_reports()
                # flow.results['timestamp'] = flow.timestamp
                # if flow.results:  # non empty
                #     flow.print_results()
                #     flow.dump_results()

                # # Run post-results hooks
                # for hook in flow.post_results_hooks:
                #     flow.logger.info(f"Running post-results hook from {hook.__self__.__class__.__name__}")
                #     hook(flow.run_dir, flow.settings, flow.results)
