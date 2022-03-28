from abc import ABCMeta, abstractmethod
import inspect
from typing import (
    Any,
    ItemsView,
    Dict,
    List,
    Optional,
    OrderedDict,
    Type,
    Tuple,
    TypeVar,
    Union,
    Sequence,
)
import os
import re
from pathlib import Path
import subprocess
import jinja2
from jinja2 import PackageLoader, StrictUndefined, ChoiceLoader
import logging
import multiprocessing
import psutil
from box import Box
import pydantic

from ..design import Design
from ..tool import Tool
from ..utils import camelcase_to_snakecase, try_convert, unique
from ..dataclass import validator, Field, XedaBaseModel, define, asdict
from ..debug import DebugLevel
from .cocotb import Cocotb, CocotbSettings

log = logging.getLogger(__name__)


__all__ = [
    "Flow",
    "FlowSettingsError",
    "FlowFatalException",
    "FPGA",
    "SimFlow",
    "SynthFlow",
]


def regex_match(
    string: str, pattern: str, ignorecase: bool = False
) -> Optional[re.Match]:
    if not isinstance(string, str):
        return None
    return re.match(pattern, string, flags=re.I if ignorecase else 0)


def final_kill(proc: subprocess.Popen):
    try:
        proc.terminate()
        proc.wait()
        proc.kill()
        proc.wait()
    except:
        pass


def removesuffix(s: str, suffix: str) -> str:
    """similar to str.removesuffix in Python 3.9+"""
    return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, suffix: str) -> str:
    """similar to str.removeprefix in Python 3.9+"""
    return s[len(suffix) :] if suffix and s.startswith(suffix) else s


registered_flows: Dict[str, Tuple[str, Type["Flow"]]] = {}


DictStrPath = Dict[str, Union[str, os.PathLike]]

FlowType = TypeVar("FlowType", bound="Flow")
FlowSettingsType = TypeVar("FlowSettingsType", bound="Flow.Settings")


