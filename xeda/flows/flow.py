from __future__ import annotations
from abc import ABCMeta, abstractmethod
from pydantic import BaseModel, Field, Extra
from typing import List, Optional, Type, Tuple
from datetime import datetime
import json
import os
import re
from pathlib import Path
import subprocess
import time
from jinja2 import Environment, PackageLoader, StrictUndefined
import logging
from jinja2.loaders import ChoiceLoader
from progress import SHOW_CURSOR
from progress.spinner import Spinner as Spinner
import colored
from typing import Dict, List
import inspect
import multiprocessing
from pydantic.types import NoneStr

from .design import Design
from ..utils import camelcase_to_snakecase, try_convert
from ..debug import DebugLevel

logger = logging.getLogger()


class FlowFatalException(Exception):
    """Fatal error"""
    pass


class NonZeroExit(Exception):
    """Process exited with non-zero return"""
    pass


def final_kill(proc):
    try:
        proc.terminate()
        proc.wait()
        proc.kill()
        proc.wait()
    except:
        pass

# @contextmanager
# def process(*args, **kwargs):
#     proc = subprocess.Popen(*args, **kwargs)
#     try:
#         yield proc
#     finally:
#         final_kill(proc)


def my_print(*args, **kwargs):
    print(*args, **kwargs)


# similar to str.removesuffix in Python 3.9+
def removesuffix(s: str, suffix: str) -> str:
    return s[:-len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, suffix: str) -> str:
    return s[len(suffix):] if suffix and s.startswith(suffix) else s


class MetaFlow(ABCMeta):
    # called when instance is created
    # def __call__(self, *args, **kwargs):
    #     obj = super(MetaFlow, self).__call__(*args, **kwargs)
    #     return obj
    pass


registered_flows: Dict[str, Tuple[str, Type[Flow]]] = {}


""" A flow may run one or more tools and is associated with a single set of settings and a single design. """


