"""EDA flow abstraction"""
from __future__ import annotations

import inspect
import logging
import multiprocessing
import os
import re
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

# from attrs import define
import jinja2
import psutil
from box import Box
from jinja2 import ChoiceLoader, PackageLoader, StrictUndefined

from ..dataclass import (
    Field,
    ValidationError,
    XedaBaseModel,
    validation_errors,
    validator,
)
from ..design import Design
from ..utils import camelcase_to_snakecase, regex_match, try_convert_to_primitives, unique

log = logging.getLogger(__name__)


__all__ = [
    "Flow",
    "FlowSettingsError",
    "FlowFatalError",
]


registered_flows: Dict[str, Tuple[str, Type["Flow"]]] = {}

DictStrPath = Dict[str, Union[str, os.PathLike]]


class Flow(metaclass=ABCMeta):
    """A flow may run one or more tools and is associated with a single set of settings and a single design.
    All tool executables should be available on the installed system or on the same docker image."""

    name: str  # set automatically
    incremental: bool = False
    copied_resources_dir: str = "copied_resources"

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        verbose: int = Field(0)
        # debug: DebugLevel = Field(DebugLevel.NONE.value, hidden_from_schema=True)
        debug: bool = Field(False)
        quiet: bool = Field(True)
        redirect_stdout: bool = Field(
            False, description="Redirect stdout from execution of tools to files."
        )

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
            # FIXME default of the host machine not optimal for dockerized or remote execution
            default_factory=multiprocessing.cpu_count,
            description="max number of threads",
        )
        ncpus: int = Field(
            # FIXME default of the host machine not optimal for dockerized or remote execution
            psutil.cpu_count(logical=False),
            description="Number of physical CPUs to use.",
        )
        no_console: bool = Field(False, hidden_from_schema=True)
        reports_dir: Path = Field(Path("reports"), hidden_from_schema=True)
        checkpoints_dir: Path = Field(Path("checkpoints"), hidden_from_schema=True)
        outputs_dir: Path = Field(Path("outputs"), hidden_from_schema=True)
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
        print_commands: bool = Field(True, description="Print executed commands")

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

    class Results(Box):
        """Flow results"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
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
            registered_flows[cls.name] = (mod_name, cls)

    def init(self) -> None:
        """Flow custom initialization stage. At this point, more properties have been set than during __init__
        This is usually the most appropriate place for initialization task.
        Any dependent flows should be registered here by using add_dependency
        """

    def add_dependency(
        self,
        dep_flow_class: Union[Type["Flow"], str],
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
        return self.design._design_root

    def __init__(self, settings: Settings, design: Design, run_path: Path):
        self.run_path = run_path
        self.settings = settings
        assert isinstance(self.settings, self.Settings)
        self.design: Design = design

        self.init_time: Optional[float] = None
        self.timestamp: Optional[str] = None
        self.flow_hash: Optional[str] = None
        self.design_hash: Optional[str] = None

        # generated artifacts as a dict of category to list of file paths
        self.artifacts = Box()
        self.reports: DictStrPath = {}  # DEPRECATED! TODO: remove
        # TODO deprecate and use self.reports
        if self.settings.reports_dir:
            self.settings.reports_dir.mkdir(exist_ok=True, parents=True)
        self.reports_dir = self.settings.reports_dir  # DEPRECATED! use settings.reports_dir
        self.results = self.Results(
            success=False,
            # "Time of the execution of run() in fractional seconds.
            # Initialized with None and set only after execution has finished."
            runtime=None,
            artifacts={},
        )
        self.jinja_env = self._create_jinja_env(extra_modules=[self.__module__])
        self.add_template_test("match", regex_match)
        self.dependencies: List[Tuple[Union[Type["Flow"], str], Flow.Settings, List[str]]] = []
        self.completed_dependencies: List[Flow] = []

    def pop_dependency(self, typ: Type["Flow"]) -> Flow:
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

    def parse_reports(self) -> bool:
        log.debug("No parse_reports action for %s", self.name)
        return True

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
        return script_path.resolve().relative_to(self.run_path)  # resource_name

    def add_template_filter(self, filter_name: str, func) -> None:
        assert filter_name
        if filter_name in self.jinja_env.filters:
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
                    self.results[k] = try_convert_to_primitives(v)
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

    def normalize_path_to_design_root(self, path: Union[str, os.PathLike, Path]) -> Path:
        if not isinstance(path, Path):
            path = Path(path)
        if self.design._design_root and not path.is_absolute():
            path = self.design._design_root / path
        return path


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
