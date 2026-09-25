"""EDA flow abstraction"""

from __future__ import annotations

import inspect
import logging
import os
import shutil
from abc import ABCMeta, abstractmethod
from copy import deepcopy
from pathlib import Path
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Type,
    Union,
    get_args,
    get_origin,
)

# from attrs import define
import jinja2
from box import Box
from jinja2 import ChoiceLoader, PackageLoader, StrictUndefined

from ..dataclass import (
    Field,
    PrivateAttr,
    ValidationError,
    XedaBaseModel,
    annotation_args,
    field_annotation,
    field_validator,
    input_names,
    model_validator,
    validation_errors,
)
from ..design import Design
from ..utils import (
    XedaException,
    camelcase_to_snakecase,
    expand_env_vars,
    location_free,
    parse_patterns_in_file,
    rebuild_like,
    regex_match,
    semantic_hash,
    tcl_list,
    tcl_quote,
    tcl_word,
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
    "flowrun_hash",
    "is_unset",
]

registered_flows: Dict[str, Tuple[str, Type[Flow]]] = {}

DictStrPath = Dict[str, Union[str, os.PathLike]]


def _is_path_annotation(annotation: Any) -> bool:
    """Whether one annotation leaf represents a filesystem path."""
    return annotation in (Path, os.PathLike) or get_origin(annotation) is os.PathLike