class Flow(metaclass=MetaFlow):
    """name attribute is set automatically"""
    name = None

    """Settings that can affect flow's behavior"""
    class Settings(BaseModel, metaclass=ABCMeta, extra=Extra.forbid):
        reports_subdir_name: str = 'reports'
        timeout_seconds: int = 3600 * 2
        """max number of threads/cpus"""
        nthreads: int = Field(default_factory=multiprocessing.cpu_count)
        quiet: bool = False
        verbose: bool = False
        debug: bool = False
        no_console: bool = False
        reports_dir: str = 'reports'
        lib_paths: List[str] = []
        generics: Dict[str, str] = {}

    @classmethod
    def prerequisite_flows(cls, flow_settings, design_settings):
        return {}

    def __init_subclass__(cls) -> None:
        if inspect.isabstract(cls):
            return
        cls_name = camelcase_to_snakecase(cls.__name__)
        mod_name = cls.__module__
        logger.info(f"registering flow {cls_name} from {mod_name}")
        registered_flows[cls_name] = (mod_name, cls)

        if mod_name and not mod_name.startswith('xeda.flows.'):
            mod_name1 = removeprefix(mod_name, "xeda.plugins.")
            m = mod_name1.split('.')  # FIXME FIXME FIXME!!!
            cls_name = m[0] + "." + cls_name
        cls.name = cls_name

    def __init__(self, flow_settings: Flow.Settings, design: Design, run_path: Path, completed_dependencies: List[Flow] = []):
        self.settings: Flow.Settings = flow_settings
        self.design: Design = design
        self.run_path = run_path

        self.init_time = None

        self.reports_dir = run_path / self.settings.reports_subdir_name
        self.reports_dir.mkdir(exist_ok=True)

        self.results = dict()
        self.results['success'] = False

        loaderChoices = []
        mod_paths = []

        for mpx in [self.__module__, self.__class__.__module__] + [clz.__module__ for clz in self.__class__.__bases__]:
            for mp in [mpx, mpx.rsplit(".", 1)[0]]:
                if mp not in mod_paths:
                    mod_paths.append(mp)
        for mp in mod_paths:
            try:
                loaderChoices.append(PackageLoader(mp))
            except:
                pass

        self.jinja_env = Environment(
            loader=ChoiceLoader(loaderChoices),
            autoescape=False,
            undefined=StrictUndefined
        )

        self.completed_dependencies = completed_dependencies

    @abstractmethod
    def run(self):
        pass

    def parse_reports(self):
        # Do nothing if not implemented
        pass

    def dump_settings(self):
        effective_settings_json = self.run_path / f'settings.json'
        logger.info(f'dumping effective settings to {effective_settings_json}')
        self.dump_json(self.settings, effective_settings_json)

    def copy_from_template(self, resource_name, **kwargs) -> str:
        template = self.jinja_env.get_template(resource_name)
        script_path = self.run_path / resource_name
        logger.debug(f'generating {script_path.resolve()} from template.')
        rendered_content = template.render(
            settings=self.settings,
            design=self.design,
            **kwargs
        )
        with open(script_path, 'w') as f:
            f.write(rendered_content)
        return resource_name

    def conv_to_relative_path(self, src):
        path = Path(src).resolve(strict=True)
        return os.path.relpath(path, self.run_path)

    def fatal(self, msg):
        logger.critical(msg)
        raise FlowFatalException(msg)

    def run_process(self, prog, prog_args, check=True, stdout_logfile=None, initial_step=None, force_echo=False, nolog=False):
        prog_args = [str(a) for a in prog_args]
        if nolog:
            subprocess.check_call([prog] + prog_args, cwd=self.run_path)
            return
        if not stdout_logfile:
            stdout_logfile = f'{prog}_stdout.log'
        proc = None
        spinner = None
        unicode = True
        verbose = not self.settings.quiet and (
            self.settings.verbose or force_echo)
        echo_instructed = False
        stdout_logfile = self.run_path / stdout_logfile
        start_step_re = re.compile(
            r'^={12}=*\(\s*(?P<step>[^\)]+)\s*\)={12}=*')
        enable_echo_re = re.compile(r'^={12}=*\( \*ENABLE ECHO\* \)={12}=*')
        disable_echo_re = re.compile(r'^={12}=*\( \*DISABLE ECHO\* \)={12}=*')
        error_msg_re = re.compile(r'^\s*error:?\s+', re.IGNORECASE)
        warn_msg_re = re.compile(r'^\s*warning:?\s+', re.IGNORECASE)
        critwarn_msg_re = re.compile(
            r'^\s*critical\s+warning:?\s+', re.IGNORECASE)

        def make_spinner(step):
            if self.settings.no_console:
                return None
            return Spinner('⏳' + step + ' ' if unicode else step + ' ')

        redirect_std = self.settings.debug < DebugLevel.HIGH
        with open(stdout_logfile, 'w') as log_file:
            try:
                logger.info(
                    f'Running `{prog} {" ".join(prog_args)}` in {self.run_path}')
                with subprocess.Popen([prog, *prog_args],
                                      cwd=self.run_path,
                                      shell=False,
                                      stdout=subprocess.PIPE if redirect_std else None,
                                      bufsize=1,
                                      universal_newlines=True,
                                      encoding='utf-8',
                                      errors='replace'
                                      ) as proc:
                    logger.info(
                        f'Started {proc.args[0]}[{proc.pid}].{(" Standard output is logged to: " + str(stdout_logfile)) if redirect_std else ""}')

                    def end_step():
                        if spinner:
                            if unicode:
                                print('\r✅', end='')
                            spinner.finish()
                    if redirect_std:
                        if initial_step:
                            spinner = make_spinner(initial_step)
                        while True:
                            line = proc.stdout.readline()
                            if not line:
                                end_step()
                                break
                            log_file.write(line)
                            log_file.flush()
                            if verbose or echo_instructed:
                                if disable_echo_re.match(line):
                                    echo_instructed = False
                                else:
                                    print(line, end='')
                            else:
                                if not self.settings.quiet:
                                    if error_msg_re.match(line) or critwarn_msg_re.match(line):
                                        if spinner:
                                            print()
                                        logger.error(line)
                                    elif warn_msg_re.match(line):
                                        if spinner:
                                            print()
                                        logger.warning(line)
                                    elif enable_echo_re.match(line):
                                        if not self.settings.quiet:
                                            echo_instructed = True
                                        end_step()
                                    else:
                                        match = start_step_re.match(line)
                                        if match:
                                            end_step()
                                            step = match.group('step')
                                            spinner = make_spinner(step)
                                        else:
                                            if spinner:
                                                spinner.next()

            except FileNotFoundError as e:
                self.fatal(
                    f"Cannot execute `{prog}`. Make sure it's properly installed and is in the current PATH")
            except KeyboardInterrupt as e:
                if spinner:
                    print(SHOW_CURSOR)
                final_kill(proc)
            finally:
                final_kill(proc)

        if spinner:
            print(SHOW_CURSOR)

        if proc.returncode != 0:
            m = f'`{proc.args[0]}` exited with returncode {proc.returncode}'
            logger.critical(
                f'{m}. Please check `{stdout_logfile}` for error messages!')
            if check:
                raise NonZeroExit(m)
        else:
            logger.info(
                f'Execution of {prog} in {self.run_path} completed with returncode {proc.returncode}')

    def parse_report_regex(self, reportfile_path, re_pattern, *other_re_patterns, dotall=True):
        # TODO fix debug and verbosity levels!
        high_debug = self.settings.verbose
        if not reportfile_path.exists():
            logger.warning(
                f'File {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check log files in {self.run_path}'
            )
            return False
        with open(reportfile_path) as rpt_file:
            content = rpt_file.read()

            flags = re.MULTILINE | re.IGNORECASE
            if dotall:
                flags |= re.DOTALL

            def match_pattern(pat):
                match = re.search(pat, content, flags)
                if match is None:
                    return False
                match_dict = match.groupdict()
                for k, v in match_dict.items():
                    self.results[k] = try_convert(v)
                    logger.debug(f'{k}: {self.results[k]}')
                return True

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    if high_debug:
                        logger.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched = match_pattern(subpat)
                        if matched:
                            if high_debug:
                                logger.debug("subpat matched!")
                            break
                else:
                    if high_debug:
                        logger.debug(f"Matching: {pat}")
                    matched = match_pattern(pat)

                if not matched:
                    self.fatal(
                        f"Error parsing report file: {rpt_file.name}\n Pattern not matched: {pat}\n")
        return True

    def print_results(self, results=None):
        if not results:
            results = self.results
            # init to print_results time:
            if results.get('runtime_minutes') is None and self.init_time is not None:
                results['runtime_minutes'] = (
                    time.monotonic() - self.init_time) / 60
        data_width = 32
        name_width = 80 - data_width
        hline = "-"*(name_width + data_width)

        my_print("\n" + hline)
        my_print(f"{'Results':^{name_width + data_width}s}")
        my_print(hline)
        for k, v in results.items():
            if v is not None and not k.startswith('_'):
                if isinstance(v, float):
                    my_print(f'{k:{name_width}}{v:{data_width}.3f}')
                elif isinstance(v, bool):
                    bdisp = (colored.fg(
                        "green") + "✓" if v else colored.fg("red") + "✗") + colored.attr("reset")
                    my_print(f'{k:{name_width}}{bdisp:>{data_width}}')
                elif isinstance(v, int):
                    my_print(f'{k:{name_width}}{v:>{data_width}}')
                elif isinstance(v, list):
                    my_print(
                        f'{k:{name_width}}{" ".join([str(x) for x in v]):<{data_width}}')
                else:
                    my_print(f'{k:{name_width}}{str(v):>{data_width}s}')
        my_print(hline + "\n")

    def dump_json(self, data, path: Path):
        if path.exists():
            modifiedTime = os.path.getmtime(path)
            suffix = datetime.fromtimestamp(
                modifiedTime).strftime("%Y-%m-%d-%H%M%S")
            backup_path = path.with_suffix(f".backup_{suffix}.json")
            logger.warning(
                f"File already exists! Backing-up existing file to {backup_path}")
            os.rename(path, backup_path)

        with open(path, 'w') as outfile:
            json.dump(data, outfile, default=lambda x: x.__dict__ if hasattr(
                x, '__dict__') else str(x), indent=4)

    def dump_results(self):
        path = self.run_path / f'results.json'
        self.dump_json(self.results, path)
        logger.info(f"Results written to {path}")


