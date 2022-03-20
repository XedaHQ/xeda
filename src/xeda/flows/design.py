from abc import ABCMeta
import os
from typing import List, Optional, Dict, Tuple, Union, Any
from pydantic import BaseModel, Extra, Field, validate_model, validator
from pydantic.class_validators import root_validator
from pydantic.error_wrappers import ValidationError, display_errors
from pathlib import Path
import logging
import hashlib
import toml
from ..utils import sanitize_toml

log = logging.getLogger(__name__)


class XedaBaseModel(BaseModel, metaclass=ABCMeta):
    class Config:
        validate_assignment = True
        extra = Extra.forbid
        arbitrary_types_allowed = True


class FileResource:
    def __init__(self, path: Union[str, os.PathLike, Dict[str, str]], _root_path: Optional[Path] = None, **data) -> None:
        self._content_hash = None
        try:
            if isinstance(path, dict):
                if 'file' not in path:
                    raise Exception(f'Required field `file` is missing.')
                path = path['file']
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
    def hash(self):
        """return hash of file content"""
        if self._content_hash is None:
            with open(self.file, 'rb') as f:
                self._content_hash = hashlib.sha256(f.read()).hexdigest()
        return self._content_hash

    def __eq__(self, other):
        if not isinstance(other, FileResource):
            return False
        return self.hash == other.hash and self.file.samefile(other.file)

    def __hash__(self):
        # path is already absolute
        return hash(tuple(self.hash, str(self.file)))

    def __str__(self):
        return str(self.file)

    def __repr__(self) -> str:
        return 'FileResource:' + self.__str__()


class DesignSource(FileResource):
    def __init__(self, path: str, typ: str = None, standard: str = None, variant: str = None, _root_path: Optional[Path] = None, **data) -> None:
        super().__init__(path, _root_path, **data)

        def type_from_suffix(path: Path) -> Tuple[Optional[str], Optional[str]]:
            type_variants_map = {
                ('vhdl', variant): ['vhd', 'vhdl'],
                ('verilog', variant): ['v'],
                ('systemverilog', variant): ['sv'],
                ('bsv', variant): ['bsv'],
                ('bs', variant): ['bs'],
            }
            for h, suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return h
            return None, None

        self.type, self.variant = (
            typ, variant) if typ else type_from_suffix(self.file)
        if standard and len(standard) == 4:
            if standard.startswith("20") or standard.startswith("19"):
                standard = standard[2:]
        self.standard = standard

    def __eq__(self, other):
        # added attributes do not change semantic equality
        return super().__eq__(other)


DefineType = Any
# the order matters!
# Union[FileResource, int, bool, float, str]

# Tuple -> Tuple[()] (empty tuple), but pydantic 1.9.0 + Python 3.9 typing do not like it
DesignTopType = Union[Tuple, Tuple[str], Tuple[str, str]]


class DVSettings(XedaBaseModel):
    """Design/Verification settings"""
    # public fields
    sources: List[DesignSource]
    top: DesignTopType = Field(
        tuple(), description="Toplevel module(s) of the design. In addition to the primary toplevel, a secondary toplevel module can also be specified."
    )
    generics: Dict[str, DefineType] = Field(
        default=dict(),
        description='Top-level generics/defines specified as a mapping',
        alias='parameters',
        allow_population_by_field_name=True,
        has_alias=True,
    )  # top defines/generics
    parameters: Dict[str, DefineType] = Field(
        default=dict(),
        description='Top-level generics/defines specified as a mapping',
        alias='generics',
        allow_population_by_field_name=True,
        has_alias=True,
    )
    defines: Dict[str, DefineType] = Field(default=dict())

    @root_validator()
    def the_root_validator(cls, values):
        value = values.get('parameters')
        if not value:
            value = values.get('generics')
        if value:
            for k, v in value.items():
                if isinstance(v, dict) and 'file' in v:
                    value[k] = FileResource(v).__str__()
            values['generics'] = value
            values['parameters'] = value
        return values

    @validator('top', pre=True)
    def top_validator(cls, top) -> DesignTopType:
        if top:
            if isinstance(top, str):
                top = (top,)
            if isinstance(top, list):
                assert len(top) <= 2
                top = tuple(top)
            return top
        return tuple()

    @validator('sources', pre=True, always=False)
    def sources_to_files(cls, sources):
        return [
            src if isinstance(src, DesignSource) else DesignSource(src) for src in sources
        ]

    @property
    def primary_top(self) -> str:
        return self.top[0] if self.top else ""


class Clock(BaseModel):
    port: Optional[str]


class RtlSettings(DVSettings):
    clock: Clock = Clock(port=None)  # TODO rename to primary_clock?
    clocks: Dict[str, Clock] = {}
    clock_port: Optional[str] = None  # TODO remove?

    @root_validator(pre=False)
    def rtl_settings_validate(cls, values):
        clock = values.get('clock')
        clock_port = values.get('clock_port')
        if not clock.port:
            clock.port = clock_port
        if not clock_port:
            values['clock_port'] = clock.port
        if not values.get('clocks'):
            values['clocks'] = {'main_clock': clock}
        return values


class TbSettings(DVSettings):
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench")
    configuration_specification: Optional[str] = None
    cocotb: bool = Field(False, description="testbench is based on cocotb framework")


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(None, description="Standard version",
                              alias='version', has_alias=True, allow_population_by_field_name=True)
    version: Optional[str] = Field(None, description="Standard version",
                             alias='standard', has_alias=True, allow_population_by_field_name=True)

    @validator('standard', pre=True)
    def two_digit_standard(cls, standard, values):
        if standard and len(standard) == 4:
            if standard.startswith("20") or standard.startswith("19"):
                standard = standard[2:]
        return standard

    @root_validator(pre=True)
    def language_root_validator(cls, values):
        if 'standard' in values:
            values['version'] = values['standard']
        return values


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(XedaBaseModel):
    vhdl: VhdlSettings = VhdlSettings()
    verilog: LanguageSettings = LanguageSettings()


class Design(XedaBaseModel):
    name: str
    rtl: RtlSettings
    tb: Optional[TbSettings] = None
    language: Language = Language()

    class Config(XedaBaseModel.Config):
        extra = Extra.allow

    @property
    def sim_sources(self):
        return self.rtl.sources + [src for src in self.tb.sources if src not in self.rtl.sources and (src.type == 'vhdl' or src.type == 'verilog')]

    @property
    def sim_tops(self) -> DesignTopType:
        if self.tb:
            if self.tb.cocotb and self.rtl.top:
                return self.rtl.top
            else:
                return self.tb.top
        return ()

    def check(self):  # TODO remove? as not serving a purpose (does it even work?)
        *_, validation_error = validate_model(self.__class__, self.__dict__)
        if validation_error:
            raise validation_error

    @classmethod
    def from_toml(cls, design_file: Union[str, os.PathLike], design_root: Union[None, str, os.PathLike] = None) -> 'Design':
        """Load and validate a design description in TOML fromat"""
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        toml_dict = toml.load(design_file)
        design_dict = sanitize_toml(toml_dict)
        if design_root is None:
            # Default value for design_root is the folder containing the design description file.
            design_root = design_file.parent
        current_wd = Path.cwd()
        try:
            os.chdir(design_root)
            return Design(**design_dict)
        except ValidationError as e:
            errors = e.errors()
            log.critical(f"{len(errors)} error(s) validating design from {design_file}.")
            raise InvalidDesign(
                f"\n{display_errors(errors)}\n"
            ) from None
        finally:
            os.chdir(current_wd)


class InvalidDesign(Exception):
    """Failed to validate Design properties"""
