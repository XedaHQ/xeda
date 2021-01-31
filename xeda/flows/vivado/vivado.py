# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)
import logging
from ..flow import Flow, DebugLevel

logger = logging.getLogger()


def supported_vivado_generic(k, v, sim):
    if sim:
        return True
    if isinstance(v, int):
        return True
    if isinstance(v, bool):
        return True
    v = str(v)
    return (v.isnumeric() or (v.strip().lower() in {'true', 'false'}))


def vivado_gen_convert(k, x, sim):
    if sim:
        return x
    xl = str(x).strip().lower()
    if xl == 'false':
        return "1\\'b0"
    if xl == 'true':
        return "1\\'b1"
    return x


def vivado_generics(kvdict, sim):
    return ' '.join([f"-generic{'_top' if sim else ''} {k}={vivado_gen_convert(k, v, sim)}" for k, v in kvdict.items() if supported_vivado_generic(k, v, sim)])


class Vivado(Flow):
    reports_subdir_name = 'reports'

    def run_vivado(self, script_path, stdout_logfile=None):
        if stdout_logfile is None:
            stdout_logfile = f'{self.name}_stdout.log'
        debug = self.args.debug > DebugLevel.NONE
        vivado_args = ['-nojournal', '-mode', 'tcl' if debug >=
                       DebugLevel.HIGHEST else 'batch', '-source', str(script_path)]
        # if not debug:
        #     vivado_args.append('-notrace')
        return self.run_process('vivado', vivado_args, initial_step='Starting vivado',
                                stdout_logfile=stdout_logfile)
