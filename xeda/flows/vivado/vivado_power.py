# © 2021 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
import logging
import os
import math
import csv
from pathlib import Path
import re
from types import SimpleNamespace
from typing import List
from xml.etree import ElementTree
import html

from ..flow import DesignSource, Flow
from ...flows.settings import Settings
from .vivado_sim import VivadoPostsynthSim
from .vivado_synth import VivadoSynth
from .vivado import Vivado

logger = logging.getLogger()


class VivadoPower(Vivado):

    required_settings = {'clock_period'}

    default_saif_file = 'impl_timing.saif'

    @classmethod
    def prerequisite_flows(cls, flow_settings, _design_settings):
        synth_overrides = dict(strategy=flow_settings.get('strategy', 'AreaPower')) ## FIXME!!! For reasons still unknown, not all strategies lead to correct post-impl simulation
        postsynthsim_overrides = dict(constrain_io=True, elab_debug='typical',
                                      saif=cls.default_saif_file,  dependencies=dict(vivado_synth=synth_overrides))

        period = flow_settings.get('clock_period')
        if period:
            postsynthsim_overrides['clock_period'] = period
            synth_overrides['clock_period'] = period

        opt_power = flow_settings.get('optimize_power')
        if opt_power is not None:
            postsynthsim_overrides['optimize_power'] = opt_power
            synth_overrides['optimize_power'] = opt_power

        return {VivadoPostsynthSim: (postsynthsim_overrides, {})}

    def __init__(self, settings: Settings, args: SimpleNamespace, completed_dependencies: List['Flow']):
        self.postsynthsim_flow = completed_dependencies[0]
        self.postsynthsim_settings = self.postsynthsim_flow.settings.flow
        self.synth_flow = self.postsynthsim_flow.completed_dependencies[0]
        self.synth_settings = self.synth_flow.settings.flow
        self.synth_results = self.synth_flow.results
        self.power_report_filename = 'power_impl_timing.xml'
        super().__init__(settings, args, completed_dependencies)

    def run(self):
        run_configs = self.postsynthsim_settings.get('run_configs')

        def update_saif_path(rc):
            rc['saif'] = str(self.postsynthsim_flow.flow_run_dir / rc['saif'])
            return rc

        if run_configs:
            run_configs = [update_saif_path(rc) for rc in run_configs]
        else:
            run_configs = dict(saif=self.default_saif_file,
                               report=self.power_report_filename)

        self.run_configs = run_configs

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                              tb_top=self.postsynthsim_flow.tb_top,
                                              run_configs=self.run_configs,
                                              checkpoint=str(
                                                  self.synth_flow.flow_run_dir / VivadoSynth.checkpoints_dir / 'post_route.dcp')
                                              )

        return self.run_vivado(script_path, stdout_logfile='vivado_postsynth_power_stdout.log')

    def parse_power_report(self, report_xml):
        tree = ElementTree.parse(report_xml)

        results = {}
        components = {}

        for tablerow in tree.findall("./section[@title='Summary']/table/tablerow"):
            tablecells = tablerow.findall('tablecell')
            key, value = (html.unescape(
                x.attrib['contents']).strip() for x in tablecells)
            results[key] = value

        for tablerow in tree.findall("./section[@title='Summary']/section[@title='On-Chip Components']/table/tablerow"):
            tablecells = tablerow.findall('tablecell')
            if len(tablecells) >= 2:
                contents = [html.unescape(
                    x.attrib['contents']).strip() for x in tablecells]
                key = contents[0]
                value = contents[1]
                components[key] = value
        results['Components Power'] = components

        return results

    def parse_reports(self):
        report_xml = self.flow_run_dir / self.power_report_filename

        results = self.parse_power_report(report_xml)

        self.results['success'] = True
        self.results.update(**results)
