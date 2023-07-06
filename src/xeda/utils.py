"utility functions and classes"
import importlib
import json
import logging
import os
import re
import time
import unittest
from collections import defaultdict
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime, timedelta
from functools import cached_property, reduce
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    OrderedDict,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from xml.etree import ElementTree

from varname import argname

from .dataclass import XedaBaseModel

try:
    import tomllib  # pyright: ignore reportMissingImports
except ModuleNotFoundError:
    # python_version < "3.11":
    import tomli as tomllib  # type: ignore


# install_import_hook("xeda")

__all__ = [
    "SDF",
    "WorkingDirectory",
    "Timer",
    # re-exports
    "tomllib",
    "cached_property",
    # utility functions
    "load_class",
    "dump_json",
    "toml_loads",
    "parse_xml",
    "try_convert_to_primitives",
    "try_convert",
    # list/container utils
    "unique",
    # str utils
    "camelcase_to_snakecase",
    "snakecase_to_camelcase",
    "regex_match",
    "removesuffix",
    "removeprefix",
    # dict utils
    "hierarchical_merge",
    "get_hierarchy",
    "set_hierarchy",
    "first_value",
    "first_key",
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


def toml_load(path: Union[str, os.PathLike]) -> Dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def toml_loads(s: str) -> Dict[str, Any]:
    return tomllib.loads(s)


def backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        log.warning("%s does not exist for backup!", path)
        return None
    modifiedTime = os.path.getmtime(path)
    suffix = (
        f".backup_{datetime.fromtimestamp(modifiedTime):%Y-%m-%d-%H%M%S}"
        # .strftime("")}'
    )
    if path.suffix:
        suffix += path.suffix
    backup_path = path.with_suffix(suffix)
    typ = "file" if path.is_file() else "directory" if path.is_dir() else "???"
    log.debug("Renaming existing %s from '%s' to '%s'", typ, path.name, backup_path.name)
    while backup_path.exists():
        backup_path = backup_path.with_suffix(backup_path.suffix + "_")
    return path.rename(backup_path)


def dump_json(data: object, path: Path, backup: bool = True, indent: int = 4) -> None:
    if path.exists() and backup:
        backup_existing(path)
        assert not path.exists(), "Old file still exists!"

    with open(path, "w") as outfile:
        json.dump(
            data,
            outfile,
            default=lambda x: x.__dict__ if hasattr(x, "__dict__") else str(x),
            indent=indent,
        )


def unique(lst: List[Any]) -> List[Any]:
    """returns unique elements of the list in their original order (first occurrence)."""
    return list(OrderedDict.fromkeys(lst))


def camelcase_to_snakecase(name: str) -> str:
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def snakecase_to_camelcase(name: str) -> str:
    return "".join(word.title() for word in name.split("_"))


def load_class(full_class_string: str, default_module_name: Optional[str] = None) -> Optional[type]:
    cls_path_lst = full_class_string.split(".")
    assert len(cls_path_lst) > 0

    cls_name = snakecase_to_camelcase(cls_path_lst[-1])
    if len(cls_path_lst) == 1:  # module name not specified, use default
        mod_name = default_module_name
    else:
        mod_name = ".".join(cls_path_lst[:-1])
    assert mod_name

    module = importlib.import_module(mod_name, __package__ if mod_name.startswith(".") else None)
    cls = getattr(module, cls_name)
    if not isinstance(cls, type):
        return None
    return cls


def hierarchical_merge(
    base_dict: Dict[Any, Any], overrides: Dict[Any, Any], add_new_keys: bool = True
) -> Dict[Any, Any]:
    """
    Hierarchical merge of 'overrides' into 'base_dict' and return the resulting dict
    returns content of base_dict merge with content of merge_dict.
    if add_new_keys=False keys in merge_dict not existing in base_dict are ignored
    """
    rtn_dct = deepcopy(base_dict)
    if add_new_keys is False:
        overrides = {key: overrides[key] for key in set(rtn_dct).intersection(set(overrides))}

    rtn_dct.update(
        {
            key: hierarchical_merge(rtn_dct[key], overrides[key], add_new_keys=add_new_keys)
            if isinstance(rtn_dct.get(key), dict) and isinstance(overrides[key], dict)
            else overrides[key]
            for key in overrides
        }
    )
    return rtn_dct


_T1 = TypeVar("_T1")
_D1 = TypeVar("_D1")


def try_convert(value: Any, typ: Type[_T1], default: Optional[_D1] = None) -> Union[None, _T1, _D1]:
    try:
        return typ(value)  # type: ignore
    except:  # noqa
        return default


def try_convert_to_primitives(
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
            return try_convert_to_primitives(list(s.strip("][").split(",")))
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
        return [try_convert_to_primitives(e) for e in s]
    return str(s)


# hierarchical key separator is dot (.), but can be escaped with a \ (e.g., "\\.")
SEP = r"(?<!\\)\."


def get_hierarchy(dct: Dict[str, Any], path):
    if isinstance(path, str):
        path = path.split(SEP)
    try:
        return reduce(dict.__getitem__, path, dct)
    except ValueError:
        return None


def set_hierarchy(dct: Dict[str, Any], path, value):
    if isinstance(path, str):
        path = re.split(SEP, path)
    k = path[0]
    if len(path) == 1:
        if isinstance(value, (dict)):
            new_value: Dict[str, Any] = {}
            for k2, v2 in value.items():
                set_hierarchy(new_value, k2, v2)
            value = new_value
        dct[k] = value
    else:
        if k not in dct:
            dct[k] = {}
        set_hierarchy(dct[k], path[1:], value)


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


HierDict = Dict[str, Union[None, str, Dict[str, Any]]]


def parse_xml(
    report_xml: Union[Path, os.PathLike, str],
    tags_whitelist: Optional[List[str]] = None,
    tags_blacklist: Optional[List[str]] = None,
    skip_empty_children: bool = True,
) -> Optional[HierDict]:
    def etree_to_dict_rec(
        t: ElementTree.Element,
    ) -> HierDict:
        d: HierDict = {t.tag: {} if t.attrib else None}
        children = list(t)
        if children:
            dd = defaultdict(list)
            for dc in map(etree_to_dict_rec, children):
                for k, v in dc.items():
                    if skip_empty_children and isinstance(v, dict) and not v:
                        continue
                    dd[k].append(v)
            d = {t.tag: {k: v[0] if len(v) == 1 else v for k, v in dd.items()}}
        if t.attrib:
            tag = t.tag
            d[tag].update(  # type: ignore
                ("@" + k, v)
                for k, v in t.attrib.items()
                if (tags_blacklist is None or k not in tags_blacklist)
                and (tags_whitelist is None or k in tags_whitelist)
            )
        if t.text:
            text = t.text.strip()
            if children or t.attrib:
                if text:
                    d[t.tag]["#text"] = text  # type: ignore
            else:
                d[t.tag] = text
        return d

    try:
        tree = ElementTree.parse(report_xml)
    except FileNotFoundError:
        log.critical("File %s not found.", report_xml)
        return None
    except ElementTree.ParseError as e:
        log.critical("Parsing %s failed: %s", report_xml, e.msg)
        return None
    root = tree.getroot()
    return etree_to_dict_rec(root)


class Timer:
    def __init__(self, hi_res: bool = False) -> None:
        self.hi_res = hi_res
        self.time_func = time.monotonic_ns if hi_res else time.monotonic
        self.start_time = self.time_func()

    def delta(self):
        return self.time_func() - self.start_time

    @property
    def nanoseconds(self) -> int:
        d = self.delta()
        return int(d if self.hi_res else d * 1000_000_000)

    @property
    def seconds(self) -> int:
        if self.hi_res:
            return self.nanoseconds // 1000_000_000
        return int(self.delta())

    @property
    def minutes(self) -> int:
        return self.seconds // 60

    @property
    def hours(self) -> int:
        return self.minutes // 60

    @property
    def timedelta(self):
        return timedelta(seconds=self.seconds)


def regex_match(string: str, pattern: str, ignorecase: bool = False) -> Optional[re.Match]:
    if not isinstance(string, str):
        return None
    return re.match(pattern, string, flags=re.I if ignorecase else 0)


def removesuffix(s: str, suffix: str) -> str:
    """similar to str.removesuffix in Python 3.9+"""
    return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, prefix: str) -> str:
    """similar to str.removeprefix in Python 3.9+"""
    return s[len(prefix) :] if prefix and s.startswith(prefix) else s


_K = TypeVar("_K")
_V = TypeVar("_V")


def first_value(d: Dict[_K, _V]) -> Optional[_V]:  # pyright: ignore reportInvalidTypeVarUse
    return next(iter(d.values())) if d else None


def first_key(d: Dict[_K, _V]) -> Optional[_K]:  # pyright: ignore reportInvalidTypeVarUse
    return next(iter(d)) if d else None


class UnitTestUtils(unittest.TestCase):
    def test_try_convert(self):
        self.assertEqual(try_convert(" ", int), None)
        self.assertEqual(try_convert("", int), None)
        self.assertEqual(try_convert("xx", int), None)
        self.assertEqual(try_convert("1234", int), 1234)
        self.assertEqual(try_convert("1234.5", float), 1234.5)


# DictStrHier = Dict[str, "StrOrDictStrHier"]
DictStrHier = Dict[str, Any]
StrOrDictStrHier = Union[str, DictStrHier]


def conv(v):
    # if not isinstance(v, (dict, Mapping, list, tuple, Sequence, str, int, float, bool)):
    #     v = try_convert_to_primitives(v, convert_lists=True)
    return v


def expand_hierarchy(d: Dict[str, Any]) -> Dict[str, Any]:
    expanded: DictStrHier = {}
    for k, v in d.items():
        set_hierarchy(expanded, k, conv(v))
    return expanded


def settings_to_dict(
    settings: Union[List[str], Tuple[str, ...], Mapping[str, StrOrDictStrHier]],
    hierarchical_keys: bool = True,
) -> Dict[str, Any]:
    if not settings:
        return {}
    if isinstance(settings, str):
        settings = settings.split(",")
    if isinstance(settings, (tuple, list)):
        res: DictStrHier = {}
        for override in settings:
            sp = override.split("=")
            if len(sp) != 2:
                raise ValueError(
                    f"Settings should be in KEY=VALUE format! (value given: {override})"
                )
            key, val = sp
            set_hierarchy(res, key, conv(val))
        return res
    if isinstance(settings, dict):
        if not hierarchical_keys:
            return settings
        return expand_hierarchy(settings)
    raise TypeError(f"Unsupported type: {type(settings)}")
