"""
XEDA's utility functions and classes
"""

import hashlib
import importlib
import json
import logging
import os
import re
import sys
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

from varname import argname  # type: ignore

from .dataclass import XedaBaseModel

if sys.version_info >= (3, 11):
    import tomllib
else:
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
    "settings_to_dict",
    "XedaException",
    "ToolException",
    "NonZeroExitCode",
    "ExecutableNotFound",
    # etc
    "expand_env_vars",
    "parse_patterns",
    "parse_patterns_in_file",
    "semantic_hash",
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
            key: (
                hierarchical_merge(rtn_dct[key], overrides[key], add_new_keys=add_new_keys)
                if isinstance(rtn_dct.get(key), dict) and isinstance(overrides[key], dict)
                else overrides[key]
            )
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


def get_hierarchy(dct: Dict[str, Any], path: Union[str, List[str]]) -> Optional[Any]:
    if isinstance(path, str):
        path = re.split(SEP, path)
    try:
        return reduce(dict.__getitem__, path, dct)
    except KeyError as e:
        print(f"Error getting hierarchy for path '{path}': {e}")
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
    if sys.version_info >= (3, 9):
        return s.removesuffix(suffix)
    else:
        return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, prefix: str) -> str:
    """similar to str.removeprefix in Python 3.9+"""
    if sys.version_info >= (3, 9):
        return s.removeprefix(prefix)
    else:
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


