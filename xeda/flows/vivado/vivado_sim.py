# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
import copy
import json
import logging
import os
import math
import csv
from pathlib import Path
import re
from xml.etree import ElementTree
import html
from distutils.util import strtobool
from ...utils import try_convert, unique_list
from ..flow import DesignSource, SimFlow, DebugLevel
from .vivado_synth import VivadoSynth
from .vivado import Vivado

logger = logging.getLogger()


class VivadoSim(Vivado, SimFlow):
    def run(self):
        flow_settings = self.settings.flow
        tb_settings = self.settings.design["tb"]
        generics = tb_settings.get("generics", {})
        saif = flow_settings.get('saif')

        elab_flags = flow_settings.get('elab_flags')
        if not elab_flags:
            elab_flags = ['-nospecify',
                          '-notimingchecks', '-relax', '-maxdelay']

        elab_debug = flow_settings.get('elab_debug')
        run_configs = flow_settings.get('run_configs')
        if not elab_debug and (self.args.debug or saif or self.vcd):
            elab_debug = "typical"
        if elab_debug:
            elab_flags.append(f'-debug {elab_debug}')

        if not run_configs:
            run_configs = [dict(saif=saif, generics=generics)]
        else:
            for rc in run_configs:
                # merge
                rc['generics'] = {**generics, **rc['generics']}
                if saif and not 'saif' in rc:
                    rc['saif'] = saif

        tb_uut = tb_settings.get('uut')
        sdf = flow_settings.get('sdf')
        if sdf:
            if not isinstance(sdf, list):
                sdf = [sdf]
            for s in sdf:
                if isinstance(s, str):
                    s = {"file": s}
                root = s.get("root", tb_uut)
                assert root, "neither SDF root nor tb.uut are provided"
                elab_flags.append(
                    f'-sdf{s.get("delay", "max")} {root}={s["file"]}')

        libraries = flow_settings.get('libraries')
        if libraries:
            elab_flags.extend([f'-L {l}' for l in libraries])

        elab_optimize = flow_settings.get('elab_optimize', '-O3')
        if elab_optimize and elab_optimize not in elab_flags:  # FIXME none of -Ox in elab_flags
            elab_flags.append(elab_optimize)

        script_path = self.copy_from_template(f'vivado_sim.tcl',
                                              analyze_flags=' '.join(flow_settings.get(
                                                  'analyze_flags', ['-relax'])),
                                              elab_flags=' '.join(
                                                  unique_list(elab_flags)),
                                              run_configs=run_configs,
                                              sim_flags=' '.join(
                                                  flow_settings.get('sim_flags', [])),
                                              initialize_zeros=False,
                                              vcd=self.vcd,
                                              sim_tops=self.sim_tops,
                                              tb_top=self.tb_top,
                                              lib_name='work',
                                              sim_sources=self.sim_sources,
                                              debug_traces=self.args.debug >= DebugLevel.HIGHEST or self.settings.flow.get(
                                                  'debug_traces')
                                              )
        return self.run_vivado(script_path)


class VivadoPostsynthSim(VivadoSim):
    depends_on = {VivadoSynth: {'rtl.sources': ['results/impl_timesim.v']}}

    def pre_depend(self, dep):
        pass

    def post_depend(self, dep):
        pass

    def run(self):
        design_settings = self.settings.design
        tb_settings = design_settings['tb']
        flow_settings = self.settings.flow
        top = tb_settings['top']
        if isinstance(top, str):
            top = [top]
        if not 'glbl' in top:
            top.append('glbl')
        tb_settings['top'] = top

        if 'libraries' not in flow_settings:
            flow_settings['libraries'] = []
        if 'simprims_ver' not in flow_settings['libraries']:
            flow_settings['libraries'].append('simprims_ver')

        netlist_base = os.path.splitext(
            str(design_settings['rtl']['sources'][0]))[0]
        timing_sim = try_convert(flow_settings.get('timing_sim'))
        if isinstance(timing_sim, str):
            timing_sim = strtobool(timing_sim)
        if timing_sim is None:
            timing_sim = True # defaults is true!
        timing_sim = bool(timing_sim)
        logger.info(f'timing_sim: {timing_sim}')
        if timing_sim and not flow_settings.get('sdf'):
            flow_settings['sdf'] = {'file': netlist_base + '.sdf'}

        clock_period_ps_generic = tb_settings.get(
            'clock_period_ps_generic', 'G_PERIOD_PS')  # FIXME
        if clock_period_ps_generic:
            clock_ps = math.floor(
                self.settings.flow_depends['vivado_synth']['clock_period'] * 1000)
            tb_settings['generics'][clock_period_ps_generic] = clock_ps
            for rc in flow_settings.get('run_configs', []):
                rc['generics'][clock_period_ps_generic] = clock_ps

        flow_settings['elab_flags'] = ['-relax', '-maxdelay', '-transport_int_delays',
                                       '-pulse_r 0', '-pulse_int_r 0', '-pulse_e 0', '-pulse_int_e 0']

        VivadoSim.run(self)