class Flow(metaclass=ABCMeta):
    """A flow may run one or more tools and is associated with a single set of settings and a single design.
    All tool executables should be available on the installed system or on the same docker image."""

    name: str  # set automatically

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        quiet: bool = Field(False, hidden_from_schema=True)
        verbose: int = Field(0, hidden_from_schema=True)
        debug: DebugLevel = Field(DebugLevel.NONE.value, hidden_from_schema=True)
        timeout_seconds: int = Field(3600 * 2, hidden_from_schema=True)
        nthreads: int = Field(
            default_factory=multiprocessing.cpu_count,
            description="max number of threads",
        )
        ncpus: int = Field(
            psutil.cpu_count(logical=False),
            description="Number of physical CPUs to use.",
        )
        no_console: bool = Field(False, hidden_from_schema=True)
        reports_dir: str = Field("reports", hidden_from_schema=True)
        clean: bool = False  # TODO remove!
        lib_paths: List[str] = Field(
            [], description="Additional directories to add to the library search path"
        )

        @validator("lib_paths", pre=True)
        def lib_paths_validator(cls, value):
            if isinstance(value, str):
                value = [value]
            return value

    @define
    class Results:
        success: bool = False
        # "Time of the execution of run() in fractional seconds.
        # Initialized with None and set only after execution has finished."
        runtime: Optional[float] = None

        artifacts: Dict[
            str, str
        ] = {}  # flattened finalized copy of Flow.artifacts TODO

        # dictionary-like API:
        def __getitem__(self, key: str) -> Any:
            assert isinstance(key, str)
            if not hasattr(self, key):
                raise ValueError()
            return getattr(self, key)

        def __setitem__(self, key: str, value) -> None:
            assert isinstance(key, str)
            asdict(self)[key] = value

        def items(self) -> ItemsView[str, Any]:
            return asdict(self).items()

        def get(self, key: str, default=None) -> Any:
            return asdict(self).get(key, default)

        def __contains__(self, item) -> bool:
            return asdict(self).__contains__(item)

        def update(self, *args, **kwargs) -> None:
            d = {}
            if args:
                d = args[0]
                assert isinstance(d, dict)
            d = {**d, **kwargs}
            for k, v in kwargs.items():
                self.__setitem__(k, v)

    @property
    def succeeded(self) -> bool:
        return self.results.success

    def __init_subclass__(cls) -> None:
        cls_name = cls.__name__
        mod_name = cls.__module__
        log.info(f"registering flow {cls_name} from {mod_name}")
        if not inspect.isabstract(cls):
            registered_flows[cls_name] = (mod_name, cls)

        cls.name = camelcase_to_snakecase(cls_name)

    def init(self) -> None:
        """Flow custom initialization stage. At this point, more properties have been set than during __init__
        This is usually the most appropriate place for initialization task.
        Any dependent flows should be registered here by using add_dependency
        """
        pass

    def add_dependency(
        self, dep_flow_class: Type["Flow"], dep_settings: Settings
    ) -> None:
        self.dependencies.append((dep_flow_class, dep_settings))

    @classmethod
    def _create_jinja_env(
        cls, extra_modules: Optional[List[str]] = None
    ) -> jinja2.Environment:
        if extra_modules is None:
            extra_modules = []
        loaderChoices = []
        mod_paths = []
        modules = unique(
            extra_modules + [cls.__module__] + [clz.__module__ for clz in cls.__bases__]
        )
        for mpx in modules:
            for mp in [mpx, mpx.rsplit(".", 1)[0]]:
                if mp not in mod_paths:
                    mod_paths.append(mp)
        for mp in mod_paths:
            try:  # TODO better/cleaner way
                loaderChoices.append(PackageLoader(mp))
            except ValueError:
                pass
        return jinja2.Environment(
            loader=ChoiceLoader(loaderChoices),
            autoescape=False,
            undefined=StrictUndefined,
        )

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        self.run_path = run_path
        self.settings = settings
        assert isinstance(self.settings, self.Settings)
        self.design: Design = design

        self.init_time: Optional[float] = None
        self.timestamp: Optional[str] = None
        self.flow_hash: Optional[str] = None
        self.design_hash: Optional[str] = None

        # a map of "precious" files generated by the flow
        # these files can be used in a subsequent flow depending on this flow
        # everything else in run_path can be deleted by the flow-runner or overwritten in subsequent operations
        # artifacts can be backed up if the same run_path is used after this flow is complete
        self.artifacts = Box()
        self.reports: DictStrPath = {}
        # TODO deprecate and use self.reports
        self.reports_dir = run_path / self.settings.reports_dir
        self.reports_dir.mkdir(exist_ok=True)
        self.results = Box(
            success=False,
            # "Time of the execution of run() in fractional seconds.
            # Initialized with None and set only after execution has finished."
            runtime=None,
            artifacts={},
        )
        self.jinja_env = self._create_jinja_env(extra_modules=[self.__module__])
        self.add_template_test("match", regex_match)
        self.dependencies: List[Tuple[Type[Flow], Flow.Settings]] = []
        self.completed_dependencies: List[Flow] = []

    @abstractmethod
    def run(self) -> None:
        """return False on failure"""

    def parse_reports(self) -> bool:
        log.info("No parse_reports action for %s", self.name)
        return True

    def copy_from_template(self, resource_name, **kwargs) -> os.PathLike:
        template = self.jinja_env.get_template(resource_name)
        script_path: Path = self.run_path / resource_name
        log.debug(f"generating {script_path.resolve()} from template.")
        rendered_content = template.render(
            settings=self.settings,
            design=self.design,
            artifacts=self.artifacts,
            **kwargs,
        )
        with open(script_path, "w") as f:
            f.write(rendered_content)
        return script_path.relative_to(self.run_path)  # resource_name

    def add_template_filter(self, filter_name: str, func) -> None:
        self.jinja_env.filters[filter_name] = func

    def add_template_test(self, filter_name: str, func) -> None:
        self.jinja_env.tests[filter_name] = func

    def conv_to_relative_path(self, src):
        path = Path(src).resolve(strict=True)
        return os.path.relpath(path, self.run_path)

    def parse_report_regex(
        self,
        reportfile_path,
        re_pattern,
        *other_re_patterns,
        dotall=True,
        required=False,
        sequential=False,
    ) -> bool:
        if isinstance(reportfile_path, str):
            reportfile_path = self.run_path / reportfile_path
        # TODO fix debug and verbosity levels!
        if not reportfile_path.exists():
            log.warning(
                f"File {reportfile_path} does not exist! Most probably the flow run had failed.\n Please check log files in {self.run_path}"
            )
            return False
        with open(reportfile_path) as rpt_file:
            content = rpt_file.read()

            flags = re.MULTILINE | re.IGNORECASE
            if dotall:
                flags |= re.DOTALL

            def match_pattern(pat: str, content: str) -> Tuple[bool, str]:
                match = re.search(pat, content, flags)
                if match is None:
                    return False, content
                match_dict = match.groupdict()
                for k, v in match_dict.items():
                    self.results[k] = try_convert(v)
                    log.debug(f"{k}: {self.results.get(k)}")
                if sequential:
                    content = content[match.span(0)[1] :]
                    log.debug(f"len(content)= {len(content)}")
                return True, content

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    log.debug(f"Matching any of: {pat}")
                    for subpat in pat:
                        matched, content = match_pattern(subpat, content)
                else:
                    log.debug(f"Matching: {pat}")
                    matched, content = match_pattern(pat, content)

                if not matched and required:
                    log.critical(
                        f"Error parsing report file: {rpt_file.name}\n Pattern not matched: {pat}\n"
                    )
                    return False
        return True


