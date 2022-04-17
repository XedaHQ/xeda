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
from ..utils import camelcase_to_snakecase, try_convert, unique
from .cocotb import Cocotb, CocotbSettings

log = logging.getLogger(__name__)


__all__ = [
    "Flow",
    "FlowSettingsError",
    "FlowFatalError",
    "FPGA",
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


class Flow(metaclass=ABCMeta):
    """A flow may run one or more tools and is associated with a single set of settings and a single design.
    All tool executables should be available on the installed system or on the same docker image."""

    name: str  # set automatically

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        quiet: bool = Field(False, hidden_from_schema=True)
        verbose: int = Field(0, hidden_from_schema=True)
        # debug: DebugLevel = Field(DebugLevel.NONE.value, hidden_from_schema=True)
        debug: bool = Field(False, hidden_from_schema=True)
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
        def lib_paths_validator(cls, value):  # pylint: disable=no-self-argument
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
                    validation_errors(e.errors()), e.model  # type: ignore
                ) from None

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
        assert inspect.isclass(typ) and issubclass(typ, Flow)
        for i in range(len(self.completed_dependencies) - 1, -1, -1):
            if isinstance(self.completed_dependencies[i], typ):
                dep = self.completed_dependencies.pop(i)
                assert isinstance(dep, typ)
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
        def validate_vcd(cls, vcd):  # pylint: disable=no-self-argument
            if vcd is not None:
                if isinstance(vcd, bool) or not vcd:
                    vcd = "dump.vcd" if vcd else None
                else:
                    assert isinstance(vcd, str)
                    if vcd[1:].count(".") == 0:  # if it doesn't have an extension
                        vcd += ".vcd"
            return vcd

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        super().__init__(settings, design, run_path)
        assert isinstance(self.settings, self.Settings)
        self.cocotb: Optional[Cocotb] = (
            Cocotb(
                **self.settings.cocotb.dict(),
                sim_name=self.cocotb_sim_name,
            )
            if self.cocotb_sim_name
            else None
        )


