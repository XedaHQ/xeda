from abc import abstractmethod
from datetime import datetime
import coloredlogs
import os
import sys
import re
import logging
import pkg_resources
import json
from types import SimpleNamespace
from typing import Dict, Any
from pathlib import Path

from ..flows.flow_gen import FlowGen
from ..flows.design import Design
from ..flows.flow import Flow, FlowFatalException, my_print
from ..flow_runner import FlowRunner
from ..utils import dict_merge, try_convert

logger = logging.getLogger()


def merge_overrides(overrides, settings):
    if overrides:
        if isinstance(overrides, str):
            overrides = [overrides]
        if len(overrides) == 1:
            overrides = re.split(r'\s*,\s*', overrides[0])
        for override in overrides:
            key, val = override.split('=')
            hier = key.split('.')
            patch_dict = dict()
            for field in hier[:-1]:
                new_dict = dict()
                patch_dict[field] = new_dict
                patch_dict = new_dict
            patch_dict[hier[-1]] = try_convert(val, convert_lists=True)
            settings = dict_merge(settings, patch_dict, True)
    return settings


def print_results(results, title, subset):
    data_width = 32
    name_width = 80 - data_width
    hline = "-"*(name_width + data_width)

    my_print("\n" + hline)
    my_print(f"{title:^{name_width + data_width}s}")
    my_print(hline)
    for k, v in results.items():
        if not k.startswith('_') and (not subset or k in subset):
            if isinstance(v, float):
                my_print(f'{k:{name_width}}{v:{data_width}.6f}')
            elif isinstance(v, bool):
                my_print(
                    f'{k:{name_width}}{"True" if v else "False":>{data_width}}')
            elif isinstance(v, int):
                my_print(f'{k:{name_width}}{v:>{data_width}}')
            elif isinstance(v, list):
                my_print(f'{k:{name_width}}{" ".join(v):<{data_width}}')
            else:
                my_print(f'{k:{name_width}}{str(v):>{data_width}s}')
    my_print(hline + "\n")


class DefaultRunner(FlowRunner):
    pass

        # flow_class = self.load_flowclass(flow_name_or_class) if isinstance(
        #     flow_name_or_class, str) else flow_name_or_class

        # completed_dependencies = []

        # prerequisite_flows = flow_class.prerequisite_flows(
        #     flow_settings, design_settings)

        # for prereq, (flow_overrides, design_overrides) in prerequisite_flows.items():
        #     prereq_name = prereq if isinstance(
        #         prereq, str) else camelcase_to_snakecase(prereq.name)

        #     parent_overrides = flow_settings.get('dependencies', {}).get(prereq_name, {})

        #     prereq_flowsettings = dict_merge(self.get_flow_settings(prereq_name), parent_overrides)
        #     prereq_flowsettings = dict_merge(prereq_flowsettings, flow_overrides)
        #     prereq_design = dict_merge(design_settings, design_overrides)

        #     logger.info(f"Prerequisite: {prereq.__name__}")
        #     # recursive call
        #     completed_prereq = self.launch_flow(
        #         prereq, prereq_flowsettings, prereq_design, self.args.force_rerun
        #     )
        #     completed_dependencies.append(completed_prereq)

        # flow = self.setup_flow(flow_settings, design_settings, flow_class,
        #                        completed_dependencies)

        # results_json = flow.run_path / 'results.json'

        # if not force_run:
        #     try:
        #         with open(results_json) as f:
        #             flow.results = json.load(f)
        #     except FileNotFoundError:
        #         force_run = True
        #         logger.info(
        #             f"Running flow {flow.name} as {results_json} does not exist.")
        #     except Exception as e:
        #         force_run = True
        #         logger.info(f"running flow {flow.name} due to {e}")

        #     if not force_run and not flow.results.get('success'):
        #         force_run = True
        #         logger.info(
        #             f"Re-running flow {flow.name} as the previous run was not successful")

        #     prev_hash = flow.results.get('flow.run_hash')
        #     if not force_run and prev_hash != flow.xedahash:
        #         force_run = True
        #         logger.info(
        #             f"Re-running flow {flow.name} as the previous run hash ({prev_hash}) did not match the current one ({flow.xedahash})")

        # if force_run:
        


def nukemall():
    def on_terminate(proc):
        logger.warning(
            f"Child process {proc.info['name']}[{proc}] terminated with exit code {proc.returncode}")

    try:
        pass
        # procs = psutil.Process().children(recursive=True)
        # print(f"killing {len(procs)} child processes")
        # for p in procs:
        #     p.terminate()
        # gone, alive = psutil.wait_procs(procs, timeout=3, callback=on_terminate)
        # for p in alive:
        #     p.kill()
        # on nix: negative number means the process group with that PGID
        # os.kill(-os.getpgid(0), signal.SIGINT)
    except:
        logger.exception('exception during killing')