class SimFlow(Flow):
    class Settings(Flow.Settings):
        vcd: NoneStr = None
        stop_time: NoneStr = None

    def __init__(self, flow_settings: SimFlow.Settings, design: Design, run_path: Path, completed_dependencies: List[Flow] = []):
        self.settings: SimFlow.Settings = flow_settings
        super().__init__(flow_settings, design, run_path, completed_dependencies)

    @property
    def sim_sources(self):
        return self.design.rtl.sources + [src for src in self.design.tb.sources if src not in self.design.rtl.sources]

    @property
    def sim_tops(self) -> List[str]:
        """ a view of tb.top that returns a list of primary_unit [secondary_unit] """
        conf_spec = self.design.tb.configuration_specification
        if conf_spec:
            return [conf_spec]
        tops = []
        if self.design.tb.top:
            tops = [self.design.tb.top]
        if self.design.tb.secondary_top:
            tops.append(self.design.tb.secondary_top)
        return tops

    @property
    def tb_top(self) -> str:
        assert self.design.tb.top, "design.tb.top must be set for simulation flow"
        return self.design.tb.top

    def parse_reports(self):
        self.results['success'] = True

    @property
    def vcd(self) -> Optional[str]:
        vcd = self.settings.vcd
        if vcd:
            if not isinstance(vcd, str):  # e.g. True
                vcd = 'dump.vcd'
            elif not vcd.endswith('.vcd'):
                vcd += '.vcd'
        elif self.settings.debug:  # >= DebugLevel.LOW:
            vcd = 'debug_dump.vcd'
        return vcd


