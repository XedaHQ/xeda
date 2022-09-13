from __future__ import annotations

import hashlib
import logging
import os
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, TypeVar, Union

from .dataclass import (
    Extra,
    Field,
    ValidationError,
    XedaBaseModel,
    root_validator,
    validation_errors,
    validator,
)
from .utils import WorkingDirectory, toml_load

log = logging.getLogger(__name__)

__all__ = [
    "Design",
    "DesignSource",
    "FileResource",
    "VhdlSettings",
    "LanguageSettings",
    "Clock",
]


class DesignValidationError(Exception):
    def __init__(
        self,
        errors: List[Tuple[str, str, str]],
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
                for loc, msg, ctx in self.errors
            ),
        )


class FileResource:
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        _root_path: Optional[Path] = None,
    ) -> None:
        try:
            if isinstance(path, dict):
                if "file" not in path:
                    raise ValueError("Required field 'file' is missing.")
                path = path["file"]
            self._path = path
            p = Path(path)
            if not p.is_absolute():
                if not _root_path:
                    _root_path = Path.cwd()
                p = _root_path / p
            self.file = p.resolve(strict=True)
        except FileNotFoundError as e:
            log.critical("Design resource '%s' does not exist!", path)
            raise e from None

    @cached_property
    def content_hash(self) -> str:
        """return hash of file content"""
        with open(self.file, "rb") as f:
            return hashlib.sha3_256(f.read()).hexdigest()

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FileResource):
            return False
        return self.content_hash == other.content_hash and self.file.samefile(
            other.file
        )

    def __hash__(self) -> int:
        # path is already absolute
        return hash((self.content_hash, str(self.file)))

    def __str__(self) -> str:
        return str(self.file)

    def __repr__(self) -> str:
        return "FileResource:" + self.__str__()


class DesignSource(FileResource):
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Optional[str] = None,
        standard: Optional[str] = None,
        variant: Optional[str] = None,
        _root_path: Optional[Path] = None,
        **data: Any,
    ) -> None:
        super().__init__(path, _root_path, **data)

        def type_from_suffix(path: Path) -> Tuple[Optional[str], Optional[str]]:
            type_variants_map = {
                ("vhdl", variant): ["vhd", "vhdl"],
                ("verilog", variant): ["v"],
                ("systemverilog", variant): ["sv"],
                ("bsv", variant): ["bsv"],
                ("bs", variant): ["bs"],
            }
            for h, suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return h
            return None, None

        self.type, self.variant = (typ, variant) if typ else type_from_suffix(self.file)
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


DefineType = Any
# the order matters!
# Union[FileResource, int, bool, float, str]

# Tuple -> Tuple[()] (empty tuple), but pydantic 1.9.0 + Python 3.9 typing do not like it
# Tuple of 0, 1, or 2 strings:
Tuple012 = Union[Tuple[str, ...], Tuple[str], Tuple[str, str]]  # xtype: ignore


