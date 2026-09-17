from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import pprint
import re
import subprocess
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from enum import Enum
from functools import cached_property
from glob import glob
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic_core import core_schema

from .dataclass import (
    Field,
    SerializeAsAny,
    ValidationError,
    XedaBaseModel,
    field_validator,
    model_validator,
    model_with_allow_extra,
    validation_errors,
)
from .proc_utils import tool_output_redirect
from .utils import (
    NonZeroExitCode,
    WorkingDirectory,
    expand_env_vars,
    expand_hierarchy,
    hierarchical_merge,
    removesuffix,
    settings_to_dict,
    toml_load,
    unique,
)

log = logging.getLogger(__name__)

__all__ = [
    "AnyDesignValidationException",
    "Clock",
    "Design",
    "DesignFileParseError",
    "DesignSource",
    "DesignValidationError",
    "FileResource",
    "LanguageSettings",
    "SourceType",
    "VhdlSettings",
]


def pformat(data):
    return pprint.pformat(data, compact=True, sort_dicts=False).removeprefix("{").removesuffix("}")


class DesignFileParseError(Exception):
    pass


class AnyDesignValidationException(Exception):
    pass


class DesignValidationError(AnyDesignValidationException):
    def __init__(
        self,
        errors: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]],
        data: Optional[Dict[str, Any]] = None,
        *args: object,
        design_root: Union[str, os.PathLike, None] = None,
        design_name: Optional[str] = None,
        file: Optional[str] = None,
        design_in_msg: bool = False,
    ) -> None:
        super().__init__(*args)
        self.errors = errors  # (location, message, context, type)
        self.data = data or {}
        self.design_root = design_root
        self.design_name = design_name
        self.file = file
        self.design_in_msg = design_in_msg

    def __str__(self) -> str:
        def fmt_loc(loc):
            if loc:
                return ".".join(re.split(r"\s*->\s*", loc)) + ":\n "
            return ""

        name = self.design_name or self.data.get("name")
        return "{}: {} error{} validating design{}\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            f" '{name}'" if name else "",
            "\n".join(f"{fmt_loc(loc)}{msg}\n" for loc, msg, _, _ in self.errors),
        ) + (f"\nDesign:\n{pformat(self.data)}\n" if self.data and self.design_in_msg else "")