class VivadoPower(VivadoPostsynthSim):
    # depends_on = {VivadoPostsynthSim: {'rtl.sources': ['results/impl_timesim.v']}}
    def run(self):
        flow_settings = self.settings.flow

        if flow_settings.get('prerun_time') is None:
            flow_settings['prerun_time'] = 100 + math.floor(
                self.settings.flow_depends['vivado_synth']['clock_period'] * 4) - 1
        saif_file = 'impl_timing.saif'
        self.power_report_filename = 'power_impl_timing.xml'
        flow_settings['saif'] = saif_file
        # run simulation FIXME implement through flow dependency system
        VivadoPostsynthSim.run(self)

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                              tb_top=self.tb_top,
                                              run_configs=[
                                                  dict(saif=saif_file, report=self.power_report_filename)],
                                              checkpoint=os.path.relpath(str(
                                                  self.run_path / 'vivado_synth' / VivadoSynth.checkpoints_dir / 'post_route.dcp'), Path(self.flow_run_dir))
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


# TODO move to an LWC plugin that hooks into VivadoPower
class VivadoPowerLwc(VivadoPower):
    def run(self):
        flow_settings = self.settings.flow
        tb_settings = self.settings.design['tb']
        design_name = self.settings.design['name']
        lwc_settings = self.settings.design.get('lwc', {})

        if flow_settings.get('prerun_time') is None:
            flow_settings['prerun_time'] = 100 + math.floor(
                self.settings.flow_depends['vivado_synth']['clock_period'] * 4) - 1

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
            for t in ['pdi', 'sdi', 'do']:
                tv_generics[f'G_FNAME_{t.upper()}'] = DesignSource(
                    os.path.join(power_tvs_root, tv_sub, f'{t}.txt'))
            return dict(generics=tv_generics, saif=f'{tv_sub}.saif', report=f'{tv_sub}.xml', name=tv_sub)

        run_configs = [pow_tv_run_config(tv) for tv in power_tvs]
        flow_settings['run_configs'] = run_configs
        flow_settings['elab_debug'] = 'typical'

        tb_settings["top"] = "LWC_TB"
        # if not tb_settings.get('configuration_specification'):
        #     tb_settings["configuration_specification"] = "LWC_TB_wrapper_conf"

        if not flow_settings.get('skip_simulation'):
            # run simulation FIXME implement through dependency system
            VivadoPostsynthSim.run(self)

        script_path = self.copy_from_template(f'vivado_power.tcl',
                                              tb_top=self.tb_top,
                                              run_configs=run_configs,
                                              checkpoint=os.path.relpath(str(
                                                  self.run_path / 'vivado_synth' / VivadoSynth.checkpoints_dir / 'post_route.dcp'), Path(self.flow_run_dir))
                                              )

        self.run_vivado(
            script_path, stdout_logfile='vivado_postsynth_power_stdout.log')

    def parse_reports(self):
        design_name = self.settings.design['name']

        clock_period = self.settings.flow_depends['vivado_synth']['clock_period']
        freq = math.floor(1000 / clock_period)
        fields = {'Design': design_name,
                  'Frequency (MHz)': freq, 'Static': None}

        for rc in self.settings.flow['run_configs']:
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
            self.results[name] = results

        # copy synthesis results for reference
        # FIXME dependency results/paths
        with open(self.run_path / 'vivado_synth' / 'vivado_synth_results.json') as synth_results_file:
            synth_results = json.load(synth_results_file)

        fields['LUT'] = synth_results['lut']
        fields['FF'] = synth_results['ff']
        fields['Slice'] = synth_results['slice']
        fields['LUT RAM'] = synth_results['lut_mem']

        csv_path = Path.cwd() / f"VivadoPowerLwc_{design_name}_{freq}MHz.csv"
        with open(csv_path, "w") as csv_file:
            writer = csv.DictWriter(csv_file, fields.keys())
            writer.writeheader()
            writer.writerow(fields)

        logger.info(f"Power results written to {csv_path}")

        self.results['success'] = True
