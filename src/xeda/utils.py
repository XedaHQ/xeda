import re
import importlib
from typing import Any, List
import os
import json
from pathlib import Path
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


def sanitize_toml(obj):
    if isinstance(obj, (str, int, float, bool)):
        return obj
    elif isinstance(obj, list):
        return [sanitize_toml(x) for x in obj]
    elif isinstance(obj, tuple):
        return tuple(sanitize_toml(list(obj)))
    elif isinstance(obj, dict):
        return {k: sanitize_toml(v) for k, v in obj.items()}
    elif hasattr(obj, "__dict__"):
        return sanitize_toml(dict(**obj.__dict__))
    else:
        logger.critical(
            f"ERROR in sanitize_toml: unhandled object of type %s: %s", type(obj), obj
        )
        return sanitize_toml(dict(obj))


def backup_existing(path: Path):
    if not path.exists():
        logger.warning(f"path {path} does not exist to backup!")
        return
    modifiedTime = os.path.getmtime(path)
    suffix = (
        f'.backup_{datetime.fromtimestamp(modifiedTime).strftime("%Y-%m-%d-%H%M%S")}'
    )
    if path.suffix:
        suffix += path.suffix
    backup_path = path.with_suffix(suffix)
    typ = "file" if path.is_file() else "directory" if path.is_dir() else "???"
    logger.warning(f"Renaming existing {typ} '{path.name}' to '{backup_path.name}'")
    os.rename(path, backup_path)  # TODO use shutil.move instead?


def dump_json(data, path: Path):
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
    return list(dict.fromkeys(lst))


def camelcase_to_snakecase(name: str) -> str:
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def snakecase_to_camelcase(name: str) -> str:
    return "".join(word.title() for word in name.split("_"))


def load_class(full_class_string: str, defualt_module_name=None) -> type:
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
    return getattr(module, cls_name)


def dict_merge(base_dct, merge_dct, add_keys=True):
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


def try_convert(s, convert_lists=False, to_str=True):
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
            if s1.lower in ["true", "yes"]:
                return True
            if s1.lower in ["false", "no"]:
                return False
            return s1 if to_str else s