def expand_hierarchy(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if d is None:
        return {}
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
    # if isinstance(settings, str):
    #     settings = settings.split(",")
    if isinstance(settings, (tuple, list)):
        res: DictStrHier = {}
        for override in settings:
            if isinstance(override, str):
                sp = override.split("=", maxsplit=1)
                if len(sp) != 2:
                    raise ValueError(
                        f"Settings should be in KEY=VALUE format! (value given: {override})"
                    )
                key, val = sp
                set_hierarchy(res, key, conv(val))
            elif override and isinstance(override, dict):
                for key, val in override.items():  # type: ignore
                    set_hierarchy(res, key, conv(val))
        return res
    if isinstance(settings, dict):
        if not hierarchical_keys:
            return settings
        return expand_hierarchy(settings)
    raise TypeError(f"Unsupported type: {type(settings)}")


_xeda_varprog = None


def expand_vars(path: str, environ: Dict[str, str]) -> str:
    """Expand shell variables of form $var and ${var}.  Unknown variables
    are left unchanged.
    This is adapted from Python's os.path.expandvars, but uses explicit environ arg instead of os.environ
    """
    path = os.fspath(path)
    global _xeda_varprog

    if "$" not in path:
        return path
    if not _xeda_varprog:
        _xeda_varprog = re.compile(r"\$(\w+|\{[^}]*\})", re.ASCII)
    search = _xeda_varprog.search
    start = "{"
    end = "}"
    i = 0
    while True:
        m = search(path, i)
        if not m:
            break
        i, j = m.span(0)
        name = m.group(1)
        if name.startswith(start) and name.endswith(end):
            name = name[1:-1]
        try:
            value = environ[name]
        except KeyError:
            i = j
        else:
            tail = path[j:]
            path = path[:i] + value
            i = len(path)
            path += tail
    return path


_ENV_BLACKLIST = [
    "PATH",
    "TMPDIR",
    "LANG",
    "SHELL",
    "USER",
    "LOGNAME",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
    "PYTHONPATH",
    "JAVA_HOME",
    "OLDPWD",
    "LD_PRELOAD",
    "DISPLAY",
    "TERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "COLORTERM",
    "EDITOR",
    "CLICOLOR",
    "INFOPATH",
    "SHLVL",
    "COMMAND_MODE",
    "LDFLAGS",
    "CPPFLAGS",
    "CFLAGS",
    "CXXFLAGS",
    "PAGER",
    "LESS",
    "LSCOLORS",
    "LS_COLORS",
    "BASH_ENV",
    "ZSH",
    "SSH_AUTH_SOCK",
    "ORIGINAL_XDG_CURRENT_DESKTOP",
    "GIT_ASKPASS",
]


def expand_env_vars(path: Union[str, Path], overrides: Optional[Dict[str, Any]] = None) -> Path:
    """Substitute environment variables in path with their values.
    if the value for a variable in overrides is None, then the variable is ignored (not expanded).
    """
    if not isinstance(path, str):
        path = str(path)
    # fast check
    if len(path) < 2:
        return Path(path)
    if overrides is None:
        overrides = {}
    environ = dict(os.environ)
    # we filter out some common environment variables that are not relevant to XEDA
    # also filtering out any variable starting with an underscore ("_")
    for k in environ.keys():
        if k.startswith("_"):
            _ENV_BLACKLIST.append(k)
    for k in _ENV_BLACKLIST:
        environ.pop(k, None)
    for k, v in overrides.items():
        if v is None:
            environ.pop(k, None)
        else:
            environ[k] = str(v)
    path = expand_vars(path, environ)
    return Path(path)

    # # intentionally limiting the pattern to uppercase and 2 characters or more + "/"
    # ENVVAR_START_RE = re.compile(r"^\$(?P<var>[A-Z][A-Z0-9_]+)/")
    # env_match = ENVVAR_START_RE.match(path)
    # if not env_match:
    #     return path
    # var = env_match.group("var")
    # if not var:
    #     return path
    # if var in overrides:
    #     var_value = overrides[var]
    #     if var_value is None:
    #         return path
    # else:
    #     var_value = os.getenv(var)
    #     if var_value is None:
    #         log.warning(
    #             "Environment variable %s not set. Passing on the unchanged value.",
    #             var,
    #         )
    # if var_value is not None:
    #     remainder = path[len(env_match.group(0)) :]
    #     p = Path(var_value) / remainder
    #     log.info(
    #         "Substituting variable %s in path %s with %s. Expanded path is: %s.",
    #         var,
    #         path,
    #         var_value,
    #         str(p),
    #     )
    #     return p
    # return path


class XedaException(Exception):
    """Super-class of all xeda exceptions
    should be catched by CLI and handled appropriately
    """


class ToolException(XedaException):
    """Super-class of all tool exceptions"""


class NonZeroExitCode(ToolException):
    def __init__(self, command_args: Any, exit_code: int, *args: object) -> None:
        if isinstance(command_args, (list, tuple)):
            command_args = " ".join(map(str, command_args))
        self.command_args = command_args
        self.exit_code = exit_code
        super().__init__(*args)

    def __str__(self) -> str:
        return f"Command '{self.command_args}' exited with code {self.exit_code}!"


class ExecutableNotFound(ToolException):
    def __init__(
        self,
        executable: str,
        tool: Optional[str] = None,
        path: Optional[str] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.exec = executable
        self.tool = tool
        self.path = path

    def __str__(self) -> str:
        msg = f"Executable '{self.exec}' "
        if self.tool:
            msg += f"(for {self.tool}) "
        msg += "was not found!"
        if self.path is not None:
            msg += f" (PATH={self.path})"
        return msg

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}: {self.__str__()}"


def parse_patterns(
    content: str,
    re_pattern: Union[str, List[str]],
    *other_re_patterns: Union[str, List[str]],
    flags: re.RegexFlag = re.MULTILINE | re.IGNORECASE,  # re.NOFLAG
    required: bool = False,
    sequential: bool = False,
) -> Optional[dict]:
    results = dict()

    def match_pattern(pat: str, content: str) -> Tuple[bool, str]:
        match = re.search(pat, content, flags)
        if match is None:
            return False, content
        match_dict = match.groupdict()
        for k, v in match_dict.items():
            v = try_convert_to_primitives(v)
            results[k] = v
            log.debug("%s: %s", k, v)
        if sequential:
            content = content[match.span(0)[1] :]
            log.debug("len(content)=%d", len(content))
        return True, content

    for pat in [re_pattern, *other_re_patterns]:
        if not pat:
            continue
        matched = False
        if isinstance(pat, list):
            log.debug("Matching any of: %s", pat)
            for subpat in pat:
                matched, content = match_pattern(subpat, content)
        else:
            log.debug("Matching: %s", pat)
            matched, content = match_pattern(pat, content)

        if not matched and required:
            log.error(
                "Pattern not matched: %s\n",
                pat,
            )
            return None
    return results


def parse_patterns_in_file(
    reportfile_path: Union[str, os.PathLike],
    re_pattern: Union[str, List[str]],
    *other_re_patterns: Union[str, List[str]],
    dotall: bool = True,
    required: bool = False,
    sequential: bool = False,
) -> Optional[dict]:
    if not isinstance(reportfile_path, Path):
        reportfile_path = Path(reportfile_path)
    # TODO fix debug and verbosity levels!
    flags = re.MULTILINE | re.IGNORECASE
    if dotall:
        flags |= re.DOTALL
    with open(reportfile_path) as rpt_file:
        content = rpt_file.read()
        return parse_patterns(
            content,
            re_pattern,
            *other_re_patterns,
            flags=flags,
            required=required,
            sequential=sequential,
        )


def semantic_hash(data: Any) -> str:
    def _sorted_dict_str(data: Any) -> Any:
        if isinstance(data, (dict, Mapping)):
            return {k: _sorted_dict_str(data[k]) for k in sorted(data.keys())}
        if isinstance(data, (list, tuple)):
            return [_sorted_dict_str(val) for val in data]
        if hasattr(data, "__dict__"):
            return _sorted_dict_str(data.__dict__)
        return str(data)

    r = repr(_sorted_dict_str(data))
    return hashlib.sha3_256(bytes(r, "UTF-8")).hexdigest()
