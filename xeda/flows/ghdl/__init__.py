# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

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

        vhdl_std_opts = []
        vhdl_std = vhdl_settings.get('standard')
        if vhdl_std:
            vhdl_std_opts.append(f'--std={"93c" if str(vhdl_std) == "93" else vhdl_std}')

        optimize = ['-O3']

        analysis_options = []

        if vhdl_settings.get('synopsys'):
            analysis_options.append('--ieee=synopsys')
            vhdl_std_opts.append('-fsynopsys')

        analysis_options += vhdl_std_opts + ['-frelaxed-rules', '--warn-no-vital-generic',
                            '-frelaxed', '--mb-comments'] + optimize

        run_options = ['--ieee-asserts=disable-at-0']  # TODO
        elab_options = vhdl_std_opts + ['--syn-binding', '-frelaxed']


        lib_paths = flow_settings.get('lib_paths', [])
        if isinstance(lib_paths, str):
            lib_paths = [lib_paths]
        lib_paths = [f'-P{p}' for p in lib_paths]

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
        
        self.run_process('ghdl', ['remove'] + vhdl_std_opts,
                         initial_step='Clean up previously-generated files and library',
                         stdout_logfile='ghdl_remove_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['import'] + analysis_options + warns + list(map(lambda x: str(x), self.sim_sources)),
                         initial_step='Analyzing VHDL files',
                         stdout_logfile='ghdl_analyze_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['make'] + elab_options + optimize + warns + lib_paths + self.sim_tops,
                         initial_step='Elaborating design',
                         stdout_logfile='ghdl_elaborate_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['run'] + vhdl_std_opts + self.sim_tops + run_options + tb_generics_opts, # GHDL supports primary_unit [secondary_unit] 
                         initial_step='Running simulation',
                         stdout_logfile='ghdl_run_stdout.log',
                         force_echo=True
                         )
