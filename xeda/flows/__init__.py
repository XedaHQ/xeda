# © 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)

from pathlib import Path

from typing import Union, Dict, List
import hashlib

JsonType = Union[str, int, float, bool, List['JsonType'], 'JsonTree']
JsonTree = Dict[str, JsonType]
StrTreeType = Union[str, List['StrTreeType'], 'StrTree']
StrTree = Dict[str, StrTreeType]


fake_fields = {'author', 'url', 'comment', 'description', 'license'}

def semantic_hash(data: JsonTree, hash_files=True, hasher=hashlib.sha256) -> str:
    def get_digest(b: bytes):
        return hasher(b).hexdigest()
    def file_digest(filename: str):
        with open(filename, 'rb') as f:
            return get_digest(f.read())
    def sorted_dict_str(data: JsonType) -> StrTreeType:
        if type(data) == dict:
            return {k: sorted_dict_str(file_digest(data[k]) if hash_files and (k == 'file') else data[k]) for k in sorted(data.keys()) if not k in fake_fields}
        elif type(data) == list:
            return [sorted_dict_str(val) for val in data]
        elif hasattr(data, '__dict__'):
            return sorted_dict_str(data.__dict__)
        else:
            return str(data)

    return get_digest(bytes(repr(sorted_dict_str(data)), 'UTF-8'))


class Settings:
    def __init__(self) -> None:
        self.flow = dict()
        self.design = dict()
        self.run = dict()


class DesignSource:
    @classmethod
    def is_design_source(cls, src):
        return isinstance(src, dict) and 'file' in src
    
    def __init__(self, file: str, type: str = None, sim_only: bool = False, standard: str = None, variant: str = None, comment: str = None) -> None:
        def type_from_suffix(file: Path) -> str:
            type_variants_map = {
                ('vhdl', variant): ['vhd', 'vhdl'],
                ('verilog', variant): ['v'],
                ('verilog', 'systemverilog'): ['sv'],
                ('bsv', variant): ['bsv'],
                ('bs', variant): ['bs'],
            }
            for h, suffixes in type_variants_map.items():
                if file.suffix[1:] in suffixes:
                    return h
            return None, None

        file = Path(file)

        self.file: Path = file
        self.type, self.variant = (type, variant) if type else type_from_suffix(file)
        self.sim_only = sim_only
        self.standard = standard
        self.comment = comment


def try_convert(s):
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            s = str(s)
            if s.lower in ['true', 'yes']:
                return True
            if s.lower in ['false', 'no']:
                return False
            return s
