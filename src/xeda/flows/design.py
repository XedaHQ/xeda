from abc import ABCMeta
import os
from typing import List, Optional, Dict, Tuple, Union, Any
from pydantic import BaseModel, Field, NoneStr, validator, Extra, validate_model
from pydantic.class_validators import root_validator
from pydantic.error_wrappers import ValidationError, display_errors
from pathlib import Path, PurePath
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
    def __init__(self, path: Union[str, os.PathLike, Dict[str, str]], **data) -> None:
        self._content_hash = None
        try:
            if isinstance(path, dict):
                if 'file' not in path:
                    raise Exception(f'Required field `file` is missing.')
                path = path['file']
            p = Path(path)
            if not p.is_absolute():
                p = Path.cwd() / p
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
    def __init__(self, path: str, typ: str = None, standard: str = None, variant: str = None, **data) -> None:
        super().__init__(path, **data)

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


class DVSettings(XedaBaseModel, validate_assignment=True):
    """Design/Verification settings"""
    sources: List[DesignSource]
    top: Optional[Tuple[str, Optional[str]]] = Field(
        None, description="Toplevel module(s) of the design. In addition to the primary toplevel, a secondary toplevel module can also be specified."
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
    def top_validator(cls, top):
        if top is not None:
            if isinstance(top, str):
                top = top.split(",")
            if isinstance(top, list):
                assert 1 <= len(top) <= 2
                top = (top[0], top[1] if len(top) == 2 else None)
        return top

    @validator('sources', pre=True)
    def sources_to_files(cls, sources):
        if sources:
            return [
                src if isinstance(src, DesignSource) else DesignSource(src) for src in sources
            ]


class Clock(BaseModel):
    port: NoneStr


class RtlSettings(DVSettings):
    clock: Clock = Clock(port=None)  # TODO rename to primary_clock?
    clocks: Dict[str, Clock] = {}
    clock_port: NoneStr = None  # TODO remove?

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
    uut: NoneStr = Field(
        None, description="instance name of the unit under test in the testbench")
    secondary_top: NoneStr = Field(
        None, description="Name of the secondary top unit (if available)")
    configuration_specification: NoneStr = None
    cocotb: bool = Field(False, description="testbench is based on cocotb framework")


class LanguageSettings(XedaBaseModel):
    standard: NoneStr = Field(None, description="Standard version",
                              alias='version', has_alias=True, allow_population_by_field_name=True)
    version: NoneStr = Field(None, description="Standard version",
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


class Design(XedaBaseModel, extra=Extra.allow):
    name: str
    rtl: RtlSettings
    tb: Optional[TbSettings] = None
    language: Language = Language()

    @property
    def sim_sources(self):
        return self.rtl.sources + [src for src in self.tb.sources if src not in self.rtl.sources and (src.type == 'vhdl' or src.type == 'verilog')]

    @staticmethod
    def _make_tops_list(x: Union[str, Tuple[str, Optional[str]]]) -> List[str]:
        if isinstance(x, str):
            return [x]
        elif isinstance(x, tuple):
            return [t for t in x if t]
        return x

    @property
    def sim_tops(self) -> List[str]:
        """ a view of tb.top that returns a list of primary_unit [secondary_unit] """
        # conf_spec = self.tb.configuration_specification ## TODO ???
        # if conf_spec:
        #     return [conf_spec]
        if self.tb is None:
            return []
        if self.tb.cocotb and self.rtl.top:
            return self._make_tops_list(self.rtl.top)
        if self.tb.top:
            tops = self._make_tops_list(self.tb.top)
            if self.tb.secondary_top and not tops[1]:
                tops = [tops[0], self.tb.secondary_top]  # FIXME
            return tops
        return []

    def check(self):  # TODO remove? as not serving a purpose (does it even work?)
        *_, validation_error = validate_model(self.__class__, self.__dict__)
        if validation_error:
            raise validation_error

    @classmethod
    def from_toml(cls, design_file: Union[str, PurePath]) -> 'Design':
        design_dict = sanitize_toml(toml.load(design_file))
        try:
            return Design(**design_dict)
        except ValidationError as e:
            errors = e.errors()
            log.critical(f"{len(errors)} error(s) validating design from {design_file}.")
            raise DesignError(
                f"\n{display_errors(errors)}\n"
            ) from None


class DesignError(Exception):
    """Raised when `design` settings are invalid"""
    pass
