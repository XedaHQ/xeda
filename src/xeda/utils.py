"utility functions and classes"
import importlib
import json
import logging
import os
import re
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime
from functools import cached_property, reduce
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    OrderedDict,
    Tuple,
    Type,
    TypeVar,
    Union,
)


from typeguard.importhook import install_import_hook
from varname import argname

from .dataclass import XedaBaseModel

try:
    import tomllib  # type: ignore # pyright: reportMissingImports=none
except ModuleNotFoundError:
    # python_version < "3.11":
    import tomli as tomllib  # type: ignore


install_import_hook("xeda")

__all__ = [
    "SDF",
    # utility functions
    "toml_load",
    "toml_loads",
    "tomllib",
    "cached_property",
    "unique",
    "WorkingDirectory",
]

log = logging.getLogger(__name__)


_T = TypeVar("_T")


class WorkingDirectory(AbstractContextManager):
    def __init__(self, wd: Union[None, str, os.PathLike]):
        self.prev_wd: Optional[Path] = None
        if wd is not None and not isinstance(wd, Path):
            wd = Path(wd)
        self.wd = wd

    def __enter__(self) -> None:
        self.prev_wd = Path.cwd()
        if self.wd:
            log.debug("Changing working directory from %s to %s", self.prev_wd, self.wd)
            os.chdir(self.wd)

    def __exit__(
        self,
        exception_type: Optional[Type[BaseException]],
        exception_value: Optional[BaseException],
        exception_traceback: Optional[TracebackType],
    ) -> None:
        if self.prev_wd:
            log.debug("Changing back working directory to %s", self.prev_wd)
            os.chdir(self.prev_wd)


class SDF(XedaBaseModel):
    """
    root: region where the SDF is applied
        /TESTBENCH/UUT
    if tb.top=TESTBENCH and the simulation netlist is instantiated inside the testbench with an instance name of UUT
    """

    root: Optional[str] = None
    min: Optional[str] = None
    max: Optional[str] = None
    typ: Optional[str] = None

    # def __init__(self, *args: str, **data: str) -> None:
    #     if args:
    #         assert len(args) == 1 and isinstance(args[0], str), "only 1 str argument"
    #         data["max"] = args[0]
    #     super().__init__(**data)

    def __attrs_post_init__(self):
        pass

    def delay_items(self) -> Iterable[Tuple[str, Union[str, None]]]:
        """returns an iterable of (delay_type, sdf_file)"""
        return tuple(
            (delay_type, getattr(self, delay_type))
            for delay_type in ("min", "max", "typ")
            if getattr(self, delay_type)
        )


def toml_load(path: Union[str, os.PathLike]) -> Dict[str, Any]:  # type: ignore
    with open(path, "rb") as f:
        return tomllib.load(f)  # type: ignore


def toml_loads(s: str) -> Dict[str, Any]:
    return tomllib.loads(s)  # type: ignore


def backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        log.warning("%s does not exist for backup!", path)
        return None
    modifiedTime = os.path.getmtime(path)
    suffix = (
        f'.backup_{datetime.fromtimestamp(modifiedTime).strftime("%Y-%m-%d-%H%M%S")}'
    )
    if path.suffix:
        suffix += path.suffix
    backup_path = path.with_suffix(suffix)
    typ = "file" if path.is_file() else "directory" if path.is_dir() else "???"
    log.warning(
        "Renaming existing %s from '%s' to '%s'", typ, path.name, backup_path.name
    )
    # TODO use shutil.move instead? os.rename vs Path.rename?
    # os.rename(path, backup_path)
    return path.rename(backup_path)


def dump_json(data: object, path: Path, backup_previous: bool = True) -> None:
    if path.exists() and backup_previous:
        backup_existing(path)
        assert not path.exists(), "Old file still exists!"

    with open(path, "w") as outfile:
        json.dump(
            data,
            outfile,
            default=lambda x: x.__dict__ if hasattr(x, "__dict__") else str(x),
            indent=4,
        )


def unique(lst: List[Any]) -> List[Any]:
    """uniquify list while preserving order"""
    return list(OrderedDict.fromkeys(lst))


def camelcase_to_snakecase(name: str) -> str:
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def snakecase_to_camelcase(name: str) -> str:
    return "".join(word.title() for word in name.split("_"))


