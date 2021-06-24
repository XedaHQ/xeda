from typing import List
from pydantic.types import NoneStr
from ..flow import SimFlow, Flow


class Ghdl(Flow):
    """GHDL flows: sim, synthesis, lint"""
    class GhdlSettings(Flow.Settings):
        analysis_flags: List[str] = [
            '-frelaxed-rules', '--warn-no-vital-generic', '-frelaxed', '--mb-comments'
        ]
        optimization_flags: List[str] = ['-O3']
        elab_flags: List[str] = ['--syn-binding', '-frelaxed']
        run_flags: List[str] = ['--ieee-asserts=disable-at-0']
        warn_flags: List[str] = [
            '-Wbinding', '-Wdefault-binding', '-Wreserved', '-Wlibrary', '-Wvital-generic',
            '-Wdelayed-checks', '-Wbody', '-Wspecs', '-Wunused', '--warn-no-runtime-error'
        ]
        warn_error: bool = False


class GhdlSim(Ghdl, SimFlow):
    class Settings(Ghdl.GhdlSettings, SimFlow.Settings):
        sdf: List[str] = []
        ghw: NoneStr = None

    def run(self):

        warns = self.settings.warn_flags
        if self.settings.warn_error:
            warns += ['--warn-error']

        vhdl_std_opts = []
        vhdl_std = self.design.language.vhdl.standard
        if vhdl_std:
            vhdl_std_opts.append(
                f'--std={"93c" if str(vhdl_std) == "93" else vhdl_std}')

        analysis_options = self.settings.analysis_flags

        if self.design.language.vhdl.synopsys:
            analysis_options.append('--ieee=synopsys')
            vhdl_std_opts.append('-fsynopsys')

        analysis_options += vhdl_std_opts + self.settings.optimization_flags

        run_options = self.settings.run_flags
        elab_options = self.settings.elab_flags + \
            vhdl_std_opts + self.settings.optimization_flags

        lib_paths = [f'-P{p}' for p in self.settings.lib_paths]

        if self.settings.verbose:
            analysis_options.append('-v')
            elab_options.append('-v')

        stop_time = self.settings.stop_time
        if stop_time:
            run_options.append(f'--stop-time={stop_time}')

        # --sdf=min=PATH=FILENAME
        # --sdf=typ=PATH=FILENAME
        # --sdf=max=PATH=FILENAME
        tb_uut = self.design.tb.uut
        sdf = self.settings.sdf
        if sdf:
            # FIXME!
            for s in sdf:
                if isinstance(s, str):
                    s = {"file": s}
                root = s.get("root", tb_uut)
                assert root, "neither SDF root nor tb.uut are provided"
                run_options.append(
                    f'--sdf={s.get("delay", "max")}={root}={s["file"]}')

        ghw = self.settings.ghw
        if self.vcd:
            run_options.append(f'--vcd={self.vcd}')
        elif ghw:
            if not ghw.endswith('.ghw'):
                ghw += '.ghw'
            run_options.append(f'--wave={ghw}')

        tb_generics_opts = [f"-g{k}={v}" for k,
                            v in self.design.tb.generics.items()]

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

        self.run_process('ghdl', ['make'] + elab_options + warns + lib_paths + self.sim_tops,
                         initial_step='Elaborating design',
                         stdout_logfile='ghdl_elaborate_stdout.log',
                         check=True
                         )

        self.run_process('ghdl', ['run'] + vhdl_std_opts + self.sim_tops + run_options + tb_generics_opts,  # GHDL supports primary_unit [secondary_unit]
                         initial_step='Running simulation',
                         stdout_logfile='ghdl_run_stdout.log',
                         force_echo=True
                         )