class DVSettings(XedaBaseModel):  # type: ignore
    """Design/Verification settings"""

    sources: List[DesignSource]
    generics: Dict[str, DefineType] = Field(
        default={},
        description="Top-level generics/defines specified as a mapping",
        alias="parameters",
        allow_population_by_field_name=True,
        has_alias=True,
    )  # top defines/generics
    parameters: Dict[str, DefineType] = Field(
        default={},
        description="Top-level generics/defines specified as a mapping",
        alias="generics",
        allow_population_by_field_name=True,
        has_alias=True,
    )
    defines: Dict[str, DefineType] = Field(default={})

    # pylint: disable=no-self-argument
    @root_validator()
    def the_root_validator(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        value = values.get("parameters")
        if not value:
            value = values.get("generics")
        if value:
            for k, v in value.items():
                if isinstance(v, dict) and "file" in v:
                    value[k] = FileResource(v).__str__()
            values["generics"] = value
            values["parameters"] = value
        return values

    @validator("sources", pre=True, always=True)
    def sources_to_files(cls, value):
        ds = []
        for src in value:
            if not isinstance(src, DesignSource):
                try:
                    src = DesignSource(src)
                except FileNotFoundError as e:
                    raise ValueError(
                        f"Source file: {src} was not found: {e.strerror} {e.filename}"
                    ) from None
            ds.append(src)
        return ds


class Clock(XedaBaseModel):
    port: str


class RtlSettings(DVSettings):
    """design.rtl"""

    top: str = Field(
        description="Toplevel RTL module/entity",
    )
    clock: Optional[Clock] = None  # TODO rename to primary_clock?
    clocks: Dict[str, Clock] = {}
    clock_port: Optional[str] = None  # TODO remove?

    @root_validator(pre=False)
    def rtl_settings_validate(cls, values):  # pylint: disable=no-self-argument
        """copy equivalent clock fields (backward compatibility)"""
        clock = values.get("clock")
        clock_port = values.get("clock_port")
        clocks = values.get("clocks")

        if clock_port and not clock:
            clock = Clock(port=clock_port)
            values["clock"] = clock
        if clock and not clock_port:
            values["clock_port"] = clock.port
        if clock and not clocks:
            values["clocks"] = {"main_clock": clock}
        return values


class TbSettings(DVSettings):
    """design.tb"""

    top: Tuple012 = Field(
        tuple(),
        description="Toplevel testbench module(s), specified as a tuple of strings. In addition to the primary toplevel, a secondary toplevel module can also be specified.",
    )
    sources: List[DesignSource] = []
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench"
    )
    cocotb: bool = Field(False, description="testbench is based on cocotb framework")

    @validator("top", pre=True)
    def top_validator(cls, top: Union[None, str, Sequence[str], Tuple012]) -> Tuple012:
        if top:
            if isinstance(top, str):
                return (top,)
            if isinstance(top, (tuple, list, Sequence)):
                assert len(top) <= 2
                return tuple(top)
        return tuple()


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(
        None,
        description="Standard version",
        alias="version",
        has_alias=True,
        allow_population_by_field_name=True,
    )
    version: Optional[str] = Field(
        None,
        description="Standard version",
        alias="standard",
        has_alias=True,
        allow_population_by_field_name=True,
    )

    @validator("standard", pre=True)
    def two_digit_standard(cls, standard, values):
        if isinstance(standard, int):
            standard = str(standard)
        elif not isinstance(standard, str):
            raise ValueError("standard should be of type string")
        if standard and len(standard) == 4:
            if standard.startswith("20") or standard.startswith("19"):
                standard = standard[2:]
        return standard

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


class Design(XedaBaseModel):
    name: str
    rtl: RtlSettings
    tb: TbSettings = TbSettings()  # type: ignore
    language: Language = Language()
    _design_root: Optional[Path] = None

    class Config(XedaBaseModel.Config):
        extra = Extra.allow

    def __init__(
        self,
        design_root: Union[None, str, os.PathLike] = None,
        **data: Any,
    ) -> None:
        if not design_root:
            design_root = data.get("_design_root", Path.cwd())
        assert design_root
        if not isinstance(design_root, Path):
            design_root = Path(design_root)
        if not data.get("_design_root"):
            data["_design_root"] = design_root

        with WorkingDirectory(design_root):
            try:
                super().__init__(**data)
            except ValidationError as e:
                raise DesignValidationError(
                    validation_errors(e.errors()), data=data, design_root=design_root  # type: ignore
                ) from None

    @property
    def sim_sources(self) -> List[DesignSource]:
        if not self.tb:
            return []
        return self.rtl.sources + [
            src
            for src in self.tb.sources
            if src not in self.rtl.sources and src.type in ("vhdl", "verilog")
        ]

    @property
    def sim_tops(self) -> Tuple012:
        if self.tb:
            if self.tb.cocotb and self.rtl.top:
                return (self.rtl.top,)
            if self.tb.top is not None:
                return self.tb.top
        return tuple()

    T = TypeVar("T", bound="Design")

    @classmethod
    def from_toml(
        cls: Type[T],
        design_file: Union[str, os.PathLike],
        design_root: Union[None, str, os.PathLike] = None,
    ) -> T:
        """Load and validate a design description from TOML file"""
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        design_dict = toml_load(design_file)
        # Default value for design_root is the folder containing the design description file.
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
                file=str(design_file),
            ) from None

    @cached_property
    def rtl_fingerprint(self) -> dict[str, dict[str, str]]:
        return {
            "sources": {str(src._path): src.content_hash for src in self.rtl.sources},
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