class SimFlow(Flow):
    cocotb_sim_name: Optional[str] = None

    class Settings(Flow.Settings):
        vcd: Optional[str] = None
        stop_time: Union[None, str, int, float] = None
        cocotb: CocotbSettings = (
            CocotbSettings()
        )  # pyright: reportGeneralTypeIssues=none
        optimization_flags: List[str] = Field([], description="Optimization flags")
        # library_id -> library_path mapping. library_path is optional
        libraries: OrderedDict[str, Union[None, str, os.PathLike]] = OrderedDict()

        @validator("vcd", pre=True)
        def validate_vcd(cls, vcd):
            if vcd is not None:
                if isinstance(vcd, bool):
                    vcd = "dump.vcd" if vcd else None
                else:
                    assert isinstance(vcd, str)
                    if not vcd.endswith(".vcd"):
                        vcd += ".vcd"
            return vcd

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)

        assert isinstance(self.settings, self.Settings)
        self.cocotb: Optional[Cocotb] = (
            Cocotb(**self.settings.cocotb.dict(), sim_name=self.cocotb_sim_name)
            if self.cocotb_sim_name
            else None
        )


class FPGA(XedaBaseModel):
    """FPGA target device"""

    part: Optional[str] = Field(None, description="full device part identifier")
    vendor: Optional[str]
    device: Optional[str]
    family: Optional[str]
    speed: Optional[str] = Field(None, description="speed-grade")
    package: Optional[str]
    capacity: Optional[str]

    # @root_validator(pre=True)
    # def fpga_root_validator(cls, values):
    #     part = values.get('part')
    #     device = values.get('device')
    #     family = values.get('family')
    #     vendor = values.get('vendor')
    #     if device:
    #         if not vendor:
    #             device = device.lower()
    #             if device.startswith('lfe'):
    #                 values['vendor'] = 'lattice'
    #             elif device.startswith('xc'):
    #                 values['vendor'] = 'xilinx'

    #     print(f"values={values}")
    #     raise ValueError('Full part number and/or vendor, family, device, package must be specified')

    @validator("device", pre=True, always=True)
    def part_validator(cls, value, values):
        part = values.get("part")
        if not value and part:
            # TODO more cheking?
            sp = part.split("-")
            value = "-".join(sp[:-1])
        return value

    def __init__(self, **data) -> None:
        super().__init__(**data)
        return
        if self.part:
            self.part = self.part.lower()
            if not self.device:
                self.device = self.part.split("-")[0]
        if self.device:
            self.device = self.device.lower()

            device = self.device.split("-")
            print(f"FPGA init device={device}")
            # exit(1)
            if self.vendor == "lattice":
                assert (
                    len(device) >= 2
                ), "Lattice device should be in form of fffff-ccF-ppppp"
                if device[0].startswith("lfe5u"):
                    print("yes")
                    # exit(1)
                    if device[0] == "lfe5um":
                        self.family = "ecp5"  # With SERDES
                        self.has_serdes = True
                    if device[0] == "lfe5um5g":
                        self.family = "ecp5-5g"
                    elif device[0] == "lfe5u":
                        self.family = "ecp5"
                    self.capacity = device[1][:-1] + "k"
                    if len(device) == 3:
                        spg = device[2]
                        self.speed = spg[0]
                        package = spg[1:-1]
                        if package.startswith("bg"):
                            package = "cabga" + package[2:]
                        self.package = package
                        self.grade = spg[-1]
            elif self.vendor == "xilinx":
                d = device[0]
                if d.startswith("xc7"):
                    self.family = "xc7"
                elif d.startswith("xcu") and d[3] != "p":
                    self.family = "xcu"
                elif d.startswith("xcv") and d[3] != "e":
                    self.family = "xcv"
                elif d.startswith("xc3sda"):
                    self.family = "xc3sda"
                elif d.startswith("xc2vp"):
                    self.family = "xc2vp"
                elif d.startswith("xczu"):
                    self.family = "xcup"
                else:
                    self.family = d[:4]
                # TODO: more
            if not self.part and self.device and self.package and self.speed:
                if self.vendor == "xilinx":
                    self.part = (self.device + self.package + self.speed).lower()

        # if self.part:
        #     if self.vendor == 'xilinx':
        #         if not self.speed:
        #             self.speed = self.part.split('-')[-1]
        #         if not self.package:
        #             sp = self.part.split('-')
        #             if len(sp) > 2:
        #                 self.package = sp[1].remove