class FileResource:
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        _root_path: Optional[Path] = None,
        resolve=True,
    ) -> None:
        """
        A file resource
        file: existing file, its existence is checked during validation
        path:
        """
        try:
            if isinstance(path, dict):
                path_value = path.get("path")
                if path_value:
                    resolve = False  # override resolve
                file_value = path.get("file")
                if path_value and file_value:
                    raise ValueError("'file' and 'path' are mutually exclusive.")
                path_value = path_value or file_value
                if not path_value:
                    raise ValueError(
                        "Either 'file' (existing file) or 'path' (unchecked path) must be set for a FireResource."
                    )
                path = path_value
            path = Path(path)
            self._specified_path = path  # keep a copy of path, as specified by user, without resolving or convertint to an absolute path
            if not path.is_absolute():
                if not _root_path:
                    _root_path = Path.cwd()
                path = _root_path / path
            self.file = path.resolve(strict=False) if resolve else path.absolute()
        except FileNotFoundError as e:
            log.error("Design resource '%s' does not exist!", path)
            raise e

    @property
    def path(self) -> Path:
        return self.file

    @cached_property
    def content_hash(self) -> str:
        """return hash of file content"""
        with open(self.file, "rb") as f:
            return hashlib.sha3_256(f.read()).hexdigest()[:32]  # first 128 bits is more than enough

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FileResource):
            return False
        return self.content_hash == other.content_hash and self.file.samefile(other.file)

    def __hash__(self) -> int:
        # path is already absolute
        return hash((self.content_hash, str(self.file)))

    def __str__(self) -> str:
        return str(self.file)

    def __repr__(self) -> str:
        return f"file:{self.file}"

    def get_specified_path(self):
        return self._specified_path

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Validate as an arbitrary type, exactly as `arbitrary_types_allowed` did under v1.

        Values reach here already coerced by the `mode="before"` validators on the fields that
        declare them, so an isinstance check is all that is required. The serializer is
        `when_used="json"` so that `model_dump()` keeps returning the object itself, as v1 did,
        while `model_dump_json()` emits the path string.
        """
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema(), when_used="json"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> Dict[str, Any]:
        """Make this arbitrary type declarable in JSON Schema.

        Without this, `Design.model_json_schema()` raises `PydanticInvalidForJsonSchema`, which
        would leave editors and coding agents without any machine-readable description of a
        design file.
        """
        return cls._json_schema()

    @classmethod
    def _json_schema(cls) -> Dict[str, Any]:
        field_schema: Dict[str, Any] = {}
        field_schema.update(
            title=cls.__name__,
            description=(
                "A file resource: either a path string (relative paths are resolved against the "
                "design file's directory) or an object with a 'file' key (an existing file) or a "
                "'path' key (an unchecked path)."
            ),
            anyOf=[
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Path to an existing file. Mutually exclusive with 'path'.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Path that is not checked for existence. Mutually exclusive with 'file'.",
                        },
                    },
                    # `oneOf`, not `anyOf`: FileResource rejects an object that gives neither
                    # *and* one that gives both, so exactly one branch must match. Under `anyOf`
                    # an object carrying both keys satisfies both branches and validated here
                    # while `Design.from_file` always rejected it.
                    "oneOf": [{"required": ["file"]}, {"required": ["path"]}],
                },
            ],
        )
        return field_schema


class SourceType(str, Enum):
    """Type of a design source file.

    Values are the member names themselves so that serialized settings and results are
    self-describing. NOTE: the declaration order is significant, see `from_str`.
    """

    Verilog = "Verilog"
    VerilogHeader = "VerilogHeader"
    SystemVerilog = "SystemVerilog"
    SVHeader = "SVHeader"
    Vhdl = "Vhdl"
    Bluespec = "Bluespec"
    Xdc = "Xdc"
    Sdc = "Sdc"
    MemoryFile = "MemoryFile"
    Tcl = "Tcl"
    Chisel = "Chisel"
    Cpp = "Cpp"
    Cocotb = "Cocotb"

    def __str__(self) -> str:
        return str(self.name)

    @classmethod
    def from_str(cls, source_type: str) -> Optional[SourceType]:
        try:
            return cls[source_type]
        except KeyError:
            pass
        try:
            return cls[source_type.capitalize()]
        except KeyError:
            pass
        for k, v in cls.__members__.items():
            if k.lower() == source_type.lower():
                return v
        # Backward compatibility: this enum previously used `auto()`, so settings.json files
        # written by older versions of xeda record 1-based ordinals ("5" for Vhdl) instead of
        # names. The declaration order above must not change while this is supported.
        if source_type.isdigit():
            members = list(cls.__members__.values())
            idx = int(source_type)
            if 1 <= idx <= len(members):
                return members[idx - 1]
        return None


class DesignSource(FileResource):
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Union[str, SourceType, None] = None,
        standard: Optional[str] = None,
        variant: Optional[str] = None,
        _root_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(path, dict):
            typ = typ or path.pop("type", None)
            standard = standard or path.pop("standard", None)
            variant = variant or path.pop("variant", None)
            rp = path.pop("root_path", None)
            if not _root_path and rp:
                _root_path = Path(rp)
        super().__init__(path, _root_path=_root_path, **kwargs)

        def type_from_suffix(path: Path) -> Tuple[Optional[SourceType], Optional[str]]:
            type_variants_map = {
                (SourceType.Chisel, None): ["sc"],
                (SourceType.Cpp, None): ["cc", "cpp", "cxx"],
                (SourceType.Vhdl, None): ["vhd", "vhdl"],
                (SourceType.Verilog, None): ["v"],
                (SourceType.VerilogHeader, None): ["vh"],
                (SourceType.SVHeader, None): ["svh"],
                (SourceType.SystemVerilog, None): ["sv"],
                (SourceType.Bluespec, "bsv"): ["bsv"],
                (SourceType.Bluespec, "bh"): ["bs", "bh"],
                (SourceType.Xdc, None): ["xdc"],
                (SourceType.Sdc, None): ["sdc"],
                (SourceType.Tcl, None): ["tcl"],
                (SourceType.Cocotb, None): ["py"],
                (SourceType.MemoryFile, None): ["mem"],
            }
            for (typ, vari), suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return (typ, vari)
            return None, None

        self.variant = variant
        self.type = None
        if isinstance(typ, SourceType):
            self.type = typ
        elif isinstance(typ, str):
            self.type = SourceType.from_str(typ)
        if not self.type:
            self.type, self.variant = type_from_suffix(self.file)
        self.standard = standard

    def __eq__(self, other: Any) -> bool:  # pylint: disable=useless-super-delegation
        # added attributes do not change semantic equality
        return super().__eq__(other)

    def __hash__(self) -> int:  # pylint: disable=useless-super-delegation
        # added attributes do not change semantic identity
        return super().__hash__()

    def __repr__(self) -> str:
        s = f"file:{self.file} type:{self.type}"
        if self.variant:
            s += f" variant: {self.variant}"
        if self.standard:
            s += f" standard: {self.standard}"
        return s

    def __json_encoder__(self) -> str:
        return json.dumps(
            {
                "file": (self.file),
                "type": (self.type),
                "variant": (self.variant),
                "standard": (self.standard),
            }
        )

    @classmethod
    def _json_schema(cls) -> Dict[str, Any]:
        field_schema = super()._json_schema()
        field_schema["description"] = (
            "A design source file: either a path string (relative paths are resolved against the "
            "design file's directory) or an object. The source type is inferred from the file "
            "extension unless 'type' is given explicitly."
        )
        object_schema = field_schema["anyOf"][1]
        object_schema["properties"].update(
            {
                "type": {
                    "type": "string",
                    "enum": [t.name for t in SourceType],
                    "description": "Source type. Inferred from the file extension when omitted.",
                },
                "standard": {
                    "type": "string",
                    "description": "Language standard for this source, e.g. '2008' for VHDL or '2012' for SystemVerilog.",
                },
                "variant": {
                    "type": "string",
                    "description": "Language variant, e.g. 'bsv' or 'bh' for Bluespec sources.",
                },
            }
        )
        return field_schema


DefineType = Any
# the order matters!
# Union[FileResource, int, bool, float, str]

# Tuple -> Tuple[()] (empty tuple), but pydantic 1.9.0 + Python 3.9 typing do not like it
# Tuple of 0, 1, or 2 strings:
Tuple012 = Union[Tuple[str, ...], Tuple[str], Tuple[str, str]]  # xtype: ignore


class DVSettings(XedaBaseModel):
    """Design/Verification settings"""

    sources: List[DesignSource]
    generics: Dict[str, DefineType] = Field(
        default={},
        description="Top-level generics/defines specified as a mapping",
        alias="parameters",
    )  # top defines/generics
    parameters: Dict[str, DefineType] = Field(
        default={},
        description="Top-level generics/defines specified as a mapping",
        alias="generics",
    )
    defines: Dict[str, DefineType] = Field(default={})

    @model_validator(mode="before")
    @classmethod
    def the_root_validator(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        value = values.get("parameters")
        if not value:
            value = values.get("generics")
        if value:
            if isinstance(value, (list)):
                d = dict()
                for e in value:
                    e_name = e.get("name")
                    e_value = e.get("value")
                    if e_name and e_value is not None:
                        d[e_name] = e_value
                    else:
                        raise ValueError(
                            "parameters/generics must be a dictionary or a list of objects with 'name' and 'value' attributes"
                        )
                value = d
            elif not isinstance(value, dict):
                raise ValueError(
                    "parameters/generics must be a dictionary or a list of objects with 'name' and 'value' attributes"
                )
            for k, v in value.items():
                if isinstance(v, dict) and ("file" in v or "path" in v):
                    value[k] = str(FileResource(v))
            values["generics"] = value
            values["parameters"] = value
        return values

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}

        def src_with_type(src, src_type):
            if src_type:
                return {"file": src, "type": src_type}
            return src

        def ds(src: Union[str, Path], typ=None):
            src = expand_env_vars(
                src,
                # fmt: off
                    {
                        "PWD": None, 
                        "DESIGN_ROOT": values.get("design_root")
                    },
                # fmt: on
            )
            return DesignSource(src, typ=typ, _root_path=values.get("design_root"))

        if isinstance(value, (str, Path, DesignSource)):
            value = [value]
        sources: List[DesignSource] = []

        def source_already_exists(src: DesignSource) -> bool:
            for s in sources:
                if s.file.absolute() == src.file.absolute():
                    return True
            return False

        if isinstance(value, (str, Path, FileResource, Mapping)):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError(
                f"'sources' must be a list of source files, got {type(value).__name__}: {value!r}"
            )
        for src in unique(list(value)):
            if isinstance(src, str):
                src_type = None
                # m = re.match(r"^([a-zA-Z0-9_]*)\:(.*)", src)
                # if m:
                #     src = m.group(2)
                #     src_type = SourceType.from_str(m.group(1))
                if src.count("*") > 0:
                    glob_sources = glob(src)
                    srcs = [ds(s, src_type) for s in glob_sources]
                    sources.extend(s for s in srcs if not source_already_exists(s))
                    continue  # skip the append at the bottom
                src = src_with_type(src, src_type)
            if not isinstance(src, DesignSource):
                try:
                    if isinstance(src, (str, Path)):
                        src = ds(src, None)
                    else:
                        src = DesignSource(src)
                except FileNotFoundError as e:
                    raise ValueError(f"'{src}'   {e.strerror}: {e.filename}") from e
            if not source_already_exists(src):
                sources.append(src)
        return sources


class Clock(XedaBaseModel):
    port: str
    name: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _name_validate(cls, value, info) -> str:
        values = info.data if isinstance(info.data, dict) else {}
        return value or values.get("port", None)


class Generator(XedaBaseModel):
    cwd: Optional[str] = None
    executable: Optional[str] = None
    class_: Optional[str] = Field(None, alias="class")
    args: Union[str, List[str]] = []
    command: Optional[str] = None
    check: bool = True
    env: Optional[Dict[str, str]] = None
    # sweepable parameters used in command
    parameters: dict = {}
    sources: List[Union[str, Path]] = Field(
        default_factory=list,
        description="List of design sources used by this generator",
    )
    run_only_if_sources_modified: bool = Field(
        default=True,
        description="If True, the generator will run only if either not all rtl.sources exist or one of the sources has a newer modification time than all the rtl.sources",
    )
    # for xeda to know dependencies, clean previous artifacts, check after generation:
    generated_sources: List[str] = []

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        # sources can contain globs which are expanded
        if isinstance(value, str):
            value = [value]
        sources: List[Path] = []
        for src in unique(value):
            if isinstance(src, str):
                src = str(
                    expand_env_vars(src, {"PWD": None, "DESIGN_ROOT": values.get("design_root")})
                )
                if "*" in src:
                    sources.extend(Path(s).resolve() for s in glob(src))
                else:
                    sources.append(Path(src).resolve())
            elif isinstance(src, Path):
                sources.append(src)
            else:
                raise ValueError(f"Invalid source type: {type(src)} for source '{src}'")
        # remove duplicates, but keep order
        sources = unique(sources)
        return sources

    def run(self):
        if self.command:
            cmd = self.command.split()
        elif not self.executable:
            raise ValueError("executable is not set")
        else:
            cmd = [self.executable, *self.args]
        self.run_cmd(cmd)

    def run_cmd(self, cmd, check=None, stdout=None, stderr=None):
        log.info("Running command: '%s'", " ".join(cmd))
        if stdout is None:
            # A generator's subprocess inherits our stdout, which would corrupt a `--json`
            # document. `tool_output_redirect()` is None unless output has been redirected, so
            # normal runs keep inheriting as before.
            stdout = tool_output_redirect()
        p = subprocess.run(
            cmd,
            cwd=self.cwd,
            check=check if check is not None else self.check,
            stdout=stdout,
            stderr=stderr,
            env=self.env,
        )
        if self.check and p.returncode:
            raise NonZeroExitCode(cmd, p.returncode)
        return p

    @property
    def name(self) -> str:
        return str(self.__class__.__qualname__ or "generator")


class ChiselGenerator(Generator):
    main: Optional[str] = None
    project: Optional[str] = None
    build_system: str = "mill"
    check: bool = True

    def run(self):
        if self.build_system == "mill":
            return self.run_mill()
        elif self.build_system == "bloop":
            return self.run_bloop()
        else:
            raise Exception(f"Unsupported build system: {self.build_system}")

    def run_mill(self):
        # if file ./mill or ./millw exists, use it, otherwise use mill from PATH
        mill_exec = "./mill"
        if not Path(mill_exec).exists():
            mill_exec = "mill"
        if not self.project:
            ValueError("`project` must be specified for Chisel generator")
        cmd = [mill_exec]
        if self.main:
            cmd += [f"{self.project}.runMain", self.main]
        else:
            cmd.append(f"{self.project}.run")
        if self.args:
            if isinstance(self.args, str):
                self.args = self.args.split()
            cmd += self.args
        return self.run_cmd(cmd)

    def run_bloop(self):
        if self.project is None:
            p = self.run_cmd(["bloop", "projects"], stdout=subprocess.PIPE)
            projects_str = p.stdout.decode()
            projects = re.split(r"\s+", projects_str)
            if projects:
                log.info(f"Found projects: {', '.join(projects)}")
                self.project = projects[0]
            else:
                log.error("No projects found!")
                raise ValueError("No projects found!")
        if not self.project:
            ValueError("`project` must be specified for Chisel generator")
        cmd = ["bloop", "run", self.project]
        if self.main:
            cmd += ["--main", self.main]
        if self.args:
            if isinstance(self.args, str):
                self.args = self.args.split()
            cmd.append("--")
            cmd += self.args
        return self.run_cmd(cmd)


class RtlSettings(DVSettings):
    """design.rtl"""

    top: Optional[str] = Field(
        None,
        description="Toplevel RTL module/entity",
    )
    generator: Union[str, List[str], Generator, None] = None
    attributes: Dict[str, Dict[str, Any]] = Field(
        dict(),
        description="""
        attributes may include HDL attributes for modules, ports, etc, but their actual meaning and behavior is decided by the specific target flow
        Attributes should be specified as a mapping of attr_name->(path->attr_value), i.e.:
        - key: is the _name_ of the attribute
        - value is a mapping of path->attr_value, i.e.:
            - the key is the path or scope on which the attribute applies
            - value is the actual value of the attribute
        """,
    )
    # preferred way to specify a design's clock ports:
    clocks: List[Clock] = []
    # short-hand alternatives for a single clock designs:
    clock: Optional[Clock] = None  # DEPRECATED # TODO remove
    clock_port: Optional[str] = None  # TODO remove?

    @model_validator(mode="before")
    @classmethod
    def rtl_settings_validate(cls, values):  # pylint: disable=no-self-argument
        """copy equivalent clock fields (backward compatibility)"""
        clock = values.get("clock") or values.get("clock_port")
        clocks = values.get("clocks")

        def conv_clock(clock):
            if isinstance(clock, dict):
                clock = Clock(**clock)
            elif isinstance(clock, str):
                clock = Clock(port=clock)
            return clock

        if clocks is None:
            if clock:
                clocks = [clock]
            else:
                clocks = []
        elif isinstance(clocks, (str, dict, Clock)):
            clocks = [clocks]
        if not isinstance(clocks, list):
            raise ValueError(f"Expecting 'clocks' to be a list but found {clocks}")
        clocks = [conv_clock(clk) for clk in clocks if clk]
        values["clocks"] = clocks
        if clocks:
            values["clock"] = clocks[0]
        return values


class CocotbTestbench(XedaBaseModel):
    module: Optional[str] = None
    toplevel: Optional[str] = None
    testcase: List[str] = Field(
        default=[],
        description="List of test-cases for this design. Will be overridden by flow settings: cocotb.testcase",
    )


class TbSettings(DVSettings):
    """design.tb"""

    sources: List[DesignSource] = []
    top: Tuple012 = Field(
        tuple(),
        description="Toplevel testbench module(s), specified as a tuple of strings. In addition to the primary toplevel, a secondary toplevel module can also be specified.",
    )
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench"
    )
    cocotb: Optional[CocotbTestbench] = Field(
        None, description="testbench is based on cocotb framework"
    )

    @field_validator("top", mode="before")
    @classmethod
    def _tb_top_validate(cls, value) -> Tuple012:
        if value:
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (tuple, list, Sequence)):
                if len(value) > 2:
                    raise ValueError("At most 2 simulation top modules are supported.")
                return tuple(value)
        return tuple()

    @field_validator("cocotb", mode="before")
    @classmethod
    def _auto_set_cocotb(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        if value is False:
            return None

        def has_cocotb(tb):
            sources = tb.get("sources", [])
            for src in sources:
                if isinstance(src, DesignSource) and src.type == SourceType.Cocotb:
                    return True
                if isinstance(src, dict) and src.get("type") in ["cocotb", SourceType.Cocotb]:
                    return True
                if isinstance(src, str) and src.startswith("cocotb:") and src.endswith(".py"):
                    return True
            return False

        if value is True or (value is None and has_cocotb(values)):
            return CocotbTestbench()
        return value


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(
        None,
        validate_default=False,  # v1: no `always=True` -- do not run on the default
        description="Standard version",
        alias="version",
    )

    @field_validator("standard", mode="before")
    @classmethod
    def two_digit_standard(cls, value):
        if not value:
            return None
        if isinstance(value, int):
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError("standard should be of type string")
        return value

    @classmethod
    def from_version(cls, version: str | int):
        return cls(version=cls.two_digit_standard(version))  # type: ignore


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(XedaBaseModel):
    vhdl: VhdlSettings = VhdlSettings()  # type: ignore
    verilog: LanguageSettings = LanguageSettings()  # type: ignore

    @field_validator("verilog", "vhdl", mode="before")
    @classmethod
    def _language_settings(cls, value, info):
        if isinstance(value, (str, int)):
            if info.field_name == "vhdl":
                return VhdlSettings.from_version(value)
            elif info.field_name == "verilog":
                return LanguageSettings.from_version(value)
        return value


class RtlDep(XedaBaseModel):
    pos: int = 0


class TbDep(XedaBaseModel):
    pos: int = 0


class DesignReference(XedaBaseModel):
    uri: str
    rtl: RtlDep = RtlDep()
    tb: TbDep = TbDep()
    local_cache: Path = Path.cwd() / ".xeda_dependencies"

    @staticmethod
    def from_data(data) -> DesignReference:
        if isinstance(data, DesignReference):
            return data
        if isinstance(data, str):
            data = dict(uri=data)
        elif isinstance(data, Mapping):
            # The URI form is normalized below by removing the ``git+`` discriminator. Keep
            # that normalization local: dependency dictionaries commonly come straight from
            # the caller's parsed design document, and must not be rewritten as a side effect
            # of constructing a model.
            data = dict(data)
        else:
            raise ValueError(
                "a design dependency must be a URI string, mapping, or DesignReference, "
                f"got {type(data).__name__}"
            )
        if "uri" in data:
            uri_str = data["uri"]
            GIT_PREFIX = "git+"
            if isinstance(uri_str, str) and uri_str.startswith(GIT_PREFIX):
                uri_str = uri_str[len(GIT_PREFIX) :]
                data["uri"] = uri_str
                return GitReference(**data)  # type: ignore
        if "repo_url" in data:
            return GitReference(**data)  # type: ignore
        return DesignReference(**data)  # type: ignore

    def fetch_design(self) -> Design:
        toml_path = Path(self.uri)
        if not toml_path.exists():
            raise ValueError(f"file {toml_path} does not exist!")
        return Design.from_toml(toml_path)


class GitReference(DesignReference):
    """
    uri: [https,git,...]://<hostname>[:port]/path/to/repo.git[?[branch=mybranch],[commit=mycommit]]#path/to/design_file.toml
    example:
        https://github.com/GMUCERG/TinyJAMBU-SCA.git?branch=dev#./TinyJAMBU-DOM1-v1.toml
    """

    repo_url: str
    design_file: str
    commit: Optional[str] = None
    branch: Optional[str] = None
    clone_dir: Optional[Path] = None

    @field_validator("clone_dir", mode="before")
    @classmethod
    def validate_clone_dir(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        repo_url = values.get("repo_url")
        if not value and repo_url:
            uri = urlparse(repo_url)
            uri_path = uri.path.lstrip("/.")
            if not uri.netloc:
                raise ValueError(f"invalid URL: {uri}")
            commit = values.get("commit")
            branch = values.get("branch")
            if commit:
                uri_path += "_commit=" + commit
            elif branch:
                uri_path += "_" + branch
            local_cache = values.get("local_cache")
            if local_cache:
                return Path(local_cache) / uri.netloc / uri_path
        return value

    @model_validator(mode="before")
    @classmethod
    def validate_repo(cls, values):
        repo_url = None
        design_file_path = None
        branch = None
        commit = None
        if "uri" in values:
            uri_str = values["uri"]
            if not isinstance(uri_str, str):
                raise ValueError("a git dependency URI must be a string")
            # <scheme>://<netloc>/<path>;<params>?<query>#<fragment>
            uri = urlparse(uri_str)
            if not uri.scheme or not uri.netloc:
                raise ValueError(f"invalid git URL: {uri}")
            # git design file path should be relative to root
            design_file_path = uri.fragment.lstrip("/.")  # Removes /, ../, etc.
            if not design_file_path:
                raise ValueError(
                    inspect.cleandoc(
                        """path to design_file must be specified using URL fragment (#...), e.g.,
                    https://github.com/SOME_USERNAME/SOME_REPOSITORY.git#PATH_TO_DESIGN_FILE when design file is
                    'sub_dir1/design_file2.toml' relative to the the repository's root."""
                    )
                )
            if uri.query:
                query = parse_qs(uri.query)
                br = query.get("branch")
                if br:
                    branch = br[-1]
                cmt = query.get("commit")
                if cmt:
                    commit = cmt[-1]  # last arg
            repo_url = uri._replace(fragment="", query="").geturl()

        # Explicit keys first, then the caller's own values win -- `dict(repo_url=..., **values)`
        # raised `TypeError: got multiple values for keyword argument 'repo_url'` for exactly the
        # mapping form `DesignReference.from_data()` selects when it sees a `repo_url` key.
        derived = {
            "repo_url": repo_url,
            "design_file": design_file_path,
            "branch": branch,
            "commit": commit,
        }
        merged = {k: v for k, v in derived.items() if v is not None}
        result = {**derived, **merged, **values}
        if not result.get("uri"):
            # The mapping form (`{repo_url = ..., design_file = ...}`) that
            # `DesignReference.from_data()` routes here carries no `uri`, but the base class
            # requires one. Reconstruct it so both spellings describe the same reference.
            base = result.get("repo_url")
            if not base:
                raise ValueError("a git dependency needs either 'uri' or 'repo_url'")
            uri_query = "&".join(
                f"{k}={result[k]}" for k in ("branch", "commit") if result.get(k) is not None
            )
            uri_fragment = result.get("design_file") or ""
            result["uri"] = "{}{}{}".format(
                base,
                f"?{uri_query}" if uri_query else "",
                f"#{uri_fragment}" if uri_fragment else "",
            )
        return result

    def fetch_design(self) -> Design:
        import git
        from git.repo import Repo

        if not self.clone_dir:
            raise ValueError(f"'clone_dir' not set for GitReference: {self}")
        repo = None
        if self.clone_dir.exists():
            try:
                repo = Repo(self.clone_dir)
                if not repo.git_dir:
                    raise ValueError(f"repo={repo} is missing 'git_dir'")
                log.info("Updating existing git repository at %s", self.clone_dir)
                repo.remotes.origin.fetch()
                if not self.commit:
                    repo.git.pull()
            except git.InvalidGitRepositoryError:
                log.error("Path %s is not a valid git repository.", self.clone_dir)
        if repo is None:
            log.info(
                "Cloning git repository url:%s branch:%s commit:%s",
                self.repo_url,
                self.branch,
                self.commit,
            )
            repo = Repo.clone_from(
                self.repo_url,
                self.clone_dir,
                depth=1,
                branch=self.branch,
            )
        if repo is None:
            ValueError("repo is None!")
        if self.commit:
            log.info("Checking out commit: %s", self.commit)
            repo.git.checkout(self.commit)
        elif self.branch:
            log.info("Checking out branch: %s", self.branch)
            repo.git.checkout(self.branch)

        toml_path = self.clone_dir / self.design_file
        return Design.from_toml(toml_path)