def load_class(
    full_class_string: str, defualt_module_name: Optional[str] = None
) -> Optional[type]:
    cls_path_lst = full_class_string.split(".")
    assert len(cls_path_lst) > 0

    cls_name = snakecase_to_camelcase(cls_path_lst[-1])
    if len(cls_path_lst) == 1:  # module name not specified, use default
        mod_name = defualt_module_name
    else:
        mod_name = ".".join(cls_path_lst[:-1])
    assert mod_name

    module = importlib.import_module(
        mod_name, __package__ if mod_name.startswith(".") else None
    )
    cls = getattr(module, cls_name)
    if not isinstance(cls, type):
        return None
    return cls


def dict_merge(
    base_dict: Dict[Any, Any], merge_dict: Dict[Any, Any], add_new_keys: bool = True
) -> Dict[Any, Any]:
    """
    returns content of base_dict merge with content of merge_dict.
    if add_new_keys=False keys in merge_dict not existing in base_dict are ignored
    """
    rtn_dct = deepcopy(base_dict)
    if add_new_keys is False:
        merge_dict = {
            key: merge_dict[key] for key in set(rtn_dct).intersection(set(merge_dict))
        }

    rtn_dct.update(
        {
            key: dict_merge(rtn_dct[key], merge_dict[key], add_new_keys=add_new_keys)
            if isinstance(rtn_dct.get(key), dict) and isinstance(merge_dict[key], dict)
            else merge_dict[key]
            for key in merge_dict
        }
    )
    return rtn_dct


def try_convert(
    s: Any, convert_lists: bool = False
) -> Union[bool, int, float, str, List[Union[bool, int, float, str, List[Any]]]]:
    if s is None:
        return "None"
    if isinstance(s, str):
        s = s.strip()
        assert isinstance(s, str)
        if s.startswith('"') or s.startswith("'"):
            return s.strip("\"'")
        if convert_lists and s.startswith("[") and s.endswith("]"):
            s = re.sub(r"\s+", "", s)
            return try_convert(list(s.strip("][").split(",")))
        # Should NOT convert dict, set, etc!
        if re.match(r"^\d+$", s):
            return int(s)
        if s.lower() in ["true", "yes"]:
            return True
        if s.lower() in ["false", "no"]:
            return False
        try:
            return float(s)
        except ValueError:
            pass
        return s
    if isinstance(s, (int, float, bool)):
        return s
    if isinstance(s, (tuple)):
        s = list(s)
    if isinstance(s, (list)):
        return [try_convert(e) for e in s]
    return str(s)


def get_hierarchy(dct: Dict[str, Any], path, sep="."):
    if isinstance(path, str):
        path = path.split(sep)
    try:
        return reduce(dict.__getitem__, path, dct)
    except ValueError:
        return None


def set_hierarchy(dct: Dict[str, Any], path, value, sep="."):
    if isinstance(path, str):
        path = path.split(sep)
    k = path[0]
    if len(path) == 1:
        dct[k] = value
    else:
        if k not in dct:
            dct[k] = {}
        set_hierarchy(dct[k], path[1:], value, sep)


def append_flag(flag_list: List[str], flag: str) -> List[str]:
    if flag not in flag_list:
        flag_list.append(flag)
    return flag_list


def common_root(signals: List[List[_T]]) -> List[_T]:
    longest: Optional[List[_T]] = None
    for sig in signals:
        if not sig:
            continue
        if longest is None:
            longest = sig[:-1]
            continue
        new_len = min(len(longest), len(sig))
        longest = longest[:new_len]
        if not longest:
            break
        for i in range(new_len):
            if sig[i] != longest[i]:
                longest = longest[:i]
                break
    return longest if longest else []


def setting_flag(variable: Any, assign=True, name=None) -> List[str]:
    """skip if none"""
    if variable is None or (not variable and isinstance(variable, (str))):
        return []
    if not name:
        name = argname("variable")
    assert isinstance(name, str)
    if not isinstance(variable, (list, tuple)):
        variable = [variable]
    flags = []
    for v in variable:
        if v:
            flag = "--" + (name.replace("_", "-"))
            if isinstance(v, bool):
                flags.append(flag)
            elif assign:
                flags.append(flag + "=" + str(v))
            else:
                flags += [flag, v]
    return flags
