from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import subprocess
from enum import Enum, auto
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, TypeVar, Union
from urllib.parse import parse_qs, urlparse


from .dataclass import (
    Field,
    ValidationError,
    XedaBaseModel,
    model_with_allow_extra,
    root_validator,
    validation_errors,
    validator,
)
from .utils import (
    WorkingDirectory,
    expand_hierarchy,
    hierarchical_merge,
    removeprefix,
    settings_to_dict,
    toml_load,
)

log = logging.getLogger(__name__)

__all__ = [
    "Design",
    "DesignValidationError",
    "DesignSource",
    "FileResource",
    "SourceType",
    "VhdlSettings",
    "LanguageSettings",
    "Clock",
]


class DesignValidationError(Exception):
    def __init__(
        self,
        errors: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]],
        data: Dict[str, Any],
        *args: object,
        design_root: Union[None, str, os.PathLike] = None,
        design_name: Optional[str] = None,
        file: Optional[str] = None,
    ) -> None:
        super().__init__(*args)
        self.errors = errors  # location, msg, type/context
        self.data = data
        self.design_root = design_root
        self.design_name = design_name
        self.file = file

    def __str__(self) -> str:
        name = self.design_name or self.data.get("name")
        return "{}: {} error{} validating design{}:\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            f" {name}" if name else "",
            "\n".join(
                "{}{} ({})\n".format(f"{loc}:\n   " if loc else "", msg, ctx)
                for loc, msg, ctx, typ in self.errors
            ),
        )


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
            self.file = path.resolve(strict=True) if resolve else path.absolute()
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
            return hashlib.sha3_256(f.read()).hexdigest()

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


class SourceType(str, Enum):
    Bluespec = auto()
    Chisel = auto()
    Cpp = auto()
    Cocotb = auto()
    Sdc = auto()
    SystemVerilog = auto()
    Tcl = auto()
    Verilog = auto()
    Vhdl = auto()
    Xdc = auto()

    def __str__(self) -> str:
        return str(self.name)

    @classmethod
    def from_str(cls, type: str) -> Optional[SourceType]:
        try:
            return cls[type]
        except KeyError:
            pass
        try:
            return cls[type.capitalize()]
        except KeyError:
            for k, v in cls.__members__.items():
                if k.lower() == type.lower():
                    return v
            return None