class FPGA(XedaBaseModel):
    """FPGA target device"""

    # definition order: part > device > vendor > {family, speed, package, etc}
    part: Optional[str] = Field(None, description="full device part identifier")
    device: Optional[str]
    vendor: Optional[str]
    family: Optional[str]
    generation: Optional[str]
    type: Optional[str]
    speed: Optional[str] = Field(None, description="speed-grade")
    package: Optional[str]
    capacity: Optional[str]
    pins: Optional[int]
    grade: Optional[str]

    def __init__(self, *args: str, **data: Any) -> None:
        if args:
            if len(args) != 1 or not args[0]:
                raise ValueError("Only a single 'part' non-keyword argument is valid.")
            a = args[0]
            if isinstance(a, str):
                if "part" in data:
                    raise ValueError("'part' field already given in keyword arguments.")
                data["part"] = a
            elif isinstance(a, dict):
                if data:
                    raise ValueError("Both dictionary and keyword arguments preset")
                data = a
            else:
                raise ValueError(f"Argument of type {type(a)} is not supported!")
        log.debug("fpga init! data=%s", data)
        super().__init__(**data)

    # this is called before all field validators!
    @root_validator(pre=True)
    def fpga_root_validator(cls, values):  # pylint: disable=no-self-argument
        if not values:
            return values
        # Intel: https://www.intel.com/content/dam/www/central-libraries/us/en/documents/product-catalog.pdf
        # Lattice: https://www.latticesemi.com/Support/PartNumberReferenceGuide
        # Xilinx: https://www.xilinx.com/support/documents/selection-guides/7-series-product-selection-guide.pdf
        #         https://docs.xilinx.com/v/u/en-US/ds890-ultrascale-overview
        #         https://www.xilinx.com/support/documents/selection-guides/ultrascale-fpga-product-selection-guide.pdf
        #         https://www.xilinx.com/support/documents/selection-guides/ultrascale-plus-fpga-product-selection-guide.pdf
        part = values.get("part")

        def set_if_not_exist(attr: str, v: Any) -> None:
            if attr not in values:
                values[attr] = v

        def set_xc_family(s: str):
            d = dict(s="spartan", a="artix", k="kintex", v="virtex", z="zynq")
            s = s.lower()
            if s in d:
                set_if_not_exist("family", d[s])

        if part:
            part = part.strip()
            values["part"] = part
            # speed: 6 = slowest, 8 = fastest
            match_ecp5 = re.match(
                r"^LFE5(U|UM|UM5G)-(\d+)F-(?P<sp>\d)(?P<pkg>[A-Z]+)(?P<pin>\d+)(?P<gr>[A-Z]?)$",
                part,
                flags=re.IGNORECASE,
            )
            if match_ecp5:
                set_if_not_exist("vendor", "lattice")
                set_if_not_exist("family", "ecp5")
                set_if_not_exist("type", match_ecp5.group(1).lower())
                set_if_not_exist("device", "LFE5" + match_ecp5.group(1).upper())
                set_if_not_exist("capacity", match_ecp5.group(2) + "k")
                set_if_not_exist("speed", match_ecp5.group("sp"))
                set_if_not_exist("package", match_ecp5.group("pkg"))
                set_if_not_exist("pins", int(match_ecp5.group("pin")))
                set_if_not_exist("grade", match_ecp5.group("gr"))
                return values
            # Commercial Xilinx # Generation # Family # Logic Cells in 1K units # Speed Grade (-1 slowest, L: low-power) # Package Type
            match_xc7 = re.match(
                r"^(XC)(?P<g>\d)(?P<f>[A-Z])(?P<lc>\d+)-(?P<s>-L?\d)(?P<pkg>[A-Z][A-Z][A-Z]+)(?P<pin>\d\d+)(?P<gr>[A-Z]?)$",
                part,
                flags=re.IGNORECASE,
            )
            if match_xc7:
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", match_xc7.group("g"))
                set_xc_family(match_xc7.group("f"))
                lc = match_xc7.group("lc")
                set_if_not_exist(
                    "device",
                    match_xc7.group(1)
                    + match_xc7.group("g")
                    + match_xc7.group("f")
                    + lc,
                )
                set_if_not_exist("capacity", lc + "K")
                set_if_not_exist("package", int(match_xc7.group("pkg")))
                set_if_not_exist("pins", int(match_xc7.group("pins")))
                set_if_not_exist("grade", match_xc7.group("gr"))
                return values
            match_us = re.match(
                r"^(XC)(?P<f>[A-Z])(?P<g>[A-Z]+)(?P<lc>\d+)-(?P<s>-L?\d)(?P<pkg>[A-Z][A-Z][A-Z]+)(?P<pin>\d\d+)(?P<gr>[A-Z]?)$",
                part,
                flags=re.IGNORECASE,
            )
            if match_us:
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", match_us.group("g"))
                set_xc_family(match_us.group("f"))
                lc = match_us.group("lc")
                set_if_not_exist(
                    "device",
                    match_us.group(1) + match_us.group("g") + match_us.group("f") + lc,
                )
                set_if_not_exist("capacity", lc + "K")
                set_if_not_exist("package", int(match_us.group("pkg")))
                set_if_not_exist("pins", int(match_us.group("pins")))
                set_if_not_exist("grade", match_us.group("gr"))
                return values
            # UltraSCALE+
            # capacity is index to table, roughly x100K LCs
            match_usp = re.match(
                r"^(XC)(?P<f>[A-Z])U(?P<lc>\d+)P-(?P<s>-L?\d)(?P<pkg>[A-Z][A-Z][A-Z]+)(?P<pin>\d\d+)(?P<gr>[A-Z]?)$",
                part,
                flags=re.IGNORECASE,
            )
            if match_usp:
                set_if_not_exist("vendor", "xilinx")
                set_if_not_exist("generation", "usp")
                set_xc_family(match_usp.group("f"))
                lc = match_usp.group("lc")
                set_if_not_exist("capacity", lc)
                set_if_not_exist(
                    "device", match_usp.group(1) + match_usp.group("f") + "U" + lc + "P"
                )
                set_if_not_exist("package", int(match_usp.group("pkg")))
                set_if_not_exist("pins", int(match_usp.group("pins")))
                set_if_not_exist("grade", match_usp.group("gr"))
                return values
        elif not values.get("device") and not values.get("vendor"):
            raise ValueError(
                "Missing enough information about the FPGA device. Please set the 'part' number and/or device, vendor, family, etc."
            )
        return values


class TargetTechnology(XedaBaseModel):
    fpga: Optional[FPGA] = None
    liberty: Optional[str] = None
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
        clocks: Dict[str, PhysicalClock] = {}
        fpga: Optional[FPGA] = None
        tech: Optional[TargetTechnology] = None
        blacklisted_resources: Optional[List[str]]

        @validator("clocks", always=True)
        @classmethod
        def clocks_validate(cls, value, values):  # pylint: disable=no-self-argument
            clock_period = values.get("clock_period")
            if not value and clock_period:
                value = {
                    "main_clock": PhysicalClock(name="main_clock", period=clock_period)  # type: ignore
                }
            return value

        @root_validator()
        @classmethod
        def synthflow_settings_root_validator(cls, values):
            clocks = values.get("clocks")
            if clocks and "main_clock" in clocks and not values.get("clock_period"):
                main_clock = clocks["main_clock"]
                assert isinstance(main_clock, PhysicalClock)
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
            Tuple[Optional[str], str, Optional[str]]
        ],  # (location, message, type/context)
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
                "{}{}\n".format(f"{loc}:\n   " if loc else "", msg)
                for loc, msg, ctx in self.errors
            ),
        )


class FlowFatalError(FlowException):
    """Other fatal errors"""
