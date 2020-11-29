# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

import re
from ..flow import SimFlow, Flow


class Ghdl(Flow):
    pass


class GhdlSim(Ghdl, SimFlow):

    def run(self):
        design_settings = self.settings.design
        flow_settings = self.settings.flow
        tb_settings = design_settings['tb']
        vhdl_settings = design_settings['language']['vhdl']

        # TODO synthesis, lint

        warns = ['-Wbinding', '-Wreserved', '-Wlibrary', '-Wvital-generic',
                 '-Wdelayed-checks', '-Wbody', '-Wspecs', '-Wunused', '--warn-no-runtime-error']

        vhdl_std = str(vhdl_settings['standard'])
        vhdl_std_opt = f'--std={"93c" if vhdl_std == "93" else vhdl_std}'
        optimize = ['-O3']
        analysis_options = ['-frelaxed-rules', '--warn-no-vital-generic',
                            '-frelaxed', '--mb-comments', vhdl_std_opt] + optimize

        vhdl_synopsys = vhdl_settings.get('synopsys')

        if vhdl_synopsys:
            analysis_options += ['--ieee=synopsys', '-fsynopsys']

        lib_paths = flow_settings.get('lib_paths', [])
        if isinstance(lib_paths, str):
            lib_paths = [lib_paths]
        lib_paths = [f'-P{p}' for p in lib_paths]

        elab_options = [vhdl_std_opt, '--syn-binding', '-frelaxed']
        if vhdl_synopsys:
            elab_options += ['-fsynopsys']

        run_options = ['--ieee-asserts=disable-at-0']  # TODO

        if self.args.verbose:
            analysis_options.append('-v')
            elab_options.append('-v')

        stop_time = self.settings.flow.get('stop_time')
        if stop_time:
            run_options.append(f'--stop-time={stop_time}')

        # --sdf=min=PATH=FILENAME
        # --sdf=typ=PATH=FILENAME
        # --sdf=max=PATH=FILENAME
        tb_uut = tb_settings.get('uut')
        sdf = self.settings.flow.get('sdf')
        if sdf:
            if not isinstance(sdf, list):
                sdf = [sdf]
            for s in sdf:
                if isinstance(s, str):
                    s = {"file": s}
                root = s.get("root", tb_uut)
                assert root, "neither SDF root nor tb.uut are provided"
                run_options.append(f'--sdf={s.get("delay", "max")}={root}={s["file"]}')

        if self.vcd:
            if self.vcd.endswith('.ghw'):
                run_options.append(f'--wave={self.vcd}')
            else:
                run_options.append(f'--vcd={self.vcd}')

        tb_generics_opts = [f"-g{k}={v}" for k, v in tb_settings.get("generics", {}).items()]
        # rtl_generics_opts = [f"-g{k}={v}" for k, v in rtl_settings.get("generics", {}).items()]

        # self.run_process('ghdl', ['-a'] + analysis_options + warns + sources,
        #                  initial_step='Analyzing VHDL files',
        #                  stdout_logfile='ghdl_analyze.log',
        #                  check=True
        #                  )

        # self.run_process('ghdl', ['-e'] + elab_options + [self.settings.design['tb_top']],
        #                  initial_step='Elaborating design',
        #                  stdout_logfile='ghdl_elaborate.log',
        #                  check=True
        #                  )

        tb_top = tb_settings['top']
        
        self.run_process('ghdl', ['-i'] + analysis_options + warns + list(map(lambda x: str(x), self.sim_sources)),
                         initial_step='Analyzing VHDL files',
                         stdout_logfile='ghdl_analyze_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['-m', '-f'] + elab_options + optimize + warns + lib_paths + [tb_top],
                         initial_step='Elaborating design',
                         stdout_logfile='ghdl_elaborate_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['-r', vhdl_std_opt, tb_top] + run_options + tb_generics_opts,
                         initial_step='Running simulation',
                         stdout_logfile='ghdl_run_stdout.log',
                         force_echo=True
                         )

            
