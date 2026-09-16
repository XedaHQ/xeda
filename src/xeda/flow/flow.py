"""EDA flow abstraction"""

from __future__ import annotations

import inspect
import logging
import os
import re
import shutil
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union, get_origin

# from attrs import define
import jinja2
from box import Box
from jinja2 import ChoiceLoader, PackageLoader, StrictUndefined
from pydantic.fields import SHAPE_LIST, SHAPE_SINGLETON, ModelField

from ..dataclass import Field, ValidationError, XedaBaseModel, validation_errors, validator
from ..design import Design
from ..utils import (
    XedaException,
    camelcase_to_snakecase,
    expand_env_vars,
    parse_patterns_in_file,
    regex_match,
    try_convert,
    unique,
)

log = logging.getLogger(__name__)


__all__ = [
    "COMMON_RESULT_DESCRIPTIONS",
    "Flow",
    "FlowFatalError",
    "FlowSettingsError",
    "FlowSettingsException",
    "describe_results",
]


def expand_paths(field: Optional[ModelField], value, mapping):
    if field is None or field.type_ not in (Path, Optional[Path]):
        return value
    if isinstance(value, (tuple, list)):
        return [expand_paths(field, v, mapping) for v in value]
    if isinstance(value, (str, Path)) and value and not os.path.isabs(value):
        for pattern, repl in mapping.items():
            if not os.path.isabs(value):
                pat = re.escape(pattern + os.sep) + r"?"
                value = Path(re.sub(pat, str(repl), str(value), count=1))
                log.debug("Expanded path value for %s as %s", field.name, value.absolute())
    return value


registered_flows: Dict[str, Tuple[str, Type[Flow]]] = {}

DictStrPath = Dict[str, Union[str, os.PathLike]]


#: Descriptions of result keys that several flows report with the same meaning. A flow declares
#: the subset it actually reports through `describe_results`, so `xeda list-results <flow>` never
#: advertises a key that flow does not produce.
COMMON_RESULT_DESCRIPTIONS: Dict[str, str] = {
    # timing
    "Fmax": "Maximum achievable clock frequency in MHz, derived from the constrained period and "
    "the worst negative slack. This is the achieved result, unlike `clock_frequency`.",
    "clock_period": "Constrained clock period in nanoseconds, as given to the tool.",
    "clock_frequency": "Constrained clock frequency in MHz. This is the constraint that was "
    "given to the tool, not the achieved maximum -- see `Fmax`.",
    "clock_name": "Name of the clock the reported timing refers to.",
    "clock_port": "Design port the reported clock is attached to.",
    "wns": "Worst negative slack (ns) over all setup paths. Negative means setup timing is not "
    "met.",
    "whs": "Worst hold slack (ns) over all hold paths. Negative means a hold violation.",
    "tns": "Total negative slack (ns), summed over every failing setup endpoint.",
    "setup_wns": "Worst negative slack (ns) on setup paths.",
    "hold_wns": "Worst negative slack (ns) on hold paths.",
    "worst_slack": "Worst slack (ns) over all analyzed paths.",
    "setup_violations": "Number of endpoints that fail setup timing.",
    "hold_violations": "Number of endpoints that fail hold timing.",
    "num_violating_paths": "Number of paths that fail setup timing.",
    "hold_num_violating_paths": "Number of paths that fail hold timing.",
    "minimum_period": "Minimum achievable clock period in nanoseconds.",
    "maximum_frequency": "Maximum achievable clock frequency in MHz.",
    "f_max": "Maximum achievable clock frequency in MHz.",
    # FPGA resources
    "lut": "Number of LUTs used.",
    "LUT": "Number of LUTs used.",
    "ff": "Number of flip-flops (registers) used.",
    "FF": "Number of flip-flops (registers) used.",
    "slice": "Number of slices / logic elements occupied.",
    "dsp": "Number of DSP blocks used.",
    "bram": "Number of block RAM primitives used.",
    # ASIC area
    "area": "Cell area of the synthesized design, in the technology's area unit.",
    "design_area": "Area of the placed design, in `design_area_unit`.",
    "design_area_unit": "Unit the reported `design_area` is expressed in.",
    "utilization_percent": "Core area utilization, in percent.",
    "utilization_ratio": "Core area utilization, as a ratio in 0..1.",
    "aspect_ratio": "Core height divided by core width.",
    "area_combinational": "Area of the combinational cells.",
    "area_noncombinational": "Area of the sequential (state-holding) cells.",
    "area_macro_bbox": "Area of macros and black boxes.",
    "area_cell_total": "Total cell area, excluding net interconnect.",
    "area_total": "Total area, including net interconnect where the tool reports it.",
    "area_core": "Area of the core region.",
    "io": "Number of I/O buffers/pads used.",
    "timing_met": "Whether every constrained clock domain met its constraint.",
    "clock_domains": "Number of clock domains the timing analysis reported.",
}