class DesignSource(FileResource):
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        type: Union[None, str, SourceType] = None,
        standard: Optional[str] = None,
        variant: Optional[str] = None,
        _root_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(path, dict):
            type = type or path.pop("type", None)
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
                (SourceType.SystemVerilog, None): ["sv"],
                (SourceType.Bluespec, "bsv"): ["bsv"],
                (SourceType.Bluespec, "bh"): ["bs", "bh"],
                (SourceType.Xdc, None): ["xdc"],
                (SourceType.Sdc, None): ["sdc"],
                (SourceType.Tcl, None): ["tcl"],
            }
            for (typ, vari), suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return (typ, vari)
            return None, None

        self.variant = variant
        self.type = None
        if isinstance(type, SourceType):
            self.type = type
        elif isinstance(type, str):
            self.type = SourceType.from_str(type)
        if not self.type:
            self.type, self.variant = type_from_suffix(self.file)
        if standard and len(standard) == 4:
            if standard.startswith("20") or standard.startswith("19"):
                standard = standard[2:]
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
        has_alias=True,
    )  # top defines/generics
    parameters: Dict[str, DefineType] = Field(
        default={},
        description="Top-level generics/defines specified as a mapping",
        alias="generics",
        has_alias=True,
    )
    defines: Dict[str, DefineType] = Field(default={})

    @root_validator(pre=True, allow_reuse=True)
    def the_root_validator(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        value = values.get("parameters")
        if not value:
            value = values.get("generics")
        if value:
            for k, v in value.items():
                if isinstance(v, dict) and ("file" in v or "path" in v):
                    value[k] = str(FileResource(v))
            values["generics"] = value
            values["parameters"] = value
        return values

    @validator("sources", pre=True, always=True)
    def sources_to_files(cls, value):
        if isinstance(value, (str, Path, DesignSource)):
            value = [value]
        sources = []
        for src in value:
            if not isinstance(src, DesignSource):
                try:
                    src = DesignSource(src)
                except FileNotFoundError as e:
                    raise ValueError(
                        f"Source file: {src} was not found: {e.strerror} {e.filename}"
                    ) from e
            sources.append(src)
        return sources


class Clock(XedaBaseModel):
    port: str


class Generator(XedaBaseModel):
    cwd: Union[None, str] = None
    executable: Optional[str] = None
    args: Union[str, List[str]] = []
    shell: bool = False
    check: bool = True
    env: Optional[Dict[str, str]] = None
    # sweepable parameters used in command
    parameters: dict = {}
    # for xeda to know dependencies, clean previous artifacts, check after generation:
    generated_sources: List[str] = []


class RtlSettings(DVSettings):
    """design.rtl"""

    top: Optional[str] = Field(
        description="Toplevel RTL module/entity",
    )
    generator: Union[None, str, List[str], Generator] = None
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
    clocks: Dict[str, Clock] = {}
    # short-hand alternatives for a single clock designs:
    clock: Optional[Clock] = None  # DEPRECATED # TODO remove
    clock_port: Optional[str] = None  # TODO remove?

    @root_validator(pre=False)
    def rtl_settings_validate(cls, values):  # pylint: disable=no-self-argument
        """copy equivalent clock fields (backward compatibility)"""
        clock = values.get("clock")
        clock_port = values.get("clock_port")
        clocks = values.get("clocks")

        if not clock:
            if clock_port:
                clock = Clock(port=clock_port)
            elif len(clocks) == 1:
                clock = list(clocks.values())[0]
        if clock:
            if not clock_port:
                clock_port = clock.port
            if not clocks:
                clocks = {"main_clock": clock}
            values["clock"] = clock
        if clocks:
            values["clocks"] = clocks
        if clock_port:
            values["clock_port"] = clock_port
        return values


class TbSettings(DVSettings):
    """design.tb"""

    top: Tuple012 = Field(
        tuple(),
        description="Toplevel testbench module(s), specified as a tuple of strings. In addition to the primary toplevel, a secondary toplevel module can also be specified.",
    )
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench"
    )
    cocotb: bool = Field(False, description="testbench is based on cocotb framework")

    @validator("top", pre=True, always=True)
    def _tb_top_validate(cls, value) -> Tuple012:
        if value:
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (tuple, list, Sequence)):
                if len(value) > 2:
                    raise ValueError("At most 2 simulation top modules are supported.")
                return tuple(value)
        return tuple()

    @root_validator(pre=True)
    def _tb_root_validate(cls, values):
        sources_value = values.get("sources", [])
        if isinstance(sources_value, (str, Path)):
            sources_value = [sources_value]
        sources = []
        for src in sources_value:
            if isinstance(src, str) and src.startswith("cocotb:") and src.endswith(".py"):
                src = {"file": removeprefix(src, "cocotb:"), "type": SourceType.Cocotb}
                values["cocotb"] = True
            sources.append(src)
        values["sources"] = sources
        return values


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(
        None,
        description="Standard version",
        alias="version",
        has_alias=True,
    )
    version: Optional[str] = Field(
        None,
        description="Standard version",
        alias="standard",
        has_alias=True,
    )

    @validator("standard", pre=True)
    def two_digit_standard(cls, value, values):
        if not value:
            value = values.get("version")
        if not value:
            return None
        if isinstance(value, int):
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError("standard should be of type string")
        if value and len(value) == 4:
            if value.startswith("20") or value.startswith("19"):
                value = value[2:]
        return value

    @root_validator(pre=True)
    def language_root_validator(cls, values):
        if "standard" in values:
            values["version"] = values["standard"]
        return values


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(XedaBaseModel):
    vhdl: VhdlSettings = VhdlSettings()  # type: ignore
    verilog: LanguageSettings = LanguageSettings()  # type: ignore


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
        if isinstance(data, str):
            data = dict(uri=data)
        if "uri" in data:
            uri_str = data["uri"]
            GIT_PREFIX = "git+"
            if uri_str.startswith(GIT_PREFIX):
                uri_str = uri_str[len(GIT_PREFIX) :]
                data["uri"] = uri_str
                return GitReference(**data)
        if "repo_url" in data:
            return GitReference(**data)
        return DesignReference(**data)

    def fetch_design(self) -> Design:
        toml_path = Path(self.uri)
        assert toml_path.exists(), f"file {toml_path} does not exist!"
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

    @validator("clone_dir", pre=True, always=True)
    def validate_clone_dir(cls, value, values):
        repo_url = values.get("repo_url")
        if not value and repo_url:
            uri = urlparse(repo_url)
            uri_path = uri.path.lstrip("/.")
            assert uri.netloc, "invalid URL"
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

    @root_validator(pre=True)
    def validate_repo(cls, values):
        repo_url = None
        design_file_path = None
        branch = None
        commit = None
        if "uri" in values:
            uri_str = values["uri"]
            # <scheme>://<netloc>/<path>;<params>?<query>#<fragment>
            uri = urlparse(uri_str)
            assert uri.scheme and uri.netloc, "invalid git URL"
            # git design file path should be relative to root
            design_file_path = uri.fragment.lstrip("/.")  # Removes /, ../, etc.
            if not design_file_path:
                raise ValueError(
                    inspect.cleandoc(
                        """path to design_file must be specified using URL fragment (#...), e.g.,
                    https://github.com/user/repo.git#sub_dir1/design_file2.toml when design file is
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

        return dict(
            repo_url=repo_url,
            design_file=design_file_path,
            branch=branch,
            commit=commit,
            **values,
        )

    def fetch_design(self) -> Design:
        import git
        from git.repo import Repo

        assert self.clone_dir, "clone_dir not set"
        repo = None
        if self.clone_dir.exists():
            try:
                repo = Repo(self.clone_dir)
                assert repo.git_dir
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
        assert repo is not None
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
    design_root: Optional[Path] = None
    description: Optional[str] = Field(None, description="A brief description of the design.")
    authors: List[str] = Field(
        [],
        alias="author",
        description="""List of authors/developers in "Name <email>" format ('mailbox' format, RFC 5322), e.g. ["Jane Doe <jane@example.com>", "John Doe <john@example.com>"]""",
    )
    dependencies: List[DesignReference] = []
    rtl: RtlSettings
    tb: TbSettings = TbSettings()  # type: ignore
    language: Language = Language()
    flow: Dict[str, Dict[str, Any]] = Field(
        dict(),
        alias="flows",
        description="Design-specific flow settings. The keys are the name of the flow and values are design-specific overrides for that flow.",
    )
    license: Union[None, str, List[str]] = None
    version: Optional[str] = None
    url: Optional[str] = None

    @validator("flow", pre=True, always=True)
    def _flow_settings(cls, value):
        if value:
            value = settings_to_dict(value)
        return value

    @validator("dependencies", pre=True, always=True)
    def _dependencies_from_str(cls, value):
        if value and isinstance(value, list):
            value = [DesignReference.from_data(v) for v in value]
        return value

    @validator("authors", pre=True, always=True)
    def _authors_from_str(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    def __init__(
        self,
        design_root: Union[None, str, os.PathLike] = None,
        **data: Any,
    ) -> None:
        if not design_root:
            design_root = data.pop("design_root", Path.cwd())
        assert design_root
        if not isinstance(design_root, Path):
            design_root = Path(design_root)
        design_root = design_root.resolve()
        if not data.get("design_root"):
            data["design_root"] = design_root
        with WorkingDirectory(design_root):
            generator = data.get("rtl", {}).get("generator", None)
            if generator:
                log.info("Running generator: %s", generator)
                if isinstance(generator, str):
                    os.system(generator)  # nosec S605
                elif isinstance(generator, (dict, Generator)):
                    if isinstance(generator, (dict)):
                        generator = Generator(**generator)
                    subprocess.run(
                        generator.args,
                        executable=generator.executable,
                        cwd=generator.cwd,
                        shell=generator.shell,  # nosec S602
                        check=generator.check,
                        env=generator.env,
                    )
                else:
                    args = generator
                    # gen_script = Path(args[0])
                    # extension = gen_script.suffix
                    # if extension == ".py":
                    #     if not gen_script.exists():
                    #         log.critical("Generator script not found: %s", gen_script)
                    #         raise FileNotFoundError(gen_script)
                    #     args.insert(0, sys.executable)
                    subprocess.run(args, check=True, cwd=design_root)
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
        assert self.design_root, "design_root is not set!"
        return self.design_root

    @classmethod
    def from_toml(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[None, str, os.PathLike] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
    ) -> DesignType:
        if overrides is None:
            overrides = {}
        if remove_extra is None:
            remove_extra = []
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        assert design_file.suffix == ".toml"
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
        design_root: Union[None, str, os.PathLike] = None,
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
        if design_file.suffix == ".toml":
            design_dict = toml_load(design_file)
        elif design_file.suffix == ".json":
            with open(design_file, "r") as f:
                design_dict = json.load(f)
            design_dict = expand_hierarchy(design_dict)
        else:
            raise ValueError(f"File extension `{design_file.suffix}` is not supported.")
        design_dict = hierarchical_merge(design_dict, overrides)
        if "name" not in design_dict:
            log.warning(
                "'design.name' not specified! Inferring design name: `%s` from design file name.",
                design_file.stem,
            )
            design_dict["name"] = design_file.stem
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
        return src._specified_path

    @cached_property
    def rtl_fingerprint(self) -> Dict[str, Dict[str, str]]:
        return {
            "sources": {str(self.relative_path(src)): src.content_hash for src in self.rtl.sources},
            "parameters": {p: str(v) for p, v in self.rtl.parameters.items()},
        }

    @cached_property
    def rtl_hash(self) -> str:
        # assumptions:
        #  - source file names/paths do not matter
        #  - order of sources does not matter
        #       -> alphabetically sort all file _hashes_
        #  - order of parameters does not matter
        hashes = list(sorted(self.rtl_fingerprint["sources"].values()))
        param_strs = [f"{p}={v}" for p, v in sorted(self.rtl.parameters.items())]
        r = bytes(", ".join(hashes + param_strs), "utf-8")
        return hashlib.sha3_256(r).hexdigest()

    @cached_property
    def tb_hash(self) -> str:
        hashes = list(sorted(src.content_hash for src in self.tb.sources))
        param_strs = [f"{p}={v}" for p, v in self.tb.parameters.items()]
        r = bytes(", ".join(hashes + param_strs), "utf-8")
        return hashlib.sha3_256(r).hexdigest()

    def dict(self):
        return super().dict(
            exclude_unset=True,
            exclude_defaults=True,
            exclude={"rtl_hash", "tb_hash", "rtl_fingerprint"},
        )
