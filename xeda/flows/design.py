from pydantic import BaseModel
from pydantic.types import NoneStr
from typing import List, Optional, Sequence, Dict, Tuple
from pathlib import Path
import logging
import hashlib

logger = logging.getLogger()


class FileResource:
    @classmethod
    def is_file_resource(cls, src):
        return isinstance(src, cls) or (isinstance(src, dict) and 'file' in src)

    def __init__(self, path: str, **data) -> None:
        try:
            p = Path(path)
            if not p.is_absolute():
                p = Path.cwd() / p
            self.file = p.resolve(strict=True)
        except FileNotFoundError as e:
            logger.critical(f"Design source file '{path}' does not exist!")
            raise e

        with open(self.file, 'rb') as f:
            self.hash = hashlib.sha256(f.read()).hexdigest()

    def __eq__(self, other):
        # path is already absolute
        return self.hash == other.hash and self.file.samefile(other.file)

    def __hash__(self):
        # path is already absolute
        return hash(tuple(self.hash, str(self.file)))

    def __str__(self):
        return str(self.file)

    def __repr__(self) -> str:
        return 'FileResource:' + self.__str__()


class DesignSource(FileResource):
    @classmethod
    def is_design_source(cls, src):
        return cls.is_file_resource(src)

    def __init__(self, path: str, typ: str = None, standard: str = None, variant: str = None, **data) -> None:
        super().__init__(path, **data)

        def type_from_suffix(path: Path) -> Tuple[Optional[str], Optional[str]]:
            type_variants_map = {
                ('vhdl', variant): ['vhd', 'vhdl'],
                ('verilog', variant): ['v'],
                ('verilog', 'systemverilog'): ['sv'],
                ('bsv', variant): ['bsv'],
                ('bs', variant): ['bs'],
            }
            for h, suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return h
            return None, None

        self.type, self.variant = (
            typ, variant) if typ else type_from_suffix(self.file)
        self.standard = standard


class PhaseSettings(BaseModel):
    sources: List[DesignSource]
    top: NoneStr
    generics: Dict[str, str] = {}  # top defines/generics

    def __init__(self, **data):
        sources = data.get('sources')
        if sources is not None and isinstance(sources, Sequence):
            sources = [DesignSource(src) if isinstance(
                src, str) else src for src in sources]
            data.pop('sources')
        super().__init__(sources=sources, **data)

    class Config:
        arbitrary_types_allowed = True


class RtlSettings(PhaseSettings):
    clock_port: str


class TbSettings(PhaseSettings):
    uut: NoneStr = None
    secondary_top: NoneStr = None
    configuration_specification: NoneStr = None


class LanguageSettings(BaseModel):
    standard: NoneStr = None


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(BaseModel):
    vhdl: VhdlSettings = VhdlSettings()
    verilog: LanguageSettings = LanguageSettings()


class Design(BaseModel):
    name: str
    rtl: RtlSettings
    tb: TbSettings = TbSettings(sources=[], top=None)
    language: Language = Language()


class DesignError(Exception):
    """Raised when `design` settings are invalid"""
    pass