def describe_results(*keys: str, **extra: str) -> Dict[str, str]:
    """Build a flow's `results_description` from the shared vocabulary plus its own keys.

    Positional arguments name keys from `COMMON_RESULT_DESCRIPTIONS`; keyword arguments add or
    override flow-specific ones.
    """
    described = {}
    for key in keys:
        try:
            described[key] = COMMON_RESULT_DESCRIPTIONS[key]
        except KeyError:
            raise KeyError(
                f"No shared description for result key {key!r}. Add it to "
                "COMMON_RESULT_DESCRIPTIONS, or pass it as a keyword argument."
            ) from None
    described.update(extra)
    return described


class Flow(metaclass=ABCMeta):
    """A flow may run one or more tools and is associated with a single set of settings and a single design.
    All tool executables should be available on the installed system or on the same docker image."""

    name: str  # set automatically
    aliases: List[str] = []  # list of alternative names for the flow
    incremental: bool = False
    copied_resources_dir: str = "copied_resources"

    #: Documentation for the flow-specific keys this flow writes to `results` (and therefore to
    #: `results.json`). Reported by `xeda list-results <flow>`. Build it with `describe_results`.
    results_description: Dict[str, str] = {}

    #: Additive canonical aliases for results, as canonical_key -> candidate keys in preference
    #: order. After `parse_reports`, the first candidate that is present is copied to the
    #: canonical key unless that key is already set. Nothing is ever renamed or removed, so
    #: existing consumers of `results.json` are unaffected.
    results_canonical_aliases: Dict[str, Tuple[str, ...]] = {
        "Fmax": ("Fmax", "f_max", "maximum_frequency"),
        "lut": ("lut", "LUT"),
        "ff": ("ff", "FF"),
    }

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        # design_root: InitVar[Optional[Path]]
        verbose: int = Field(
            0, description="Verbosity level. Higher values make the tools more talkative."
        )
        # debug: DebugLevel = Field(DebugLevel.NONE.value, hidden_from_schema=True)
        debug: bool = Field(
            False,
            description="Run the flow in debug mode: verbose logging, and exceptions are re-raised "
            "instead of being reported as a failure.",
        )
        quiet: bool = Field(False, description="Run the flow quietly.")
        redirect_stdout: bool = Field(
            False, description="Redirect stdout from execution of tools to files."
        )
        runner_cwd_: Optional[Path] = Field(None, hidden_from_schema=True)
        design_root_: Optional[Path] = Field(None, hidden_from_schema=True)
        timeout_seconds: int = Field(3600 * 2, hidden_from_schema=True)
        nthreads: Optional[int] = Field(
            None,
            alias="ncpus",
            description="Max number of threads",
        )
        no_console: bool = Field(False, hidden_from_schema=True)
        reports_dir: Path = Field(Path("reports"), hidden_from_schema=True)
        checkpoints_dir: Path = Field(Path("checkpoints"), hidden_from_schema=True)
        outputs_dir: Path = Field(Path("outputs"), hidden_from_schema=True)
        clean: bool = Field(
            False, description="Remove the contents of the run directory before running the flow."
        )
        lib_paths: List[
            Union[
                Tuple[
                    str,  # library name/identifier
                    Union[str, Path, None],  # optional library path
                ],
                Tuple[None, Union[str, Path]],  # or just the path
                # both name and path can't be None
            ]
        ] = Field(
            [],
            description="Additional libraries specified as a list of (name, path) tuples. Either name or path can be none. A single string or a list of string is converted to a mapping of library names without paths",
        )
        docker: Optional[str] = Field(
            None,
            description="Use this docker image to run the tools, overriding flow's default pick.",
        )
        dockerized: bool = Field(False, description="Run tools from docker")
        print_commands: bool = Field(True, description="Print executed commands")
        console_colors: bool = Field(True, description="Colorize tool output on the console.")

        @validator("*", pre=True, always=False)
        def _all_fields_validator_subs_env_vars(
            cls, value, values: Dict[str, Any], field: ModelField
        ):
            if value is not None:
                origin = get_origin(field.annotation)
                if field.shape == SHAPE_LIST and (origin is list) and isinstance(value, str):
                    return value.split(",")
                if (
                    field.shape == SHAPE_SINGLETON
                    and field.type_ in (Optional[Path], Path)
                    # and field.annotation in (Optional[Path], Path)
                    and (origin is None or origin == Union)
                    and isinstance(value, (str, Path))
                    and isinstance(values, dict)
                ):
                    log.debug(
                        f"field: {field}, value: {value} origin: {origin} anno: {field.annotation} {type(field.annotation)}"
                    )
                    return expand_env_vars(
                        value,
                        # fmt: off
                        {
                            "PWD": values.get("runner_cwd_"), 
                            "DESIGN_ROOT": values.get("design_root_"), # we don't know DESIGN_ROOT, so just ignore it
                            "DESIGN_DIR": values.get("design_root_"), # we don't know DESIGN_ROOT, so just ignore it
                        },
                        # fmt: on
                    )
            return value

        @validator("verbose", pre=True, always=True)
        def _validate_verbose(cls, value):
            if not isinstance(value, int):
                return try_convert(value, int, 0)
            return value

        @validator("quiet", pre=True, always=True)
        def _validate_quiet(cls, value, values):
            if values.get("verbose") or values.get("debug"):
                return False
            return value

        def __init__(self, **data: Any) -> None:
            try:
                log.debug("Settings.__init__(): data=%s", data)
                super().__init__(**data)
            except ValidationError as e:
                if data.get("debug", None):
                    raise e
                raise FlowSettingsError(validation_errors(e.errors()), e.model, e.json()) from e

    class Results(Box):
        """Flow results"""

        def __init__(
            self,
            *args,
            **kwargs,
        ) -> None:
            kwargs |= {
                "box_intact_types": [Box],
                "box_class": Box,
            }
            self.success: bool = False
            self.tools: List[Any] = []
            self.artifacts: Box = Box()
            # "Time of the execution of run() in fractional seconds.
            # Initialized with None and set only after execution has finished."
            self.runtime: Optional[float] = None
            super().__init__(*args, **kwargs)

    @property
    def succeeded(self) -> bool:
        return self.results.success  # pyright: ignore

    def __init_subclass__(cls) -> None:
        cls_name = cls.__name__
        mod_name = cls.__module__
        log.info("registering flow %s from %s", cls_name, mod_name)
        cls.name = camelcase_to_snakecase(cls_name)
        if not inspect.isabstract(cls):
            # register under the canonical (snake_case) name first, then the class name and any
            # aliases. `cls.name` is what the CLI and the docs use, so it must be resolvable
            # without relying on a lossy snake_case -> CamelCase round-trip (e.g. OpenXC7).
            for name in unique([cls.name, cls_name] + cls.aliases):
                if name in registered_flows:
                    log.warning("Duplicate name: %s while registering flow %s", name, cls_name)
                registered_flows[name] = (mod_name, cls)

    def init(self) -> None:
        """Flow custom initialization stage. At this point, more properties have been set than during __init__
        This is usually the most appropriate place for initialization task.
        Any dependent flows should be registered here by using add_dependency
        """

    def add_dependency(
        self,
        dep_flow_class: Union[Type[Flow], str],
        dep_settings: Settings,
        copy_resources: List[str] = [],
    ) -> None:
        """
        dep_flow_class:   dependency Flow class
        dep_settings:     settings for dependency Flow
        copy_resources:   copy these resources to dependency before running.
                            All resources should be _within_ the depender (parent) run_path and the
                            paths should be _relative_ to depender's run_path.
        """
        self.dependencies.append((dep_flow_class, dep_settings, copy_resources))

    @classmethod
    def _create_jinja_env(
        cls,
        extra_modules: Optional[List[str]] = None,
        trim_blocks=False,
        lstrip_blocks=False,
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
            trim_blocks=trim_blocks,
            lstrip_blocks=lstrip_blocks,
        )

    @property
    def design_root(self):
        return self.design.design_root

    def __init__(
        self,
        settings: Union[Settings, Dict],
        design: Union[Design, Dict],
        run_path: Optional[Path] = None,
        runner_cwd: Optional[Path] = None,
    ):
        """Flow constructor
        should avoid overriding in subclasses unless absolutely needed.
        """
        if run_path is None:
            run_path = Path.cwd()
        self.run_path = run_path

        if isinstance(design, dict):
            if design.get("design_root") is None:
                design["design_root"] = run_path
            design = Design(**design)
        assert isinstance(design, Design), "design is not a Design object"

        self.design: Design = design
        # the path from which the runner was invoked, e.g., where xeda CLI was invoked
        self.runner_cwd: Optional[Path] = runner_cwd
        self.init_time: Optional[float] = None
        self.timestamp: Optional[str] = None
        self.flow_hash: Optional[str] = None
        self.design_hash: Optional[str] = None

        if isinstance(settings, dict):
            settings = self.Settings(**settings)

        assert isinstance(settings, self.Settings)
        # if we don't have a runner_cwd, use the one in Settings, otherwise set settings.runner_cwd_ if it's None
        if runner_cwd is None:
            runner_cwd = settings.runner_cwd_
        elif settings.runner_cwd_ is None:
            settings.runner_cwd_ = runner_cwd
        settings.outputs_dir = self.process_path(settings.outputs_dir)
        settings.reports_dir = self.process_path(settings.reports_dir)
        settings.checkpoints_dir = self.process_path(settings.checkpoints_dir)
        self.settings = settings
        assert isinstance(self.settings, self.Settings)

        # generated artifacts as a dict of category to list of file paths
        self.artifacts = Box()
        self.results = self.Results()
        self.jinja_env = self._create_jinja_env(extra_modules=[self.__module__])
        self.add_template_filter("quote", lambda x: f'"{x}"')
        self.add_template_test("match", regex_match)
        self.dependencies: List[Tuple[Union[Type[Flow], str], Flow.Settings, List[str]]] = []
        self.completed_dependencies: List[Flow] = []

    def pop_dependency(self, typ: Type[Flow]) -> Flow:
        assert inspect.isclass(typ) and issubclass(typ, Flow), f"{typ} is not a subclass of Flow"
        for i in range(len(self.completed_dependencies) - 1, -1, -1):
            if isinstance(self.completed_dependencies[i], typ):
                dep = self.completed_dependencies.pop(i)
                assert isinstance(dep, typ), f"Dependency: {dep} is not of type {typ}"
                return dep
        raise ValueError(f"No {typ.__name__} found in completed_dependencies")

    @abstractmethod
    def run(self) -> None:
        """return False on failure"""

    def clean(self):
        pass

    def purge_run_path(self):
        if self.run_path.exists():
            logged_warning = False
            for path in self.run_path.iterdir():
                if not logged_warning:
                    log.info("Deleting all files in the existing run directory: %s", self.run_path)
                    logged_warning = True
                if path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)

    def parse_reports(self) -> bool:
        log.debug("No parse_reports action for %s", self.name)
        return True

    def add_canonical_result_aliases(self) -> None:
        """Copy flow-specific result keys to their canonical names, additively.

        Flows historically reported the same quantity under different names (`Fmax` vs `f_max`,
        `lut` vs `LUT`), which made `results.json` awkward to consume across flows. This adds the
        canonical name alongside whatever the flow already reported; see
        `results_canonical_aliases`.
        """
        for canonical, candidates in self.results_canonical_aliases.items():
            if self.results.get(canonical) is not None:
                continue
            for candidate in candidates:
                value = self.results.get(candidate)
                if value is not None:
                    self.results[canonical] = value
                    break

    def copy_from_template(
        self, resource_name, lstrip_blocks=False, trim_blocks=False, script_filename=None, **kwargs
    ) -> Path:
        template = self.jinja_env.get_template(resource_name)
        template.environment.lstrip_blocks = lstrip_blocks
        template.environment.trim_blocks = trim_blocks
        script_path = Path(script_filename) if script_filename else self.run_path / resource_name

        log.debug("generating %s from template.", str(script_path.resolve()))
        rendered_content = template.render(
            settings=self.settings,
            design=self.design,
            artifacts=self.artifacts,
            **kwargs,
        )
        with open(script_path, "w") as f:
            f.write(rendered_content)
        return script_path.resolve().relative_to(self.run_path)

    def add_template_filter(self, filter_name: str, func, replace_existing=False) -> None:
        assert filter_name
        if filter_name in self.jinja_env.filters:
            if not replace_existing:
                raise ValueError(f"Template filter with name {filter_name} already exists!")
        self.jinja_env.filters[filter_name] = func

    def add_template_filter_func(self, func) -> None:
        self.add_template_filter(func.__name__, func)

    def add_template_global_func(self, func, filter_name=None) -> None:
        if not filter_name:
            filter_name = func.__name__
        assert filter_name
        if filter_name in self.jinja_env.globals:
            raise ValueError(f"Template global with name {filter_name} already exists!")
        self.jinja_env.globals[filter_name] = func

    def add_template_test(self, filter_name: str, func) -> None:
        self.jinja_env.tests[filter_name] = func

    def parse_report_regex(
        self,
        reportfile_path: Union[str, os.PathLike],
        re_pattern: Union[str, List[str]],
        *other_re_patterns: Union[str, List[str]],
        dotall: bool = True,
        required: bool = False,
        sequential: bool = False,
    ) -> bool:
        res = self.parse_regex(
            reportfile_path,
            re_pattern,
            *other_re_patterns,
            dotall=dotall,
            required=required,
            sequential=sequential,
        )
        if not res:
            return False
        if "success" not in res:
            res["success"] = True
        self.results.update(**res)
        return self.results.success

    def parse_regex(
        self,
        reportfile_path: Union[str, os.PathLike],
        re_pattern: Union[str, List[str]],
        *other_re_patterns: Union[str, List[str]],
        dotall: bool = True,
        required: bool = False,
        sequential: bool = False,
    ) -> Optional[dict]:
        if not isinstance(reportfile_path, Path):
            reportfile_path = Path(reportfile_path)
        if not reportfile_path.exists():
            log.warning(
                "File %s does not exist! Please check the console output and the log files in %s",
                reportfile_path.absolute(),
                self.run_path,
            )
            return None
        return parse_patterns_in_file(
            reportfile_path,
            re_pattern,
            *other_re_patterns,
            dotall=dotall,
            required=required,
            sequential=sequential,
        )

    def resolve_paths_to_design_or_cwd(self, paths: list) -> List[Path]:
        """Resolve relative paths to variables ($PWD), design_root if needed"""
        return [
            self.process_path(path, subs_vars=True, resolve_to=self.design.design_root)
            for path in paths
        ]

    def process_path(
        self,
        path: Union[str, Path],
        *,
        subs_vars: bool = True,
        resolve_to: Optional[Path] = None,
    ) -> Path:
        if subs_vars and isinstance(path, (str, Path)):
            path = expand_env_vars(
                path,
                overrides={
                    "DESIGN_ROOT": self.design.design_root,
                    "DESIGN_DIR": self.design.design_root,
                    "PWD": self.runner_cwd,
                },
            )
        if not isinstance(path, Path):
            path = Path(path)
        if resolve_to is not None and not path.is_absolute() and not str(path).startswith("$"):
            return resolve_to / path
        return path

    def normalize_path_to_design_root(self, path: Union[str, Path]) -> Path:
        return self.process_path(
            path,
            subs_vars=False,
            resolve_to=self.design.design_root,
        )


class FlowException(XedaException):
    """Super-class of all flow exceptions"""


class FlowDependencyFailure(FlowException):
    """Error during execution of a dependency flow"""


class FlowSettingsException(FlowException):
    """Validation of settings failed
    This is a fatal error and the flow should not be run.
    """


class FlowSettingsError(FlowSettingsException):
    """Validation of settings failed.
    The constructor of this subclass takes a list of tuples from pydantic validation errors.
    """

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
        return "{}: {} error{} validating {}\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            self.model.__qualname__,
            "\n".join(f"   {msg}: {loc} ({typ})" for loc, msg, _, typ in self.errors),
        )


class FlowFatalError(FlowException):
    """Other fatal errors"""
