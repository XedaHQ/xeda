from contextlib import AbstractContextManager
import importlib
import json
import logging
import os
import re
from datetime import datetime
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
    Union,
)
from .dataclass import XedaBaseModel

try:
    from functools import cached_property
except ModuleNotFoundError:
    from backports.cached_property import cached_property  # type: ignore

    # pyright: reportMissingImports=none,

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)

__all__ = [
    "SDF",
    # utility functions
    "toml_load",
    "toml_loads",
    "cached_property",
    "unique",
    "WorkingDirectory",
]


class WorkingDirectory(AbstractContextManager[None]):
    def __init__(self, wd: Union[None, str, os.PathLike[Any]]):
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

    def __init__(self, *args: str, **data: str) -> None:
        if args:
            assert len(args) == 1 and isinstance(args[0], str), "only 1 str argument"
            data["max"] = args[0]
        super().__init__(**data)

    def delay_items(self) -> Iterable[Tuple[str, Union[str, None]]]:
        """returns an iterable of (delay_type, sdf_file)"""
        return (
            (delay_type, self.dict().get(delay_type))
            for delay_type in ("min", "max", "typ")
        )


def toml_load(path: Union[str, os.PathLike]) -> Dict[str, Any]:  # type: ignore
    with open(path, "rb") as f:
        return tomllib.load(f)  # type: ignore


def toml_loads(s: str) -> Dict[str, Any]:
    return tomllib.loads(s)  # type: ignore


def backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        log.warning(f"{path} does not exist for backup!")
        return None
    modifiedTime = os.path.getmtime(path)
    suffix = (
        f'.backup_{datetime.fromtimestamp(modifiedTime).strftime("%Y-%m-%d-%H%M%S")}'
    )
    if path.suffix:
        suffix += path.suffix
    backup_path = path.with_suffix(suffix)
    typ = "file" if path.is_file() else "directory" if path.is_dir() else "???"
    log.warning(f"Renaming existing {typ} '{path.name}' to '{backup_path.name}'")
    # TODO use shutil.move instead? os.rename vs Path.rename?
    # os.rename(path, backup_path)
    return path.rename(backup_path)


def dump_json(data: object, path: Path) -> None:
    if path.exists():
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
    # compatible with Python 3.6 ???
    # seen = set()
    # return [x for x in lst if x not in seen and not seen.add(x)]
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
    base_dct: Dict[Any, Any], merge_dct: Dict[Any, Any], add_keys: bool = True
) -> Dict[Any, Any]:
    rtn_dct = base_dct.copy()
    if add_keys is False:
        merge_dct = {
            key: merge_dct[key] for key in set(rtn_dct).intersection(set(merge_dct))
        }

    rtn_dct.update(
        {
            key: dict_merge(rtn_dct[key], merge_dct[key], add_keys=add_keys)
            if isinstance(rtn_dct.get(key), dict) and isinstance(merge_dct[key], dict)
            else merge_dct[key]
            for key in merge_dct.keys()
        }
    )
    return rtn_dct


def try_convert(
    s: Any, convert_lists: bool = False, to_str: bool = True
) -> Union[bool, int, float, str, List[Any]]:
    if s is None:
        return "None"
    if isinstance(s, str):  # always?
        if s.startswith('"') or s.startswith("'"):
            return s.strip("\"'")
        if convert_lists and s.startswith("[") and s.endswith("]"):
            s = re.sub(r"\s+", "", s)
            return [try_convert(e) for e in s.strip("][").split(",")]
        # Should NOT convert dict, set, etc!

    try:
        return int(s)
    except Exception:
        try:
            return float(s)
        except Exception:
            s1 = str(s)
            if s1.lower() in ["true", "yes"]:
                return True
            if s1.lower() in ["false", "no"]:
                return False
            return s1 if to_str else s
