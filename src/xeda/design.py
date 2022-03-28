import os
from typing import List, Optional, Dict, Sequence, Tuple, Union, Any
from pydantic import Extra, Field, validate_model, validator, root_validator
from pydantic.error_wrappers import ValidationError
from pathlib import Path
import logging
import hashlib

from .utils import WorkingDirectory, toml_load
from .dataclass import XedaBaseModel, validation_errors

log = logging.getLogger(__name__)

__all__ = [
    "from_toml",
    "Design",
    "DesignSource",
    "FileResource",
    "VhdlSettings",
    "LanguageSettings",
    "Clock",
]


def from_toml(
    design_file: Union[str, os.PathLike],
    design_root: Union[None, str, os.PathLike] = None,
) -> "Design":
    """Load and validate a design description from TOML file"""
    if not isinstance(design_file, Path):
        design_file = Path(design_file)
    design_dict = toml_load(design_file)
    # Default value for design_root is the folder containing the design description file.
    if design_root is None:
        design_root = design_file.parent
    try:
        return Design(design_root=design_root, **design_dict)
    except DesignValidationError as e:
        raise DesignValidationError(  # add design_file to the emitted exception
            e.errors, e.data, e.design_root, e.design_name, str(design_file)
        ) from None


class DesignValidationError(Exception):
    def __init__(
        self,
        errors: List[Tuple[str, str, str]],
        data: Dict[str, Any],
        design_root: Union[None, str, os.PathLike] = None,
        design_name: Optional[str] = None,
        file: Optional[str] = None,
        *args: object,
    ) -> None:
        super().__init__(*args)
        self.errors = errors  # location, msg, type/context
        self.data = data
        self.design_root = design_root
        self.design_name = design_name
        self.file = file


class FileResource:
    def __init__(
        self,
        path: Union[str, os.PathLike[Any], Dict[str, str]],
        _root_path: Optional[Path] = None,
        **data: Any,
    ) -> None:
        self._content_hash: Optional[str] = None
        try:
            if isinstance(path, dict):
                if "file" not in path:
                    raise ValueError("Required field 'file' is missing.")
                path = path["file"]
            p = Path(path)
            if not p.is_absolute():
                if not _root_path:
                    _root_path = Path.cwd()
                p = _root_path / p
            self.file = p.resolve(strict=True)
        except FileNotFoundError as e:
            log.critical(f"Design resource '{path}' does not exist!")
            raise e

    @property
    def hash(self) -> str:
        """return hash of file content"""
        if self._content_hash is None:
            with open(self.file, "rb") as f:
                self._content_hash = hashlib.sha256(f.read()).hexdigest()
        return self._content_hash

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, FileResource):
            return False
        return self.hash == other.hash and self.file.samefile(other.file)

    def __hash__(self) -> int:
        # path is already absolute
        return hash((self.hash, str(self.file)))

    def __str__(self) -> str:
        return str(self.file)

    def __repr__(self) -> str:
        return "FileResource:" + self.__str__()


class DesignSource(FileResource):
    def __init__(
        self,
        path: Union[str, os.PathLike[Any], Dict[str, str]],
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

    def __eq__(self, other: Any) -> bool:
        # added attributes do not change semantic equality
        return super().__eq__(other)

    def __hash__(self) -> int:
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
    top: Tuple012 = Field(
        tuple(),
        description="Toplevel module(s) of the design. In addition to the primary toplevel, a secondary toplevel module can also be specified.",
    )
    generics: Dict[str, DefineType] = Field(
        default=dict(),
        description="Top-level generics/defines specified as a mapping",
        alias="parameters",
        allow_population_by_field_name=True,
        has_alias=True,
    )  # top defines/generics
    parameters: Dict[str, DefineType] = Field(
        default=dict(),
        description="Top-level generics/defines specified as a mapping",
        alias="generics",
        allow_population_by_field_name=True,
        has_alias=True,
    )
    defines: Dict[str, DefineType] = Field(default=dict())

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

    @validator("top", pre=True)
    def top_validator(cls, top: Union[None, str, Sequence[str], Tuple012]) -> Tuple012:
        if top:
            if isinstance(top, str):
                top = (top,)
            if isinstance(top, Sequence):
                assert len(top) <= 2
                top = tuple(top)
            return top
        return tuple()

    @validator("sources", pre=True, always=False)
    def sources_to_files(
        cls, sources: List[Union[DesignSource, str, os.PathLike, Path, Dict[str, Any]]]
    ) -> List[DesignSource]:
        return [
            src if isinstance(src, DesignSource) else DesignSource(src)
            for src in sources
        ]

    @property
    def primary_top(self) -> str:
        return self.top[0] if self.top else ""


class Clock(XedaBaseModel):
    port: Optional[str]


class RtlSettings(DVSettings):
    clock: Clock = Clock(port=None)  # TODO rename to primary_clock?
    clocks: Dict[str, Clock] = {}
    clock_port: Optional[str] = None  # TODO remove?

    @root_validator(pre=False)
    def rtl_settings_validate(cls, values):
        clock = values.get("clock")
        clock_port = values.get("clock_port")
        if not clock.port:
            clock.port = clock_port
        if not clock_port:
            values["clock_port"] = clock.port
        if not values.get("clocks"):
            values["clocks"] = {"main_clock": clock}
        return values


class TbSettings(DVSettings):
    sources: List[DesignSource] = []
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench"
    )
    cocotb: bool = Field(False, description="testbench is based on cocotb framework")


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
    vhdl: VhdlSettings = VhdlSettings()
    verilog: LanguageSettings = LanguageSettings()


class Design(XedaBaseModel):
    name: str
    rtl: RtlSettings
    tb: TbSettings = TbSettings()
    language: Language = Language()

    class Config(XedaBaseModel.Config):
        extra = Extra.allow

    def __init__(
        self,
        design_root: Union[None, str, os.PathLike] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        with WorkingDirectory(design_root):
            try:
                super().__init__(*args, **kwargs)
            except ValidationError as e:
                raise DesignValidationError(
                    validation_errors(e.errors()), data=kwargs, design_root=design_root
                ) from None

    @property
    def sim_sources(self) -> List[DesignSource]:
        if not self.tb:
            return []
        return self.rtl.sources + [
            src
            for src in self.tb.sources
            if src not in self.rtl.sources
            and (src.type == "vhdl" or src.type == "verilog")
        ]

    @property
    def sim_tops(self) -> Tuple012:
        if self.tb:
            if self.tb.cocotb and self.rtl.top:
                return self.rtl.top
            else:
                return self.tb.top
        return ()

    def check(
        self,
    ) -> None:  # TODO remove? as not serving a purpose (does it even work?)
        *_, validation_error = validate_model(self.__class__, self.__dict__)
        if validation_error:
            raise validation_error

    @staticmethod
    def from_toml(
        design_file: Union[str, os.PathLike],
        design_root: Union[None, str, os.PathLike] = None,
    ) -> "Design":
        return from_toml(design_file, design_root)
