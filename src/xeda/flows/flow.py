"""EDA flow abstraction"""
from __future__ import annotations

import inspect
import logging
import multiprocessing
import os
import re
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from typeguard import typechecked
import jinja2
import psutil
from box import Box
from jinja2 import ChoiceLoader, PackageLoader, StrictUndefined

from ..dataclass import (
    Field,
    ValidationError,
    XedaBaseModel,
    root_validator,
    validation_errors,
    validator,
)
from ..design import Design
from ..tool import Tool
from ..fpga import FPGA
from ..utils import camelcase_to_snakecase, try_convert, unique
from .cocotb import Cocotb, CocotbSettings

log = logging.getLogger(__name__)


__all__ = [
    "Flow",
    "FlowSettingsError",
    "FlowFatalError",
    "SimFlow",
    "SynthFlow",
    "Tool",
]


def regex_match(
    string: str, pattern: str, ignorecase: bool = False
) -> Optional[re.Match]:
    if not isinstance(string, str):
        return None
    return re.match(pattern, string, flags=re.I if ignorecase else 0)


def removesuffix(s: str, suffix: str) -> str:
    """similar to str.removesuffix in Python 3.9+"""
    return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, suffix: str) -> str:
    """similar to str.removeprefix in Python 3.9+"""
    return s[len(suffix) :] if suffix and s.startswith(suffix) else s


registered_flows: Dict[str, Tuple[str, Type["Flow"]]] = {}


DictStrPath = Dict[str, Union[str, os.PathLike]]


T = TypeVar("T", bound="Flow")


