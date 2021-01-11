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
        sim_overrides = {}
        period = flow_settings.get('clock_period')
        if period:
            sim_overrides['clock_period'] = period

        opt_power = flow_settings.get('optimize_power')
        if opt_power is not None:
            sim_overrides['optimize_power'] = opt_power

        sim_overrides['saif'] = cls.default_saif_file

        sim_overrides['elab_debug'] = 'typical'

        sim_overrides.update(constrain_io=True)

        return {VivadoPostsynthSim: (sim_overrides, {})}

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
            run_configs = dict(saif=self.default_saif_file, report=self.power_report_filename)

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


class VivadoPowerLwc(VivadoPower):
    required_settings = {}

    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):

        parent_prereqs = VivadoPower.prerequisite_flows(flow_settings, design_settings)
        
        flow_overrides, design_overrides = parent_prereqs[VivadoPostsynthSim]

        flow_overrides['clock_period'] = flow_settings.get('clock_period', 13.333)
        flow_overrides['optimize_power'] = flow_settings.get('optimize_power', True)
        flow_overrides['prerun_time'] = 100 + (flow_overrides['clock_period'] * 4) - 1
        flow_overrides['timing_sim'] = False

        tb_settings = design_settings['tb']
        design_name = design_settings['name']
        lwc_settings = design_settings.get('lwc', {})

        power_tvs = flow_settings.get('power_tvs')
        if not power_tvs:
            power_tvs = ['enc_16_0', 'enc_0_16', 'enc_1536_0',
                         'enc_0_1536', 'dec_16_0', 'dec_1536_0']
            algorithms = lwc_settings.get('algorithm')
            if (algorithms and (isinstance(algorithms, list) or isinstance(algorithms, tuple)) and len(algorithms) > 1) or lwc_settings.get('supports_hash'):
                power_tvs.extend(['hash_16', 'hash_1536'])

        lwc_variant = lwc_settings.get('variant')
        if not lwc_variant:
            name_splitted = design_name.split('-')
            assert len(
                name_splitted) > 1, "either specify design.lwc.variant or design.name should be ending with -v\d+"
            lwc_variant = name_splitted[-1]
            assert re.match(
                r'v\d+', lwc_variant), "either specify design.lwc.variant or design.name should be ending with -v\d+"
        power_tvs_root = os.path.join('KAT_GMU', lwc_variant)

        def pow_tv_run_config(tv_sub):
            tv_generics = copy.deepcopy(tb_settings.get('generics', {}))
            tv_generics['G_MAX_FAILURES'] = 1
            tv_generics['G_TEST_MODE'] = 0
            tv_generics['G_FNAME_LOG'] = f'{tv_sub}_LWCTB_log.txt'
            for t in ['pdi', 'sdi', 'do']:
                tv_generics[f'G_FNAME_{t.upper()}'] = DesignSource(
                    os.path.join(power_tvs_root, tv_sub, f'{t}.txt'))
            saif = f'{tv_sub}.saif'
            return dict(generics=tv_generics, saif=str(saif), report=f'{tv_sub}.xml', name=tv_sub)

        flow_overrides['run_configs'] = [pow_tv_run_config(tv) for tv in power_tvs]
        design_overrides['tb'] = design_overrides.get('tb', {})
        if 'configuration_specification' not in tb_settings:
            design_overrides['tb']["configuration_specification"] = "LWC_TB_wrapper_conf"

        return {VivadoPostsynthSim: (flow_overrides, design_overrides) }


    def parse_reports(self):
        design_name = self.settings.design['name']

        clock_period = self.synth_settings['clock_period']

        freq = math.floor(1000 / clock_period)
        fields = {'Design': design_name,
                  'Frequency (MHz)': freq, 'Static': None}

        timing_pat = re.compile(r"PASS \(0\): SIMULATION FINISHED after (?P<cycles>\d+) cycles at (?P<totaltime>.*)")

        for rc in self.postsynthsim_settings['run_configs']:
            name = rc['name']
            report_xml = self.flow_run_dir / rc['report']

            results = self.parse_power_report(report_xml)

            assert results['Design Nets Matched'].startswith('100%')
            assert results['Confidence Level'] == 'High'
            static = results['Device Static (W)']
            if fields['Static'] is None:
                fields['Static'] = static
            else:
                if fields['Static'] != static:
                    logger.warning(
                        f"Static power for {name} {static} is different from previous test vectors ({fields['Static']})")
                    fields[f'Static:{name}'] = static
            fields[name] = results['Dynamic (W)']

            lwctb_log = self.postsynthsim_flow.flow_run_dir / f'{name}_LWCTB_log.txt'
            with open(lwctb_log) as f:
                match = timing_pat.search(f.read())
                assert match, f"timing pattern not found in the LWC_TB log {lwctb_log}"
                results['cycles'] = match.group('cycles')
                fields[name + '_cycles'] = results['cycles']
                results['totaltime'] = match.group('totaltime')
                fields[name + '_totaltime'] = results['totaltime']
            self.results[name] = results

        fields['LUT'] = self.synth_results['lut']
        fields['FF'] = self.synth_results['ff']
        fields['Slice'] = self.synth_results['slice']
        fields['LUT RAM'] = self.synth_results['lut_mem']

        csv_path = Path.cwd() / f"VivadoPowerLwc_{design_name}_{freq}MHz.csv"
        with open(csv_path, "w") as csv_file:
            writer = csv.DictWriter(csv_file, fields.keys())
            writer.writeheader()
            writer.writerow(fields)

        logger.info(f"Power results written to {csv_path}")

        self.results['success'] = True
