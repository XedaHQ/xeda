# © 2021 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import copy
import logging
import os
import math
import csv
from pathlib import Path
import re
from typing import List
from xml.etree import ElementTree
import html

from ..flow import Flow
from .vivado_sim import VivadoPostsynthSim, VivadoSim
from .vivado_synth import VivadoSynth
from . import Vivado

logger = logging.getLogger(__name__)


class VivadoPower(Vivado):
    class Settings(Vivado.Settings):
        clock_period: float
        power_report_filename: str = 'power_impl_timing.xml'
        post_synth_sim: VivadoPostsynthSim.Settings = VivadoPostsynthSim.Settings(elab_debug='typical')

    def init(self):
        ss = self.settings
        assert isinstance(ss, self.Settings)
        # override/sanitize VivadoPostsynthSim dependency settings
        pss = ss.post_synth_sim
        if pss.synth.input_delay is None:
            pss.synth.input_delay = 0.0
        if pss.synth.output_delay is None:
            pss.synth.output_delay = 0.0

        # FIXME!!! For reasons still unknown, not all strategies lead to correct post-impl simulation
        pss.synth.synth.strategy = 'AreaPower'
        pss.synth.optimize_power = True
        # pss.synth.clock_period
        # pss.synth.clocks
        self.add_dependency(VivadoPostsynthSim, pss)

    def run(self):
        saif_filename: str = 'impl_timing.saif'
        postsynthsim = self.completed_dependencies[0]
        postsynthsim_settings = postsynthsim.settings
        run_configs = postsynthsim_settings.get('run_configs')

        def update_saif_path(rc):
            rc['saif'] = str(postsynthsim.flow_run_dir / rc['saif'])
            return rc

        if run_configs:
            run_configs = [update_saif_path(rc) for rc in run_configs]
        else:
            run_configs = dict(saif=self.saif_filename,
                               report=self.power_report_filename)

        self.run_configs = run_configs

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                              tb_top=postsynthsim.tb_top,
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
                contents = [
                    html.unescape(x.attrib['contents']).strip()
                    for x in tablecells
                ]
                key = contents[0]
                value = contents[1]
                components[key] = value
        results['Components Power'] = components

        return results

    def parse_reports(self):
        report_xml = self.flow_run_dir / self.power_report_filename
        results = self.parse_power_report(report_xml)
        self.results.success = True  # ???
        self.results.update(**results)