class TargetTechnology(XedaBaseModel):
    fpga: Optional[FPGA] = None
    liberty: Optional[str] = None
    gates: Optional[str] = None
    lut: Optional[str] = None


class PhysicalClock(XedaBaseModel):
    name: Optional[str] = None
    period: float = Field(description="period (nanoseconds)")
    rise: float = Field(0.0, description="rise time (nanoseconds)")
    fall: float = Field(0.0, description="fall time (nanoseconds)")
    uncertainty: Optional[float] = Field(None, description="clock uncertainty")
    skew: Optional[float] = Field(None, description="skew")
    port: Optional[str] = Field(None, description="associated design port")

    @validator("fall", always=True)
    def fall_validate(cls, value, values):
        if not value:
            value = round(values.get("period", 0.0) / 2.0, 3)
        return value

    @property
    def duty_cycle(self) -> float:
        return (self.fall - self.rise) / self.period

    @property
    def freq_mhz(self) -> float:
        return 1000.0 / self.period


class SynthFlow(Flow):
    class Settings(Flow.Settings):
        """base Synthesis flow settings"""

        clock_period: Optional[float] = Field(
            None, description="target clock period in nanoseconds"
        )
        clocks: Dict[str, PhysicalClock] = {}

        @validator("clocks", always=True)
        def clocks_validate(cls, value, values):
            clock_period = values.get("clock_period")
            if not value and clock_period:
                value = {
                    "main_clock": PhysicalClock(name="main_clock", period=clock_period)  # type: ignore
                }
            return value

        fpga: Optional[FPGA] = None
        tech: Optional[TargetTechnology] = None
        blacklisted_resources: Optional[List[str]]

    def __init__(self, flow_settings: Settings, design: Design, run_path: Path):
        design_clocks = design.rtl.clocks
        for clock_name, physical_clock in flow_settings.clocks.items():
            if not physical_clock.port:
                try:
                    physical_clock.port = design_clocks[clock_name].port
                    flow_settings.clocks[clock_name] = physical_clock
                except LookupError as e:
                    log.critical(
                        f"Physical clock {clock_name} has no corresponding clock port in design.rtl"
                    )
                    raise e from None
        super().__init__(flow_settings, design, run_path)


class FpgaSynthFlow(SynthFlow):
    class Settings(SynthFlow.Settings):
        """base FPGA Synthesis flow settings"""

        fpga: FPGA


class AsicSynthFlow(SynthFlow):
    class Settings(SynthFlow.Settings):
        """base ASIC Synthesis flow settings"""


class DseFlow(Flow):
    pass


class FlowFatalException(Exception):
    """Fatal error"""

class FlowDependencyFailure(FlowFatalException):
    """Fatal error"""


class FlowSettingsError(Exception):
    """Raised when flow settings are invalid"""

    def __init__(
        self,
        flow: Type[Flow],
        errors: List[Tuple[str, str, str]],
        model,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.flow = flow
        self.errors = errors
        self.model = model