@typechecked
class Flow(metaclass=ABCMeta):
    """A flow may run one or more tools and is associated with a single set of settings and a single design.
    All tool executables should be available on the installed system or on the same docker image."""

    name: str  # set automatically

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        verbose: int = Field(0)
        # debug: DebugLevel = Field(DebugLevel.NONE.value, hidden_from_schema=True)
        debug: bool = Field(False)
        quiet: bool = Field(True)

        @validator("verbose", pre=True, always=True)
        def _validate_verbose(cls, value):
            if isinstance(value, (bool, str)):
                return int(value)
            return value

        @validator("quiet", pre=True, always=True)
        def _validate_quiet(cls, value, values):
            if values.get("verbose") or values.get("debug"):
                return False
            return value

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
        outputs_dir: str = Field("outputs", hidden_from_schema=True)
        clean: bool = False  # TODO remove!
        lib_paths: List[
            Union[
                Tuple[
                    str,  # library name/identifier
                    Union[None, str, Path],  # optional library path
                ],
                Tuple[None, Union[str, Path]],  # or just the path
                # both name and path can't be None
            ]
        ] = Field(
            [],
            description="Additional libraries specified as a list of (name, path) tuples. Either name or path can be none. A single string or a list of string is converted to a mapping of library names without paths",
        )
        dockerized: bool = Field(False, description="Run tools from docker")

        @validator("lib_paths", pre=True)
        def _lib_paths_validator(cls, value):  # pylint: disable=no-self-argument
            if isinstance(value, str):
                value = [(value, None)]
            elif isinstance(value, (list, tuple)):
                value = [(x, None) if isinstance(x, str) else x for x in value]
            return value

        def __init__(self, **data: Any) -> None:
            try:
                log.debug("Settings.__init__(): data=%s", data)
                super().__init__(**data)
            except ValidationError as e:
                raise FlowSettingsError(
                    validation_errors(e.errors()), e.model, e.json()  # type: ignore
                ) from e

    def run_tool(self, tool: Tool):
        """run a tool"""
        tool.design_root = self.design_root
        tool.run()

    @property
    def succeeded(self) -> bool:
        return self.results.success

    def __init_subclass__(cls) -> None:
        cls_name = cls.__name__
        mod_name = cls.__module__
        log.info("registering flow %s from %s", cls_name, mod_name)
        cls.name = camelcase_to_snakecase(cls_name)
        if not inspect.isabstract(cls):
            registered_flows[cls.name] = (mod_name, cls)

    def init(self) -> None:
        """Flow custom initialization stage. At this point, more properties have been set than during __init__
        This is usually the most appropriate place for initialization task.
        Any dependent flows should be registered here by using add_dependency
        """

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
        # assert isinstance(self.settings, self.Settings)
        self.design: Design = design
        self.design_root = design._design_root

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
        self.results: Box = Box(
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

    def pop_dependency(self, typ: Type[T]) -> Flow:
        assert inspect.isclass(typ) and issubclass(
            typ, Flow
        ), f"{typ} is not a subclass of Flow"
        for i in range(len(self.completed_dependencies) - 1, -1, -1):
            if isinstance(self.completed_dependencies[i], typ):
                dep = self.completed_dependencies.pop(i)
                assert isinstance(dep, typ), f"Dependency: {dep} is not of type {typ}"
                return dep
        raise ValueError(f"No {typ.__name__} found in completed_dependencies")

    @abstractmethod
    def run(self) -> None:
        """return False on failure"""

    def parse_reports(self) -> bool:
        log.info("No parse_reports action for %s", self.name)
        return True

    def copy_from_template(self, resource_name, **kwargs) -> os.PathLike:
        template = self.jinja_env.get_template(resource_name)
        script_path: Path = self.run_path / resource_name
        log.debug("generating %s from template.", str(script_path.resolve()))
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
        reportfile_path: Union[str, os.PathLike],
        re_pattern: Union[str, List[str]],
        *other_re_patterns: Union[str, List[str]],
        dotall: bool = True,
        required: bool = False,
        sequential: bool = False,
    ) -> bool:
        if not isinstance(reportfile_path, Path):
            reportfile_path = Path(reportfile_path)
        # TODO fix debug and verbosity levels!
        if not reportfile_path.exists():
            log.warning(
                "File %s does not exist! Most probably the flow run had failed.\n Please check log files in %s",
                reportfile_path,
                self.run_path,
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
                    log.debug("%s: %s", k, self.results.get(k))
                if sequential:
                    content = content[match.span(0)[1] :]
                    log.debug("len(content)=%d", len(content))
                return True, content

            for pat in [re_pattern, *other_re_patterns]:
                matched = False
                if isinstance(pat, list):
                    log.debug("Matching any of: %s", pat)
                    for subpat in pat:
                        matched, content = match_pattern(subpat, content)
                else:
                    log.debug("Matching: %s", pat)
                    matched, content = match_pattern(pat, content)

                if not matched and required:
                    log.critical(
                        "Error parsing report file: %s\n Pattern not matched: %s\n",
                        rpt_file.name,
                        pat,
                    )
                    return False
        return True


class SimFlow(Flow, metaclass=ABCMeta):
    """superclass of all simulation flows"""

    cocotb_sim_name: Optional[str] = None

    class Settings(Flow.Settings):
        vcd: Optional[str] = None
        stop_time: Union[None, str, int, float] = None
        cocotb: CocotbSettings = (
            CocotbSettings()
        )  # pyright: reportGeneralTypeIssues=none
        optimization_flags: List[str] = Field([], description="Optimization flags")

        @validator("vcd", pre=True)
        def _validate_vcd(cls, vcd):  # pylint: disable=no-self-argument
            if vcd is not None:
                if isinstance(vcd, bool) or not vcd:
                    vcd = "dump.vcd" if vcd else None
                else:
                    assert isinstance(vcd, str), "`vcd` file name should be string"
                    if vcd[1:].count(".") == 0:  # if it doesn't have an extension
                        vcd += ".vcd"
            return vcd

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        assert isinstance(
            self.settings, self.Settings
        ), "self.settings is not an instance of self.Settings class"
        self.cocotb: Optional[Cocotb] = (
            Cocotb(
                **self.settings.cocotb.dict(),
                sim_name=self.cocotb_sim_name,
            )
            if self.cocotb_sim_name
            else None
        )


class TargetTechnology(XedaBaseModel):
    liberty: Optional[str] = None
    adk: Optional[str] = None
    gates: Optional[str] = None
    lut: Optional[str] = None


class PhysicalClock(XedaBaseModel):
    name: Optional[str] = None
    period: float = Field(
        description="Clock Period (in nanoseconds). Either (and only one of) 'period' OR 'freq' have to be specified."
    )
    freq: float = Field(
        description="Clock frequency (in MegaHertz). Either (and only one of) 'period' OR 'freq' have to be specified."
    )
    rise: float = Field(0.0, description="Rise time (nanoseconds)")
    fall: float = Field(0.0, description="Rall time (nanoseconds)")
    uncertainty: Optional[float] = Field(None, description="Clock uncertainty")
    skew: Optional[float] = Field(None, description="skew")
    port: Optional[str] = Field(None, description="associated design port")

    @validator("fall", always=True)
    def fall_validate(cls, value, values):  # pylint: disable=no-self-argument
        if not value:
            value = round(values.get("period", 0.0) / 2.0, 3)
        return value

    @property
    def duty_cycle(self) -> float:
        return (self.fall - self.rise) / self.period

    @property
    def freq_mhz(self) -> float:
        return 1000.0 / self.period

    @root_validator(pre=True, skip_on_failure=True)
    @classmethod
    def root_validate_phys_clock(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        log.debug("%s root_validator values=%s", cls.__name__, values)
        freq = values.get("freq")
        if "period" in values:
            period = float(values["period"])
            if freq and abs(float(freq) * period - 1000.0) > 0.001:
                raise ValueError(
                    f"Both freq and period cannot be specified at the same time period={values.get('period')} freq={values.get('freq')}"
                )
            values["freq"] = 1000.0 / values["period"]
        else:
            if freq:
                values["period"] = 1000.0 / float(freq)
            else:
                raise ValueError("Neither freq or period were specified")
        if not values.get("name"):
            values["name"] = "main_clock"
        return values


class SynthFlow(Flow, metaclass=ABCMeta):
    """Superclass of all synthesis flows"""

    class Settings(Flow.Settings):
        """base Synthesis flow settings"""

        clock_period: Optional[float] = Field(
            None, description="target clock period in nanoseconds"
        )
        clocks: Dict[str, PhysicalClock] = Field({}, description="Design clocks")
        blacklisted_resources: List[str] = []

        @validator("clocks", pre=True, always=True)
        def clocks_validate(cls, value, values):  # pylint: disable=no-self-argument
            clock_period = values.get("clock_period")
            if not value and clock_period:
                value = {
                    "main_clock": PhysicalClock(name="main_clock", period=clock_period)  # type: ignore
                }
            return value

        @root_validator(pre=True)
        def synthflow_settings_root_validator(cls, values):
            clocks = values.get("clocks")
            if clocks and "main_clock" in clocks and not values.get("clock_period"):
                main_clock = clocks["main_clock"]
                if not isinstance(main_clock, PhysicalClock):
                    if isinstance(main_clock, dict):
                        main_clock = PhysicalClock(**main_clock)
                assert isinstance(
                    main_clock, PhysicalClock
                ), "Specified clock for `main_clock` is neither a PhysicalClock or dict"
                values["clock_period"] = main_clock.period
            return values

    def __init__(self, flow_settings: Settings, design: Design, run_path: Path):
        for clock_name, physical_clock in flow_settings.clocks.items():
            if not physical_clock.port:
                if clock_name not in design.rtl.clocks:
                    raise FlowSettingsError(
                        [
                            (
                                None,
                                f"Physical clock {clock_name} has no corresponding clock port in design. Existing clocks: {', '.join(c for c in design.rtl.clocks)}",
                                None,
                                None,
                            )
                        ],
                        self.Settings,
                    )
                physical_clock.port = design.rtl.clocks[clock_name].port
                flow_settings.clocks[clock_name] = physical_clock
        for clock_name, clock in design.rtl.clocks.items():
            if clock_name not in flow_settings.clocks:
                raise FlowSettingsError(
                    [
                        (
                            None,
                            f"No clock period or frequency was specified for clock: '{clock_name}' (clock port: '{clock.port})'",
                            None,
                            None,
                        )
                    ],
                    self.Settings,
                )
        super().__init__(flow_settings, design, run_path)


class FpgaSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of all FPGA synthesis flows"""

    class Settings(SynthFlow.Settings):
        """base FPGA Synthesis flow settings"""

        fpga: FPGA


class AsicSynthFlow(SynthFlow, metaclass=ABCMeta):
    """Superclass of all ASIC synthesis flows"""

    class Settings(SynthFlow.Settings):
        """base ASIC Synthesis flow settings"""

        tech: Optional[TargetTechnology] = None


class DseFlow(Flow, metaclass=ABCMeta):
    """Superclass of all design-space exploration flows"""


class FlowException(Exception):
    """Super-class of all flow exceptions"""


class FlowDependencyFailure(FlowException):
    """Error during execution of a dependency flow"""


class FlowSettingsError(FlowException):
    """Validation of settings failed"""

    def __init__(
        self,
        errors: List[
            Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]
        ],  # (location, message, context, type)
        model,
        *args: Any,
    ) -> None:
        super().__init__(*args)
        self.errors = errors
        self.model = model

    def __str__(self) -> str:
        return "{}: {} error{} validating {}:\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            self.model.__qualname__,
            "\n".join(
                "{}{}{}{}\n".format(
                    f"{loc}:\n   " if loc else "",
                    msg,
                    f"\ntype: {typ}" if typ else "",
                    f"\ncontext: {ctx}" if ctx else "",
                )
                for loc, msg, ctx, typ in self.errors
            ),
        )


class FlowFatalError(FlowException):
    """Other fatal errors"""