DesignType = TypeVar("DesignType", bound="Design")


class Design(XedaBaseModel):
    name: str = Field(
        description="Unique name for the design, which should consist of letters, numbers, underscore(_), and dash(-). Name regex: [a-zA-Z][a-zA-Z0-9_\\-]*."
    )

    design_root: Optional[Path] = Field(None, json_schema_extra={"hidden_from_schema": True})
    description: Optional[str] = Field(None, description="A brief description of the design.")
    authors: List[str] = Field(
        [],
        alias="author",
        description="""List of authors/developers in "Name <email>" format ('mailbox' format, RFC 5322), e.g. ["Jane Doe <jane@example.com>", "John Doe <john@example.com>"]""",
    )
    # `SerializeAsAny`: a dependency is usually a `GitReference`, and v2 serializes a nested
    # model by its *annotated* type unless told otherwise -- which would silently drop
    # `repo_url`, `design_file`, `branch`/`commit` and the clone settings that
    # `send_design()` relies on.
    dependencies: List[SerializeAsAny[DesignReference]] = []
    rtl: RtlSettings
    tb: TbSettings = TbSettings()  # type: ignore
    language: Language = Field(
        Language(),
        alias="hdl",
        description="HDL language settings",
    )
    flow: Dict[str, Dict[str, Any]] = Field(
        dict(),
        alias="flows",
        description="Design-specific flow settings. The keys are the name of the flow and values are design-specific overrides for that flow.",
    )
    license: Union[str, List[str], None] = None
    version: Optional[str] = None
    url: Optional[str] = None

    @field_validator("flow", mode="before")
    @classmethod
    def _flow_settings(cls, value):
        if value:
            value = settings_to_dict(value)
            value = {k: v for k, v in value.items() if v is not None}
        else:
            value = {}
        return value

    @field_validator("dependencies", mode="before")
    @classmethod
    def _dependencies_from_str(cls, value):
        if value and isinstance(value, list):
            value = [DesignReference.from_data(v) for v in value]
        return value

    @field_validator("authors", mode="before")
    @classmethod
    def _authors_from_str(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    @classmethod
    def process_compatibility(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if "rtl" not in data:
            clocks = data.pop("clocks", None)
            if clocks is None:
                clock = data.pop("clock", None)
                clocks = [clock] if clock else []
            data["rtl"] = {
                "sources": data.pop("sources", []),
                "generator": data.pop("generator", None),
                # `{}`, not `[]`: both are `Dict` fields. pydantic v1 silently coerced an
                # empty list to an empty dict; v2 rejects it.
                "parameters": data.pop("parameters", {}),
                "defines": data.pop("defines", {}),
                "top": data.pop("top", None),
                "clocks": clocks,
            }
        tb = data.get("tb", {})
        tests = data.pop("tests", [])
        if tests and not isinstance(tests, list):
            tests = [tests]
        test = data.pop("test", None)
        if test:
            tests.append(test)
        if tests and not tb:
            # TODO add support for multiple tests per design
            test = tests[0]
            if not isinstance(test, dict):
                raise ValueError(f"test: {test} is not a dictionary")
            data["tb"] = test
        return data

    @classmethod
    def process_generation(cls, data: Dict[str, Any]):
        design_root = data.get("design_root")
        if not design_root:
            design_root = Path.cwd()
        else:
            design_root = Path(design_root)
        rtl = data.get("rtl", {})
        assert isinstance(rtl, dict), f"rtl must be a dictionary, but found {type(rtl)}"
        generator = rtl.pop("generator", None)
        if generator:
            with WorkingDirectory(design_root):
                env = os.environ.copy()
                if "DESIGN_ROOT" not in env:
                    env["DESIGN_ROOT"] = str(design_root)
                if isinstance(generator, str):
                    log.info("Running generator command: %s", generator)
                    exit_code = subprocess.call(
                        generator,
                        shell=True,
                        cwd=design_root,
                        env=env,
                        stdout=tool_output_redirect(),
                    )
                    if exit_code != 0:
                        log.error("Generator '%s' failed with exit code %d", generator, exit_code)
                        raise NonZeroExitCode(generator, exit_code)
                elif isinstance(generator, (dict, Generator)):
                    if isinstance(generator, (dict)):
                        clazz = generator.get("class") or generator.get("use")
                        if clazz:
                            if not isinstance(clazz, str):
                                raise ValueError(f"class={clazz} must be a string")
                            if clazz.lower() == "chisel":
                                generator = ChiselGenerator(**generator)
                            else:
                                raise Exception(f"unknown generator class: {clazz}")
                        else:
                            generator = Generator(**generator)
                    if generator.cwd is None:
                        generator.cwd = str(design_root)
                    if generator.env is None:
                        generator.env = os.environ.copy()
                    if "DESIGN_ROOT" not in generator.env:
                        generator.env["DESIGN_ROOT"] = str(design_root)
                    skip_run = False
                    rtl_sources = rtl.get("sources", [])
                    if generator.run_only_if_sources_modified and generator.sources and rtl_sources:
                        log.debug("Generator sources: %s", generator.sources)
                        # check if rtl.sources exist and if they are newer than the generator sources
                        if all(Path(src).exists() for src in rtl_sources):
                            generator_sources_last_modified = max(
                                Path(gen_src).stat().st_mtime for gen_src in generator.sources
                            )
                            if all(
                                Path(src).stat().st_mtime >= generator_sources_last_modified
                                for src in rtl_sources
                            ):
                                skip_run = True
                                log.info(
                                    "Skipping generator '%s' run, as all rtl.sources are newer than the generator sources",
                                    generator.name,
                                )
                            else:
                                log.info(
                                    "Running generator '%s' as some rtl.sources are older than the generator sources",
                                    generator.name,
                                )
                        else:
                            log.info(
                                "Running generator '%s' as not all rtl.sources exist",
                                generator.name,
                            )
                    if not skip_run:
                        log.info("Running generator: %s", generator.name)
                        generator.run()
                else:
                    args = generator
                    # gen_script = Path(args[0])
                    # extension = gen_script.suffix
                    # if extension == ".py":
                    #     if not gen_script.exists():
                    #         log.critical("Generator script not found: %s", gen_script)
                    #         raise FileNotFoundError(gen_script)
                    #     args.insert(0, sys.executable)
                    subprocess.run(
                        args,
                        check=True,
                        cwd=design_root,
                        stdout=tool_output_redirect(),
                    )

    @classmethod
    def process_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        data = cls.process_compatibility(data)
        log.debug("Design data: %s", data)
        cls.process_generation(data)
        return data

    def __init__(
        self,
        design_root: Union[str, os.PathLike, None] = None,
        **data: Any,
    ) -> None:
        if not design_root:
            design_root = data.pop("design_root", Path.cwd())
        if not design_root:
            raise ValueError("design_root is not set")
        if not isinstance(design_root, Path):
            design_root = Path(design_root)
        design_root = design_root.resolve()
        if not data.get("design_root"):
            data["design_root"] = design_root
        data = Design.process_dict(data)
        with WorkingDirectory(design_root):
            try:
                super().__init__(**data)
            except ValidationError as e:
                raise DesignValidationError(
                    validation_errors(e.errors()), data=data, design_root=design_root  # type: ignore
                ) from e

            for dep in self.dependencies:
                dep_design = dep.fetch_design()
                log.info("adding dependency sources from %s", dep_design.name)
                pos = dep.rtl.pos
                if pos == -1:  # -1 means append 'after' the last element
                    self.rtl.sources.extend(dep_design.rtl.sources)
                    if not self.rtl.top and dep_design.rtl.top:
                        self.rtl.top = dep_design.rtl.top
                    if not self.rtl.parameters and dep_design.rtl.parameters:
                        self.rtl.parameters = dep_design.rtl.parameters
                    if not self.rtl.clocks and dep_design.rtl.clocks:
                        self.rtl.clocks = dep_design.rtl.clocks
                else:
                    if pos < 0:
                        pos += 1  # afterwards: pos=-2 means the position 'before' the last element
                    self.rtl.sources[pos:pos] = dep_design.rtl.sources
                if not self.tb.sources and dep_design.tb.sources:
                    self.tb.sources = dep_design.tb.sources
                if not self.tb.top and dep_design.tb.top:
                    self.tb.top = dep_design.tb.top

    def sources_of_type(
        self, *source_types: Union[str, SourceType], rtl=True, tb=False
    ) -> List[DesignSource]:
        source_types_str = [str(st).lower() for st in source_types]
        sources = []
        if rtl:
            sources.extend(self.rtl.sources)
        if tb and self.tb:
            sources.extend([src for src in self.tb.sources if src not in self.rtl.sources])
        if len(source_types) == 1 and isinstance(source_types[0], str) and source_types[0] == "*":
            return sources
        return [src for src in sources if str(src.type).lower() in source_types_str]

    def sim_sources_of_type(self, *source_types: Union[str, SourceType]) -> List[DesignSource]:
        if not self.tb:
            return []
        return self.sources_of_type(*source_types, rtl=True, tb=True)

    @property
    def sim_sources(self) -> List[DesignSource]:
        return self.sim_sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl
        )

    @property
    def sim_tops(self) -> Tuple012:
        if self.tb:
            if self.tb.cocotb and self.rtl.top:
                return (self.rtl.top,)
            if self.tb.top is not None:
                return self.tb.top
        return tuple()

    @property
    def root_path(self) -> Path:
        if not self.design_root:
            raise ValueError("design_root is not set")
        return self.design_root

    @classmethod
    def from_toml(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[str, os.PathLike, None] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
    ) -> DesignType:
        return cls.from_file(
            design_file,
            design_root=design_root,
            overrides=overrides,
            allow_extra=allow_extra,
            remove_extra=remove_extra,
        )

    @classmethod
    def from_file(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[str, os.PathLike, None] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
    ) -> DesignType:
        """Load and validate a design description from TOML file"""
        if overrides is None:
            overrides = {}
        if remove_extra is None:
            remove_extra = []
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        error_msg_parts = [f'File "{design_file.absolute()}"']
        if design_file.suffix == ".toml":
            design_dict = toml_load(design_file)
        elif design_file.suffix == ".json":
            with open(design_file) as f:
                try:
                    design_dict = json.load(f)
                except json.JSONDecodeError as e:
                    error_msg_parts += [
                        f"line {e.lineno + 1}",
                        f"column {e.colno + 1}",
                        e.msg,
                    ]
                    raise DesignFileParseError(", ".join(error_msg_parts)) from None
        elif design_file.suffix in {".yaml", ".yml"}:
            with open(design_file) as f:
                try:
                    design_dict = yaml.safe_load(f)
                except yaml.error.MarkedYAMLError as e:
                    if e.context_mark:
                        # context_mark's line and column start from 0, but most IDEs use 1 indexing for source locators
                        error_msg_parts += [
                            f"line {e.context_mark.line + 1}",
                            f"column {e.context_mark.column + 1}",
                        ]
                        if e.problem:
                            error_msg_parts.append(e.problem)
                        if e.note:
                            error_msg_parts.append("Note: " + e.note)

                    raise DesignFileParseError(", ".join(error_msg_parts)) from None
                except yaml.YAMLError as e:
                    raise DesignFileParseError(f"{e.args}") from None
        else:
            raise ValueError(f"File extension `{design_file.suffix}` is not supported.")
        design_dict = expand_hierarchy(design_dict)
        design_dict = hierarchical_merge(design_dict, overrides)
        if "name" not in design_dict:
            design_name = design_file.stem
            design_name = removesuffix(design_name, ".xeda")
            log.debug(
                "'design.name' not specified! Inferring design name: `%s` from design file name.",
                design_name,
            )
            design_dict["name"] = design_name
        if allow_extra:
            cls = model_with_allow_extra(cls)
        else:
            for k in remove_extra:
                design_dict.pop(k, None)
        # Default value for design_root is the folder containing the design description file.
        dr = design_dict.pop("design_root", None)
        if design_root is None:
            design_root = dr
        if design_root is None:
            design_root = design_file.parent
        try:
            return cls(design_root=design_root, **design_dict)
        except DesignValidationError as e:
            raise DesignValidationError(  # add design_file to the emitted exception
                e.errors,
                data=e.data,
                design_root=e.design_root,
                design_name=e.design_name,
                file=str(design_file.absolute()),
            ) from e
        except Exception as e:
            log.error("Error processing design file: %s", design_file.absolute())
            raise e

    def relative_path(self, src: DesignSource):
        if src.file and self.root_path:
            file = src.file.absolute()
            root = self.root_path.absolute()
            try:
                return file.relative_to(root)
            except ValueError:
                pass
        return src.get_specified_path()

    @property
    def rtl_fingerprint(self) -> Dict[str, Any]:
        return {
            "sources": OrderedDict(
                sorted((str(self.relative_path(src)), src.content_hash) for src in self.rtl.sources)
            ),
            "parameters": OrderedDict((p, str(v)) for p, v in sorted(self.rtl.parameters.items())),
            "top": self.rtl.top,
        }

    @property
    def tb_fingerprint(self) -> Dict[str, Any]:
        return {
            "sources": OrderedDict(
                sorted((str(self.relative_path(src)), src.content_hash) for src in self.tb.sources)
            ),
            "parameters": OrderedDict((p, str(v)) for p, v in sorted(self.tb.parameters.items())),
            "top": self.tb.top,
        }

    @property
    def rtl_hash(self) -> str:
        # assumptions:
        #  - source file names/paths do not matter
        #  - order of sources does not matter
        #       -> alphabetically sort all file _hashes_
        #  - order of parameters does not matter
        # fingerprint = str(self.rtl_fingerprint)
        # log.debug("RTL fingerprint: %s", fingerprint)
        # return hashlib.sha3_256(bytes(fingerprint, "utf-8")).hexdigest()[:32]  # 128 bits

        src_hashes = "|".join(sorted(src.content_hash for src in self.rtl.sources))
        params = "|".join(f"{p}={v}" for p, v in sorted(self.rtl.parameters.items()))
        defines = "|".join(f"{p}={v}" for p, v in sorted(self.rtl.defines.items()))
        r = bytes(
            f"sources={src_hashes},params={params},defines={defines},top={self.rtl.top}", "utf-8"
        )
        log.debug("RTL fingerprint: %s", r)
        return hashlib.sha3_256(r).hexdigest()[:32]  # 128 bits

    @property
    def tb_hash(self) -> str:
        src_hashes = "|".join(sorted(src.content_hash for src in self.tb.sources))
        params = "|".join(f"{p}={v}" for p, v in sorted(self.tb.parameters.items()))
        defines = "|".join(f"{p}={v}" for p, v in sorted(self.tb.defines.items()))
        r = bytes(
            f"sources={src_hashes},params={params},defines={defines},top={self.tb.top}", "utf-8"
        )
        log.debug("TB fingerprint: %s", r)
        return hashlib.sha3_256(r).hexdigest()[:32]  # 128 bits

    #: Derived fields that are recomputed on load and must never be serialized.
    _NOT_SERIALIZED = {"rtl_hash", "tb_hash", "rtl_fingerprint", "tb_fingerprint"}

    # pylint: disable=arguments-differ
    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        kwargs.setdefault("serialize_as_any", True)
        kwargs.setdefault("exclude_unset", True)
        kwargs.setdefault("exclude_defaults", True)
        kwargs.setdefault("exclude", self._NOT_SERIALIZED)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:  # type: ignore[override]
        kwargs.setdefault("serialize_as_any", True)
        kwargs.setdefault("exclude_unset", True)
        kwargs.setdefault("exclude_defaults", True)
        kwargs.setdefault("exclude", self._NOT_SERIALIZED)
        return super().model_dump_json(**kwargs)