class FPGA(BaseModel):
    part: str
    vendor: NoneStr = None
    family: NoneStr = None
    speed_grade: NoneStr = None
    package: NoneStr = None

    def __init__(self, **data) -> None:
        super().__init__(**data)
        if self.part is not None:
            if self.vendor is None:
                if self.part.startswith('LFE'):
                    self.vendor = 'Lattice'
                elif self.part.startswith('xc'):
                    self.vendor = 'Xilinx'

            if self.vendor == 'Lattice' and self.family is None:
                part_splitted = self.part.split('-')
                assert len(part_splitted) == 3
                if part_splitted[0].startswith('LFE5U'):
                    if part_splitted[0] == 'LFE5UM':
                        self.family = 'ecp5'  # With SERDES
                        self.has_serdes = True
                    if part_splitted[0] == 'LFE5UM5G':
                        self.family = 'ecp5-5g'
                    elif part_splitted[0] == 'LFE5U':
                        self.family = 'ecp5'
                    self.capacity = part_splitted[1][:-1] + 'k'
                    spg = part_splitted[2]
                    self.speed = spg[0]
                    package = spg[1:-1]
                    if package.startswith('BG'):
                        package = 'CABGA' + package[2:]
                    self.package = package
                    self.grade = spg[-1]


class SynthFlow(Flow):
    class Settings(Flow.Settings):
        """target clock period in nano-seconds"""
        clock_period: float
        fpga: Optional[FPGA]
        tech: Optional[str]


class DseFlow(Flow):
    pass