def _annotation_contains_path(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _annotation_contains_path(get_args(annotation)[0])
    if _is_path_annotation(annotation):
        return True
    return any(_annotation_contains_path(arg) for arg in get_args(annotation))


def _is_optional_path(annotation: Any) -> bool:
    """An optional path setting that takes no plain text: `""` can only mean it is unset."""
    if get_origin(annotation) not in (Union, UnionType):
        return False
    members = get_args(annotation)
    return (
        type(None) in members
        and str not in members
        and any(_is_path_annotation(member) for member in members)
    )


def _annotation_matches_value(annotation: Any, value: Any) -> bool:
    """Best-effort matching used to select a container branch of a Union before validation."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _annotation_matches_value(args[0], value)
    if origin in (Union, UnionType):
        return any(_annotation_matches_value(arg, value) for arg in args)
    if annotation is Any:
        return True
    if annotation is type(None):
        return value is None
    if _is_path_annotation(annotation):
        return isinstance(value, (str, os.PathLike))
    if origin is list:
        return isinstance(value, list)
    if origin in (set, frozenset):
        return isinstance(value, (list, set, frozenset))
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_annotation_matches_value(args[0], item) for item in value)
        return len(value) == len(args) and all(
            _annotation_matches_value(item_annotation, item)
            for item_annotation, item in zip(args, value)
        )
    if origin is dict:
        return isinstance(value, dict)
    try:
        return isinstance(value, annotation)
    except TypeError:
        return False


def _is_comma_separated_list(annotation: Any) -> bool:
    """A list setting that does not also accept a plain string, so a string must be a list."""
    accepted = annotation_args(annotation)
    return str not in accepted and any((get_origin(a) or a) is list for a in accepted)


def _expand_path_values(value: Any, annotation: Any, overrides: Dict[str, Any]) -> Any:
    """Expand variables only at path-typed leaves, including nested containers.

    A container must be traversed according to its annotation rather than by value alone: in
    ``lib_paths``, for example, the first tuple member is a library name while only the second is
    a path. Treating every string as a path would corrupt names containing ``$``.
    """
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _expand_path_values(value, args[0], overrides)
    if _is_path_annotation(annotation):
        if isinstance(value, (str, os.PathLike)) and "$" in str(value):
            return expand_env_vars(Path(value), overrides)
        return value
    if origin in (Union, UnionType):
        # Follow the first branch that holds a path and fits the value. A raw string fits both
        # branches of `str | Path`; the presence of the Path branch is what makes it a path.
        for choice in args:
            if _annotation_contains_path(choice) and _annotation_matches_value(choice, value):
                return _expand_path_values(value, choice, overrides)
        return value
    if origin in (list, set, frozenset) and isinstance(value, (list, set, frozenset)):
        item_annotation = args[0] if args else Any
        mapped = [_expand_path_values(item, item_annotation, overrides) for item in value]
        return rebuild_like(value, mapped)
    if origin is tuple and isinstance(value, (list, tuple)):
        if len(args) == 2 and args[1] is Ellipsis:
            mapped = [_expand_path_values(item, args[0], overrides) for item in value]
        else:
            mapped = [
                _expand_path_values(item, args[index] if index < len(args) else Any, overrides)
                for index, item in enumerate(value)
            ]
        return rebuild_like(value, mapped)
    if origin is dict and isinstance(value, dict):
        key_annotation, value_annotation = args if len(args) == 2 else (Any, Any)
        return {
            _expand_path_values(key, key_annotation, overrides): _expand_path_values(
                item, value_annotation, overrides
            )
            for key, item in value.items()
        }
    return value


def flowrun_hash(flow_name: str, settings: Flow.Settings) -> str:
    """What identifies a run of `flow_name` with `settings`: the key `--cached-dependencies`
    reuses a previous run by, and part of a hashed run directory's name.

    It depends on what the settings mean, not on where anything is: a path under the design root
    or under the directory xeda was started from counts relative to it (`$DESIGN_ROOT/c.xdc`),
    so moving a design, or starting xeda elsewhere, keeps the hash. Paths count as their text:
    only a design's source files are hashed by content (`Design.rtl_hash`), and no directory's
    content is ever read.
    """
    roots = [
        (var, Path(root).absolute())
        for var, root in (
            ("DESIGN_ROOT", settings.context.get("design_root")),
            ("PWD", settings.context.get("runner_cwd")),
        )
        if root is not None
    ]
    roots.sort(key=lambda var_root: len(var_root[1].parts), reverse=True)  # most specific first
    return semantic_hash(
        dict(flow_name=flow_name, flow_settings=location_free(settings.model_dump(), roots))
    )


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


def is_unset(value: Any) -> bool:
    """`None`, or an empty string or container: a setting that says nothing.

    Decides which side of a setting shared with a dependency is used; see
    `Flow.Settings.resolve_dependency`. An empty `clocks` is how `SynthFlow` spells "not given".
    `False` and `0` are values.
    """
    if value is None:
        return True
    return isinstance(value, (str, list, tuple, set, frozenset, dict)) and len(value) == 0


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

    #: Settings this flow cannot run without, each with what it is and how to give it (the text
    #: may name the flow as ``{flow}``). Checked when the flow is launched, by
    #: `check_required_settings`, not when its settings are validated: the same settings also sit
    #: inside another flow's settings as a dependency's, where the launching flow supplies what
    #: they lack, so a model that insisted on them could not even be nested.
    required_settings: Dict[str, str] = {}

    @classmethod
    def check_required_settings(cls, settings: "Flow.Settings") -> None:
        """Fail a launch that lacks a `required_settings` entry, naming each and how to give it,
        before anything is set up for the run. A value given in a dependency's section counts
        when the flow shares that setting with the dependency (`dependency_settings`), since
        `resolve_dependency` adopts it from there."""
        dependency_settings = type(settings).dependency_settings
        missing = []
        for name, how in cls.required_settings.items():
            candidates = [getattr(settings, name, None)] + [
                getattr(getattr(settings, field, None), name, None)
                for field, shared in dependency_settings.items()
                if name in shared
            ]
            if all(is_unset(value) for value in candidates):
                missing.append(f"`{name}`, {how.format(flow=cls.name)}")
        if missing:
            raise FlowSettingsException(f"{cls.name} needs " + "; and ".join(missing))

    class Settings(XedaBaseModel):
        """Settings that can affect flow's behavior"""

        # design_root: InitVar[Optional[Path]]
        verbose: int = Field(
            0, description="Verbosity level. Higher values make the tools more talkative."
        )
        # debug: DebugLevel = Field(DebugLevel.NONE.value, json_schema_extra={"hidden_from_schema": True})
        debug: bool = Field(
            False,
            description="Run the flow in debug mode: verbose logging, and exceptions are re-raised "
            "instead of being reported as a failure.",
        )
        quiet: bool = Field(
            False, description="Run the flow quietly, unless `verbose` or `debug` is set."
        )
        redirect_stdout: bool = Field(
            False, description="Redirect stdout from execution of tools to files."
        )
        timeout_seconds: int = Field(3600 * 2, json_schema_extra={"hidden_from_schema": True})
        nthreads: Optional[int] = Field(
            None,
            alias="ncpus",
            description="Max number of threads",
        )
        no_console: bool = Field(False, json_schema_extra={"hidden_from_schema": True})
        reports_dir: Path = Field(Path("reports"), json_schema_extra={"hidden_from_schema": True})
        checkpoints_dir: Path = Field(
            Path("checkpoints"), json_schema_extra={"hidden_from_schema": True}
        )
        outputs_dir: Path = Field(Path("outputs"), json_schema_extra={"hidden_from_schema": True})
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

        #: The settings this flow shares with each of its dependencies, keyed by the field that
        #: holds that dependency's settings: `nextpnr` declares `{"yosys": ("fpga", "clocks")}`.
        #: `resolve_dependency` applies it when the flow launches the dependency.
        dependency_settings: ClassVar[Dict[str, Tuple[str, ...]]] = {}

        # Every flow setting shares three input conveniences, applied by `_normalize_flow_setting`
        # before *any* field validator runs, so a flow's own validators always receive the
        # normalized value: on construction and `settings.json` reload through
        # `_normalize_flow_settings`, on assignment through `__setattr__`. (pydantic runs a
        # subclass's validators ahead of a base class's, so a base *field* validator could not
        # guarantee this; only the few subclass *model* `before` validators see raw input.)

        @classmethod
        def _normalize_flow_setting(cls, name: str, value: Any, roots: Dict[str, Any]) -> Any:
            """One flow setting's input conveniences:

            1. A dependency's settings (`dependency_settings`) are this flow's own object: an
               instance given for them is deep-copied, so two flows never share settings.
            2. A list setting given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`).
               Spaces around items and empty items are dropped, so `""` is an empty list. A
               setting that also accepts plain text keeps the text whole. Likewise an optional
               path given as `""` is unset (`-s textcfg=`): as a `Path` it would name the
               current directory.
            3. `$DESIGN_ROOT`, `$DESIGN_DIR` and `$PWD` (`roots`) are expanded at every `Path`.
            """
            annotation = field_annotation(cls, name)
            if annotation is None:
                return value
            if name in cls.dependency_settings:
                if isinstance(value, XedaBaseModel):
                    copied = value.model_copy(deep=True)
                    copied.invalidate_cached_properties()
                    return copied
                return value
            if isinstance(value, str) and _is_comma_separated_list(annotation):
                value = [item.strip() for item in value.split(",") if item.strip()]
            if value == "" and _is_optional_path(annotation):
                return None
            if value is None or not _annotation_contains_path(annotation):
                return value
            return _expand_path_values(value, annotation, roots)

        #: Where the settings were given: ``design_root`` and ``runner_cwd`` (the directory xeda
        #: was started from). Supplied as pydantic's validation context (`from_input`), never as
        #: a setting, so it is neither written to `settings.json` nor hashed: the same settings
        #: mean the same thing wherever the design lives and wherever xeda is started.
        _context: dict[str, Any] = PrivateAttr(default_factory=dict)

        @classmethod
        def from_input(
            cls,
            data: Mapping[str, Any],
            *,
            design_root: Path | None = None,
            runner_cwd: Path | None = None,
        ):
            """Validate settings as a user gave them: path variables are resolved against
            `design_root` and `runner_cwd`, and invalid input is a `FlowSettingsError` listing
            every problem. The context stays with the settings, so an assignment resolves paths
            the same way.

            Not an `__init__` override: pydantic validates a model with a custom `__init__`
            through that `__init__`, which loses the validation context.
            """
            context = {"design_root": design_root, "runner_cwd": runner_cwd}
            try:
                return cls.model_validate(dict(data), context=context)
            except ValidationError as e:
                if data.get("debug", None):
                    raise e
                raise FlowSettingsError(validation_errors(e.errors()), cls, e.json()) from e

        @property
        def context(self) -> dict[str, Any]:
            """The validation context these settings were given in; see `from_input`."""
            return self._context

        @staticmethod
        def _path_roots(context: Mapping[str, Any] | None) -> dict[str, Any]:
            context = context or {}
            design_root = context.get("design_root")
            return {
                "PWD": context.get("runner_cwd"),
                "DESIGN_ROOT": design_root,
                "DESIGN_DIR": design_root,
            }

        def model_post_init(self, context: Any, /) -> None:
            super().model_post_init(context)
            if isinstance(context, Mapping):
                self._attach_context(context)

        def _attach_context(self, context: Mapping[str, Any]) -> None:
            """Attach input location context to this setting tree.

            Pydantic passes validation context to nested values supplied by the caller, but a
            nested settings object created from a field default has already been constructed by
            then.  Carry the parent's context into every declared dependency as well, so later
            assignment has the same path semantics whether that dependency was explicit or a
            default.
            """
            self._context = {k: context.get(k) for k in ("design_root", "runner_cwd")}
            for field in type(self).dependency_settings:
                dependency = getattr(self, field, None)
                if isinstance(dependency, Flow.Settings):
                    dependency._attach_context(self._context)

        @classmethod
        def _dependency_settings_class(cls, name: str) -> type[Flow.Settings] | None:
            """Return the nested settings class declared for dependency field ``name``."""
            annotation = field_annotation(cls, name)
            if annotation is None:
                return None
            for accepted in annotation_args(annotation):
                if isinstance(accepted, type) and issubclass(accepted, Flow.Settings):
                    return accepted
            return None

        @model_validator(mode="before")
        @classmethod
        def _normalize_flow_settings(cls, values, info):
            """Normalize each supplied flow setting before model validation."""
            if info.field_name is not None and info.data is None:
                return values  # an assignment: `__setattr__` has normalized the assigned value
            roots = cls._path_roots(info.context)
            names = input_names(cls)
            for key, value in values.items():
                if key in names:
                    values[key] = cls._normalize_flow_setting(names[key], value, roots)
            return values

        def __setattr__(self, name: str, value: Any) -> None:
            if name in type(self).model_fields:
                value = type(self)._normalize_flow_setting(
                    name, value, self._path_roots(self.context)
                )
                if name in type(self).dependency_settings:
                    dependency_cls = type(self)._dependency_settings_class(name)
                    if dependency_cls is not None and isinstance(value, Mapping):
                        value = dependency_cls.from_input(value, **self.context)
                    if isinstance(value, Flow.Settings):
                        value._attach_context(self.context)
            super().__setattr__(name, value)

        @property
        def is_quiet(self) -> bool:
            """`quiet`, unless `verbose` or `debug` asks for more output. Decided here, where it
            is read, so `quiet` holds what was written and the order the three were given in
            does not matter."""
            return self.quiet and not (self.verbose or self.debug)

        def resolve_dependency(self, field: str) -> Any:
            """The settings to launch the dependency held in `field` with.

            Each setting shared with it (`dependency_settings[field]`) takes this flow's value
            unless that `is_unset`, and the dependency's own value otherwise; this flow adopts the
            result as well, so the two always run with the same one. Returns a deep copy of the
            dependency's settings carrying the shared values: the settings given for the
            dependency are left exactly as written, which is what keeps the outcome independent
            of the order settings were constructed and assigned in.
            """
            dependency = getattr(self, field).model_copy(deep=True)
            dependency.invalidate_cached_properties()
            for name in type(self).dependency_settings[field]:
                ours, theirs = getattr(self, name), getattr(dependency, name)
                if not is_unset(ours):
                    setattr(dependency, name, deepcopy(ours))
                elif not is_unset(theirs):
                    setattr(self, name, deepcopy(theirs))
            return dependency

        @field_validator("verbose", mode="before")
        @classmethod
        def _validate_verbose(cls, value):
            if not isinstance(value, int):
                return try_convert(value, int, 0)
            return value

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
        """Create the template environment with Xeda filters and helpers."""
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
        env = jinja2.Environment(
            loader=ChoiceLoader(loaderChoices),
            autoescape=False,
            undefined=StrictUndefined,
            trim_blocks=trim_blocks,
            lstrip_blocks=lstrip_blocks,
        )
        # a path a user named goes into a TCL script as a literal word, inside one, or as a list
        env.filters.update(tcl_word=tcl_word, tcl_quote=tcl_quote, tcl_list=tcl_list)
        return env

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
            design = dict(design)
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
            settings = self.Settings.from_input(
                settings, design_root=design.root_path, runner_cwd=runner_cwd
            )

        assert isinstance(settings, self.Settings)
        if runner_cwd is None:
            runner_cwd = settings.context.get("runner_cwd")
            self.runner_cwd = runner_cwd
        if not settings.context or any(value is None for value in settings.context.values()):
            # Attaching context alone would affect only later assignments: path placeholders
            # already present in a directly constructed Settings object would remain unresolved.
            # Revalidate the complete effective value so nested default settings that the caller
            # edited are preserved too.
            settings = self.Settings.from_input(
                settings.model_dump(),
                design_root=settings.context.get("design_root") or design.root_path,
                runner_cwd=settings.context.get("runner_cwd") or runner_cwd,
            )
        assert isinstance(settings, self.Settings)
        settings.outputs_dir = self.process_path(settings.outputs_dir)
        settings.reports_dir = self.process_path(settings.reports_dir)
        settings.checkpoints_dir = self.process_path(settings.checkpoints_dir)
        self.settings = settings
        assert isinstance(self.settings, self.Settings)

        # Artifact labels map to paths or nested mappings/lists/tuples of paths. Relative
        # paths are rooted at run_path; runners walk path leaves through xeda.artifacts.
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
            getattr(self.model, "__qualname__", self.model),
            "\n".join(f"   {msg}: {loc} ({typ})" for loc, msg, _, typ in self.errors),
        )


class FlowFatalError(FlowException):
    """Other fatal errors"""
