from __future__ import annotations

import difflib
import errno
import hashlib
import importlib
import inspect
import json
import logging
import os
import pprint
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from glob import escape as glob_escape
from glob import glob
from os.path import isfile
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic_core import core_schema

from .dataclass import (
    AliasChoices,
    Field,
    SerializeAsAny,
    ValidationError,
    XedaBaseModel,
    field_validator,
    input_names,
    model_validator,
    model_with_allow_extra,
    validation_errors,
)
from .digest import content_digest
from .generation import judging_generation
from .proc_utils import tool_output_redirect
from .run_dir import RunDirectory
from .utils import (
    NonZeroExitCode,
    WorkingDirectory,
    XedaException,
    expand_env_vars,
    expand_hierarchy,
    hierarchical_merge,
    location_free,
    removesuffix,
    semantic_hash,
    settings_to_dict,
    toml_load,
    unique,
)
from .yaml_loader import load_yaml, yaml_error_position

log = logging.getLogger(__name__)

#: What a design's name may be: it names the design's run directories (`<run root>/<name>/`), so
#: it is one path component that no file system reads as anything else.
DESIGN_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")

#: The parts of a design a flow can read, of which `Flow.design_parts` names a subset: the RTL
#: and the testbench. `_DESIGN_PART_ORDER` is the order every consumer visits them in.
DESIGN_PARTS: frozenset[str] = frozenset({"rtl", "tb"})
_DESIGN_PART_ORDER = ("rtl", "tb")

__all__ = [
    "AnyDesignValidationException",
    "Clock",
    "DESIGN_PARTS",
    "Design",
    "DesignFileParseError",
    "DesignSource",
    "DesignValidationError",
    "FileResource",
    "LanguageSettings",
    "SOURCE_SUFFIXES",
    "AMBIGUOUS_SUFFIXES",
    "TYPE_ONLY",
    "LANGUAGE_TYPES",
    "source_type_named",
    "source_type_of",
    "SourceType",
    "VhdlSettings",
]


def pformat(data):
    return pprint.pformat(data, compact=True, sort_dicts=False).removeprefix("{").removesuffix("}")


class DesignFileParseError(XedaException):
    """A design file that cannot be read as a design: it cannot be opened, is not valid
    TOML/JSON/YAML, has a suffix xeda does not read, or does not hold a table of design fields.

    Names the file and, when the parser knows it, the 1-based `line` and `column`.
    """

    def __init__(
        self,
        file: str | os.PathLike,
        reason: str,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        """Record the design file and source location of a load failure."""
        self.file = str(Path(file).absolute())
        self.reason = reason
        self.line = line
        self.column = column
        super().__init__(self.file, reason, line, column)  # rebuilt from `args` when unpickled

    def __str__(self) -> str:
        """Include the file and available source location in the error message."""
        where = f'"{self.file}"'
        if self.line is not None:
            where += f", line {self.line}"
            if self.column is not None:
                where += f", column {self.column}"
        return f"Cannot load design file {where}: {self.reason}"


class AnyDesignValidationException(XedaException):
    pass


class DesignValidationError(AnyDesignValidationException):
    def __init__(
        self,
        errors: List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]],
        data: Optional[Dict[str, Any]] = None,
        *args: object,
        design_root: Union[str, os.PathLike, None] = None,
        design_name: Optional[str] = None,
        file: Optional[str] = None,
        design_in_msg: bool = False,
    ) -> None:
        super().__init__(*args)
        self.errors = errors  # (location, message, context, type)
        self.data = data or {}
        self.design_root = design_root
        self.design_name = design_name
        self.file = file
        self.design_in_msg = design_in_msg

    def __str__(self) -> str:
        """Format validation errors with their design name and field locations."""

        def fmt_loc(loc):
            if loc:
                return ".".join(re.split(r"\s*->\s*", loc)) + ":\n "
            return ""

        name = self.design_name or self.data.get("name")
        return "{}: {} error{} validating design{}{}\n{}".format(
            self.__class__.__qualname__,
            len(self.errors),
            "s" if len(self.errors) > 1 else "",
            f" '{name}'" if name else "",
            # the design file `from_file` attaches: the one thing that says where to look
            f' in "{self.file}"' if self.file else "",
            "\n".join(f"{fmt_loc(loc)}{msg}\n" for loc, msg, _, _ in self.errors),
        ) + (f"\nDesign:\n{pformat(self.data)}\n" if self.data and self.design_in_msg else "")


#: The format a design file is read in, by its suffix. The one rule for what a design file is:
#: `Design.from_file` reads by it, and the launcher tells a design file from a design's name by it.
DESIGN_FILE_FORMATS: dict[str, str] = {
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def names_a_design_file(path: str | os.PathLike) -> bool:
    """Whether `path` is meant as a design file rather than as a design's name in a project:
    it has a design-file suffix, in any case. A mis-cased suffix still means a file -- which
    `Design.from_file` then rejects naming the right spelling -- rather than a name to look up
    in a project and report missing there."""
    return Path(path).suffix.lower() in DESIGN_FILE_FORMATS


def design_file_format(path: str | os.PathLike) -> str:
    """The format (`toml`, `json` or `yaml`) design file `path` is read in, by its suffix.

    Suffixes are case-sensitive, like every name xeda reads: `.TOML` is rejected naming
    `.toml`, not read as whatever it resembles.
    """
    suffix = Path(path).suffix
    fmt = DESIGN_FILE_FORMATS.get(suffix)
    if fmt is not None:
        return fmt
    if suffix.lower() in DESIGN_FILE_FORMATS:
        reason = (
            f"file suffix {suffix!r}: suffixes are case-sensitive, "
            f"did you mean {suffix.lower()!r}?"
        )
    else:
        what = f"unsupported file suffix {suffix!r}" if suffix else "no file suffix"
        supported = ", ".join(repr(known) for known in DESIGN_FILE_FORMATS)
        reason = f"{what}; a design file is TOML, JSON or YAML ({supported})"
    raise DesignFileParseError(path, reason)


def _toml_error_position(e: tomllib.TOMLDecodeError) -> tuple[str, int | None, int | None]:
    """`(message, line, column)` of a TOML error. Python 3.14 gives them as attributes; before
    that they are only in the message's `(at line L, column C)` suffix."""
    line, column = getattr(e, "lineno", None), getattr(e, "colno", None)
    msg = getattr(e, "msg", None)
    if isinstance(msg, str) and line is not None:
        return msg, line, column
    m = re.fullmatch(r"(.*?)\s*\(at line (\d+), column (\d+)\)", str(e), re.DOTALL)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    return str(e), None, None


def _read_design_file(path: Path) -> dict[str, Any]:
    """The table of design fields in design file `path`.

    Every way that fails is a `DesignFileParseError` naming the file (and the line and column,
    when the parser knows them): a suffix xeda does not read, a file that cannot be opened or is
    not UTF-8, TOML/JSON/YAML that does not parse, or a document that is not a table.
    """
    fmt = design_file_format(path)
    try:
        if fmt == "toml":
            data = toml_load(path)
        elif fmt == "json":
            data = json.loads(path.read_bytes())
        else:
            data = load_yaml(path)
    except OSError as e:
        raise DesignFileParseError(path, e.strerror or str(e)) from None
    except UnicodeDecodeError as e:
        raise DesignFileParseError(path, f"not UTF-8 text: {e.reason} at byte {e.start}") from None
    except yaml.reader.ReaderError as e:
        raise DesignFileParseError(
            path, f"not UTF-8 text: {e.reason} at byte {e.position}"
        ) from None
    except tomllib.TOMLDecodeError as e:
        raise DesignFileParseError(path, *_toml_error_position(e)) from None
    except json.JSONDecodeError as e:
        # `lineno` and `colno` are 1-based already
        raise DesignFileParseError(path, e.msg, e.lineno, e.colno) from None
    except yaml.MarkedYAMLError as e:
        raise DesignFileParseError(path, *yaml_error_position(e)) from None
    except yaml.YAMLError as e:
        raise DesignFileParseError(path, str(e)) from None
    if data is None:  # an empty YAML document, as an empty TOML file is an empty table
        data = {}
    if not isinstance(data, dict):
        raise DesignFileParseError(
            path,
            "a design file holds a table (mapping) of design fields, "
            f"not a {type(data).__name__}",
        )
    return data


def _expand_design_path(
    path: Union[str, os.PathLike], root: Path, escape: Optional[Callable[[str], str]] = None
) -> Path:
    """`path` with its environment variables expanded. `$DESIGN_ROOT` and `$DESIGN_DIR` are
    `root`, the directory a relative path resolves against, so `$DESIGN_ROOT/a.vhd` and `a.vhd`
    are always the same file. `$PWD` is left as written. `escape` applies to every substituted
    value (`glob.escape`, for a pattern)."""
    return expand_env_vars(
        str(path), {"PWD": None, "DESIGN_ROOT": root, "DESIGN_DIR": root}, escape=escape
    )


def _is_source_pattern(source: str) -> bool:
    """Whether a source names its files by pattern. Only `*` is pattern syntax: `?`, `[` and `]`
    are ordinary characters of a file name (`foo[1].v`, a bus index), and as glob syntax they
    made such a name match another file (`foo1.v`) whenever one existed. A `*` is no character
    of any file name worth naming (Windows forbids it)."""
    return "*" in source


def _expand_source_glob(pattern: str, root: Path, what: str = "source") -> List[str]:
    """The files a source pattern (`_is_source_pattern`) names, in a stable order.

    Expanded before globbing, or `glob` looks for a directory literally named `$DESIGN_ROOT`.
    Sorted, because `glob` returns them in filesystem order while source order is *semantic*:
    it is the order VHDL units are compiled in, and it is part of the design hash, so an
    unsorted pattern gives the same design a different identity on a different filesystem.
    A pattern that names no file is an error -- contributing nothing silently is how a mistyped
    pattern used to reach a tool as a missing top-level unit.

    A variable's value is a place, not more pattern, so it is escaped: a design root named
    `proj[1]` otherwise matched a sibling `proj1`, another project's sources. Only files match,
    as a shell glob of source files would; a directory named like a source is passed over.
    """
    expanded = str(_expand_design_path(pattern, root))
    matched = _globbed_source_files(pattern, root)
    if not matched:
        raise ValueError(
            f"no file matches the {what} pattern '{pattern}'"
            + (f" (expanded to '{expanded}')" if expanded != pattern else "")
        )
    return matched


def _globbed_source_files(pattern: str, root: Path) -> List[str]:
    """Files matching a pattern, in which only `*` matches: `?` and `[` are escaped in the text
    written, and variable values are literal path components altogether."""
    written = re.sub(r"[?\[]", r"[\g<0>]", pattern)
    expanded = _expand_design_path(written, root, escape=glob_escape)
    return sorted(m for m in glob(str(expanded)) if isfile(m))


def _source_paths_as_given(sources: Any, root: Path) -> Optional[List[Path]]:
    """The files an unvalidated `rtl.sources` names, for deciding if a generator must run again.

    `process_generation` runs *before* the sources validator, so it sees whatever the design
    file wrote: a path, a `$DESIGN_ROOT` spelling, a glob or a `{ file = ... }` table. Comparing
    those strings to the filesystem directly made every spelling but a plain relative path look
    absent, so the generator re-ran on every load, and a table raised `Path(dict)` as an opaque
    TypeError. Returns `None` for anything uninterpretable: the validator reports that properly
    a moment later, and until then the safe answer is to run the generator.
    """
    if isinstance(sources, (str, os.PathLike, Mapping, FileResource)):
        sources = [sources]  # the scalar shorthand the sources validator also accepts
    if not isinstance(sources, (list, tuple, set)):
        return None
    paths: List[Path] = []
    for src in sources:
        if isinstance(src, FileResource):
            paths.append(src.file)
            continue
        if isinstance(src, Mapping):
            src = src.get("file") or src.get("path")
        if not isinstance(src, (str, os.PathLike)):
            return None
        if isinstance(src, str) and _is_source_pattern(src):
            # A pattern that matches nothing yet is exactly the case for running the generator.
            for match in _globbed_source_files(src, root):
                path = Path(match)
                paths.append(path if path.is_absolute() else root / path)
        else:
            expanded = _expand_design_path(src, root)
            paths.append(expanded if expanded.is_absolute() else root / expanded)
    return paths


def _describe_generator(generator: Any) -> str:
    """How to name a generator in an error, whichever of its three input forms the design used."""
    if isinstance(generator, Generator):
        return generator.command_text()
    if isinstance(generator, (list, tuple)):
        return " ".join(str(part) for part in generator)
    return str(generator)


class FileResource:
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        _root_path: Optional[Path] = None,
        resolve=True,
    ) -> None:
        """A file of the design: a path, or a table naming either a `file`, which must exist, or
        a `path`, which is not checked (a file a generator creates later). A plain path is a
        `file`; a missing one raises `FileNotFoundError`.

        A relative path resolves against `_root_path`, by default the working directory, which
        `Design` sets to the design root while it validates; see `_expand_design_path`.
        """
        if isinstance(path, dict):
            path_value = path.get("path")
            if path_value:
                resolve = False  # override resolve
            file_value = path.get("file")
            if path_value and file_value:
                raise ValueError("'file' and 'path' are mutually exclusive.")
            path_value = path_value or file_value
            if not path_value:
                raise ValueError(
                    "Either 'file' (existing file) or 'path' (unchecked path) must be set for a FireResource."
                )
            path = path_value
        root = Path(_root_path) if _root_path else Path.cwd()
        # As *written*, before any variable is expanded or the path is made absolute. Nothing
        # about a design's identity depends on it -- see `Design._source_fingerprint` -- but a
        # flow that has to name a file after where it came from (GHDL disambiguating two VHDL
        # sources with the same stem) wants what the design said, not an absolute location.
        self._specified_path = Path(path)
        path = _expand_design_path(path, root)
        if not path.is_absolute():
            path = root / path
        if resolve and not path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        if resolve and not path.is_file():
            raise IsADirectoryError(errno.EISDIR, "a directory, not a file", str(path))
        self.file = path.resolve() if resolve else path.absolute()
        #: Whether this names a file that must exist (`file`) or one that need not yet (`path`).
        self._checked = bool(resolve)

    @property
    def path(self) -> Path:
        return self.file

    @cached_property
    def content_hash(self) -> str:
        """Hash of the file's content -- what a design is identified by.

        A file that is not there has no content and so no identity: the design cannot be
        hashed, cached or run. `{ path = ... }` defers the *check*, it does not waive it; by the
        time a run is identified, whatever was going to write the file has had its chance.
        """
        try:
            return content_digest(self.file)
        except IsADirectoryError as e:
            raise IsADirectoryError(
                errno.EISDIR, "a directory where the design names a file", str(self.file)
            ) from e
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"{self.file} does not exist, so the design that uses it cannot be identified. "
                "A file given as `{ path = ... }` is deliberately not checked while the design "
                "loads, on the understanding that a generator writes it -- nothing did."
            ) from e

    # One resource is one file: the same place, whatever it holds -- which also covers a
    # `{ path = ... }` file not written yet. Comparing contents here made de-duplicating such a
    # source (on every re-validation) raise, and added nothing: the same file has the same
    # contents. `file` is absolute, and resolved for a checked resource.
    def __eq__(self, other: Any) -> bool:
        """Compare resources by their stored absolute paths."""
        if not isinstance(other, FileResource):
            return False
        return self.file == other.file

    def __hash__(self) -> int:
        """Hash the stored absolute path used for resource equality."""
        return hash(str(self.file))

    def __str__(self) -> str:
        return str(self.file)

    def __repr__(self) -> str:
        return f"file:{self.file}"

    def get_specified_path(self):
        return self._specified_path

    def as_json_value(self) -> Union[str, Dict[str, Any]]:
        """This resource as a JSON value that loads back as the same resource.

        A bare path string reloads as a *checked* `file`, so a resource built from
        `{ path = ... }` -- one naming a file that need not exist yet, a generator's output or a
        file a testbench writes -- has to keep saying `path`, or reloading the document it was
        written to fails on a file the design never promised was there.
        """
        return str(self.file) if self._checked else {"path": str(self.file)}

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Validate as an arbitrary type: an instance check.

        Values reach here already coerced by the `mode="before"` validators on the fields that
        declare them, so an isinstance check is all that is required. The serializer is
        `when_used="json"` so that `model_dump()` returns the object itself while
        serializing to JSON emits what `as_json_value()` builds: the path, or the
        table the resource cannot be rebuilt without.
        """
        return core_schema.is_instance_schema(
            cls,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value.as_json_value(),
                return_schema=core_schema.any_schema(),
                when_used="json",
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> Dict[str, Any]:
        """Make this arbitrary type declarable in JSON Schema.

        Without this, `Design.model_json_schema()` raises `PydanticInvalidForJsonSchema`, which
        would leave editors and coding agents without any machine-readable description of a
        design file.
        """
        return cls._json_schema()

    @classmethod
    def _json_schema(cls) -> Dict[str, Any]:
        field_schema: Dict[str, Any] = {}
        field_schema.update(
            title=cls.__name__,
            description=(
                "A file resource: either a path string (relative paths are resolved against the "
                "design file's directory) or an object with a 'file' key (an existing file) or a "
                "'path' key (an unchecked path)."
            ),
            anyOf=[
                {"type": "string"},
                {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Path to an existing file. Mutually exclusive with 'path'.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Path that is not checked for existence. Mutually exclusive with 'file'.",
                        },
                    },
                    # `oneOf`, not `anyOf`: FileResource rejects an object that gives neither
                    # *and* one that gives both, so exactly one branch must match. Under `anyOf`
                    # an object carrying both keys satisfies both branches and validated here
                    # while `Design.from_file` always rejected it.
                    "oneOf": [{"required": ["file"]}, {"required": ["path"]}],
                },
            ],
        )
        return field_schema


class SourceType(str, Enum):
    """Type of a design source file.

    Values are the member names themselves so that serialized settings and results are
    self-describing. NOTE: the declaration order is significant, see `from_str`: members are
    only ever appended after the last one, never inserted or reordered.
    """

    Verilog = "Verilog"
    VerilogHeader = "VerilogHeader"
    SystemVerilog = "SystemVerilog"
    SVHeader = "SVHeader"
    Vhdl = "Vhdl"
    Bluespec = "Bluespec"
    Xdc = "Xdc"
    Sdc = "Sdc"
    MemoryFile = "MemoryFile"
    Tcl = "Tcl"
    Chisel = "Chisel"
    Cpp = "Cpp"
    Cocotb = "Cocotb"
    # the ordinals 1-13 above are frozen (old settings.json files); every member below is appended
    Lpf = "Lpf"
    Pcf = "Pcf"
    Pdc = "Pdc"
    JsonNetlist = "JsonNetlist"
    EcpConfig = "EcpConfig"
    IceAsc = "IceAsc"
    Fasm = "Fasm"
    Bitstream = "Bitstream"
    VerilogNetlist = "VerilogNetlist"
    VhdlNetlist = "VhdlNetlist"
    Blif = "Blif"
    Edif = "Edif"
    Ucf = "Ucf"
    Xcf = "Xcf"
    Qsf = "Qsf"
    Ldc = "Ldc"
    Fdc = "Fdc"
    Sdf = "Sdf"
    Spef = "Spef"
    Saif = "Saif"
    Vcd = "Vcd"
    Fst = "Fst"
    Ghw = "Ghw"
    Vpd = "Vpd"
    Fsdb = "Fsdb"
    Checkpoint = "Checkpoint"
    Liberty = "Liberty"
    Def = "Def"
    Odb = "Odb"
    Gds = "Gds"
    Cdl = "Cdl"
    Chipdb = "Chipdb"
    C = "C"
    CHeader = "CHeader"
    ObjectFile = "ObjectFile"
    Vlt = "Vlt"
    Data = "Data"

    def __str__(self) -> str:
        return str(self.name)

    @classmethod
    def from_str(cls, source_type: str) -> Optional[SourceType]:
        try:
            return cls[source_type]
        except KeyError:
            pass
        try:
            return cls[source_type.capitalize()]
        except KeyError:
            pass
        for k, v in cls.__members__.items():
            if k.lower() == source_type.lower():
                return v
        # Backward compatibility: this enum previously used `auto()`, so settings.json files
        # written by older versions of xeda record 1-based ordinals ("5" for Vhdl) instead of
        # names. The declaration order above must not change while this is supported.
        if source_type.isdigit():
            members = list(cls.__members__.values())
            idx = int(source_type)
            if 1 <= idx <= len(members):
                return members[idx - 1]
        return None


#: The suffix (without its dot, and in its letter case: inference is case-sensitive) each type
#: is inferred from, with its variant: one member per suffix. A member missing here is given only
#: by `type` (`TYPE_ONLY`). `tests/test_source_types.py` pins both.
SOURCE_SUFFIXES: dict[str, tuple[SourceType, str | None]] = {
    "v": (SourceType.Verilog, None),
    "vh": (SourceType.VerilogHeader, None),
    "sv": (SourceType.SystemVerilog, None),
    "svh": (SourceType.SVHeader, None),
    "vhd": (SourceType.Vhdl, None),
    "vhdl": (SourceType.Vhdl, None),
    "bsv": (SourceType.Bluespec, "bsv"),
    "bs": (SourceType.Bluespec, "bh"),
    "bh": (SourceType.Bluespec, "bh"),
    "xdc": (SourceType.Xdc, None),
    "sdc": (SourceType.Sdc, None),
    "mem": (SourceType.MemoryFile, None),
    "init": (SourceType.MemoryFile, None),
    "hex": (SourceType.MemoryFile, None),
    "tcl": (SourceType.Tcl, None),
    "sc": (SourceType.Chisel, None),
    "cc": (SourceType.Cpp, None),
    "cpp": (SourceType.Cpp, None),
    "cxx": (SourceType.Cpp, None),
    "py": (SourceType.Cocotb, None),
    "lpf": (SourceType.Lpf, None),
    "pcf": (SourceType.Pcf, None),
    "pdc": (SourceType.Pdc, None),
    "asc": (SourceType.IceAsc, None),
    "fasm": (SourceType.Fasm, None),
    "bit": (SourceType.Bitstream, None),
    "sof": (SourceType.Bitstream, None),
    "blif": (SourceType.Blif, None),
    "edf": (SourceType.Edif, None),
    "edif": (SourceType.Edif, None),
    "ucf": (SourceType.Ucf, None),
    "xcf": (SourceType.Xcf, None),
    "qsf": (SourceType.Qsf, None),
    "ldc": (SourceType.Ldc, None),
    "fdc": (SourceType.Fdc, None),
    "sdf": (SourceType.Sdf, None),
    "spef": (SourceType.Spef, None),
    "saif": (SourceType.Saif, None),
    "vcd": (SourceType.Vcd, None),
    "fst": (SourceType.Fst, None),
    "ghw": (SourceType.Ghw, None),
    "vpd": (SourceType.Vpd, None),
    "fsdb": (SourceType.Fsdb, None),
    "dcp": (SourceType.Checkpoint, None),
    "lib": (SourceType.Liberty, None),
    "def": (SourceType.Def, None),
    "odb": (SourceType.Odb, None),
    "gds": (SourceType.Gds, None),
    "cdl": (SourceType.Cdl, None),
    "c": (SourceType.C, None),
    "h": (SourceType.CHeader, None),
    "hpp": (SourceType.CHeader, None),
    "o": (SourceType.ObjectFile, None),
    "a": (SourceType.ObjectFile, None),
    "vlt": (SourceType.Vlt, None),
}

#: Suffixes that name several kinds of file: a source with one needs its `type`. Each
#: lists the members it is most often, for the message.
AMBIGUOUS_SUFFIXES: dict[str, tuple[SourceType, ...]] = {
    "json": (SourceType.JsonNetlist,),
    "bin": (SourceType.Bitstream, SourceType.Chipdb),
    "cfg": (SourceType.EcpConfig,),
    "config": (SourceType.EcpConfig,),
}

#: Members no suffix infers: given by an explicit `type` only (or, later, by a declared output).
TYPE_ONLY: frozenset[SourceType] = frozenset(
    {
        SourceType.JsonNetlist,
        SourceType.EcpConfig,
        SourceType.VerilogNetlist,
        SourceType.VhdlNetlist,
        SourceType.Chipdb,
        SourceType.Data,
    }
)


#: Languages whose omission would leave an incomplete design. Contract flows refuse a
#: language they cannot read; other source types may belong to another flow.
LANGUAGE_TYPES: frozenset[SourceType] = frozenset(
    {
        SourceType.Verilog,
        SourceType.SystemVerilog,
        SourceType.Vhdl,
        SourceType.Bluespec,
        SourceType.Chisel,
    }
)


def source_type_named(text: str) -> SourceType:
    """The member `text` names (`SourceType.from_str`: its name in any letter case, or an old
    `settings.json`'s ordinal); a `ValueError` naming the closest members otherwise."""
    member = SourceType.from_str(text)
    if member is not None:
        return member
    names = [m.name for m in SourceType]
    by_lower = {name.lower(): name for name in names}
    close = [
        by_lower[match]
        for match in difflib.get_close_matches(text.lower(), list(by_lower), n=3, cutoff=0.6)
    ]
    hint = f"; did you mean {' or '.join(f'`{name}`' for name in close)}?" if close else ""
    raise ValueError(f"unknown source type {text!r}{hint} (the types: {', '.join(names)})")


def source_type_of(path: Path) -> tuple[SourceType, str | None]:
    """The type and variant `path`'s suffix says (`SOURCE_SUFFIXES`); a `ValueError` asking for
    an explicit `type` when the suffix names several kinds of file, is in another letter case
    than the one xeda knows, or is one xeda infers nothing from."""
    suffix = path.suffix[1:]
    if suffix in SOURCE_SUFFIXES:
        return SOURCE_SUFFIXES[suffix]
    if suffix in AMBIGUOUS_SUFFIXES:
        candidates = AMBIGUOUS_SUFFIXES[suffix]
        raise ValueError(
            f"{path}: `.{suffix}` names several kinds of file "
            f"({', '.join(m.name for m in candidates)} among them): give its type, e.g. "
            f'{{ file = "{path.name}", type = "{candidates[0].name}" }}'
        )
    if suffix.lower() in SOURCE_SUFFIXES:
        member = SOURCE_SUFFIXES[suffix.lower()][0]
        raise ValueError(
            f"{path}: xeda infers no source type from `.{suffix}` (`.{suffix.lower()}` is "
            f'{member.name}): rename it, or give its type, e.g. {{ file = "{path.name}", '
            f'type = "{member.name}" }}'
        )
    what = f"`.{suffix}`" if suffix else "a file without a suffix"
    raise ValueError(
        f"{path}: xeda infers no source type from {what}: give its type, e.g. "
        f'{{ file = "{path.name}", type = "Data" }} -- `Data` has no automatic HDL frontend (test '
        f"vectors, a script's input); the types: {', '.join(m.name for m in SourceType)}"
    )


class DesignSource(FileResource):
    type: SourceType

    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Union[str, SourceType, None] = None,
        standard: Optional[str] = None,
        variant: Optional[str] = None,
        _root_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        """Build a design source while retaining its stated metadata."""
        if isinstance(path, dict):
            path = dict(path)
            stated_type = path.pop("type", None)
            if typ is None:
                typ = stated_type
            stated_standard = path.pop("standard", None)
            if standard is None:
                standard = stated_standard
            stated_variant = path.pop("variant", None)
            if variant is None:
                variant = stated_variant
            rp = path.pop("root_path", None)
            if not _root_path and rp:
                _root_path = Path(rp)
        super().__init__(path, _root_path=_root_path, **kwargs)

        self.variant = variant
        if isinstance(typ, SourceType):
            self.type = typ
        elif isinstance(typ, str):
            self.type = source_type_named(typ)
        elif typ is None:
            self.type, inferred_variant = source_type_of(self.file)
            if variant is None:
                self.variant = inferred_variant
        else:
            raise ValueError(f"a source's `type` is the name of a source type, not {typ!r}")
        self.standard = standard
        # What the design *stated*, as opposed to what the filename implied. Only the former is
        # written back out: reloading re-infers the latter from the same suffix, while a stated
        # `type` that contradicts it (`{ file = "legacy.v", type = "SystemVerilog" }`) is lost
        # for good if the dump leaves it out -- and it is part of the design's identity.
        self._stated: dict[str, Any] = {
            key: value
            for key, value in (
                ("type", str(self.type) if typ is not None else None),
                ("standard", standard),
                ("variant", variant),
            )
            if value is not None
        }

    def as_json_value(self) -> Union[str, Dict[str, Any]]:
        """Serialize a source with only the metadata explicitly supplied."""
        value = super().as_json_value()
        if not self._stated:
            return value
        if isinstance(value, str):
            value = {"file": value}
        return {**value, **self._stated}

    def __eq__(self, other: Any) -> bool:  # pylint: disable=useless-super-delegation
        # added attributes do not change semantic equality
        return super().__eq__(other)

    def __hash__(self) -> int:  # pylint: disable=useless-super-delegation
        # added attributes do not change semantic identity
        return super().__hash__()

    def __repr__(self) -> str:
        s = f"file:{self.file} type:{self.type}"
        if self.variant:
            s += f" variant: {self.variant}"
        if self.standard:
            s += f" standard: {self.standard}"
        return s

    @classmethod
    def _json_schema(cls) -> Dict[str, Any]:
        field_schema = super()._json_schema()
        field_schema["description"] = (
            "A design source file: either a path string (relative paths are resolved against the "
            "design file's directory) or an object. The source type is inferred from the file "
            "extension unless 'type' is given explicitly."
        )
        object_schema = field_schema["anyOf"][1]
        object_schema["properties"].update(
            {
                "type": {
                    "type": "string",
                    "enum": [t.name for t in SourceType],
                    "description": "Source type. Inferred from the file extension when omitted.",
                },
                "standard": {
                    "type": "string",
                    "description": "Language standard for this source, e.g. '2008' for VHDL or '2012' for SystemVerilog.",
                },
                "variant": {
                    "type": "string",
                    "description": "Language variant, e.g. 'bsv' or 'bh' for Bluespec sources.",
                },
            }
        )
        return field_schema


DefineType = Any
# the order matters!
# Union[FileResource, int, bool, float, str]

# Tuple of 0, 1, or 2 strings:
Tuple012 = Union[Tuple[str, ...], Tuple[str], Tuple[str, str]]  # xtype: ignore


_PARAMETERS_FORM_ERROR = (
    "parameters/generics must be a dictionary or a list of objects with 'name' and 'value' "
    "attributes"
)


def _parameters_as_mapping(value: Any) -> Any:
    """The two interchangeable parameter/generic input forms as the one mapping form.

    Anything that is neither is returned unchanged, for the caller to reject or ignore.
    """
    if not isinstance(value, list):
        return value
    normalized = {}
    for entry in value:
        # Checked before `.get`: a bare list such as `parameters = ["W"]` otherwise escaped
        # as `AttributeError: 'str' object has no attribute 'get'`, which the validator
        # guard does not turn into a validation error.
        if not isinstance(entry, Mapping):
            raise ValueError(f"{_PARAMETERS_FORM_ERROR}, got a list entry {entry!r}")
        entry_name = entry.get("name")
        entry_value = entry.get("value")
        if entry_name and entry_value is not None:
            normalized[entry_name] = entry_value
        else:
            raise ValueError(f"{_PARAMETERS_FORM_ERROR}, got {dict(entry)!r}")
    return normalized


def _normalize_parameters(value: Any) -> Any:
    """Normalize the two interchangeable parameter/generic input forms.

    A parameter written as a file (`{ file = ... }`, which must exist, or `{ path = ... }`, which
    need not yet) becomes that file's absolute path: a tool takes a parameter as plain text, and
    it runs in a run directory, not the design's. That text is then the parameter's one value;
    nothing else remembers the table it was written as. What a design's identity must not depend
    on -- where the design lives -- is left out where it is counted (`Design._parameters_
    fingerprint`), and a remote run re-roots such a path from the value alone (`send_design`).
    """
    if value is None:
        # Not `if not value`: an empty list (`parameters = []`) is the empty list form, and must
        # become `{}` like any other list rather than reach the mapping field as a list.
        return value
    value = _parameters_as_mapping(value)
    if not isinstance(value, dict):
        raise ValueError(f"{_PARAMETERS_FORM_ERROR}, got {type(value).__name__}: {value!r}")
    for key, parameter in value.items():
        if isinstance(parameter, dict) and ("file" in parameter or "path" in parameter):
            try:
                value[key] = str(FileResource(parameter))
            except IsADirectoryError as e:
                raise ValueError(f"parameter {key!r}: {e.filename} is a directory") from e
            except FileNotFoundError as e:
                raise ValueError(f"parameter {key!r}: file does not exist: {e.filename}") from e
    return value


#: What `sources` accepts, shown by `xeda design-schema`.
SOURCES_DESCRIPTION = (
    "Source files, in compile order: a path relative to the design root (`$DESIGN_ROOT` and "
    "`$DESIGN_DIR` name it too), or a table `{ file = ..., type = ..., standard = ..., "
    "variant = ... }` (`path` in place of `file` for one not written yet). A path containing `*` "
    "is a pattern: its files are inserted in sorted order, and it must match at least one. `*` is "
    "the only pattern character: `?`, `[` and `]` are part of a file name, so `rtl/fifo[1].v` "
    "names exactly that file."
)


class DVSettings(XedaBaseModel):
    """Design/Verification settings"""

    sources: List[DesignSource] = Field(description=SOURCES_DESCRIPTION)
    parameters: Dict[str, DefineType] = Field(
        default={},
        validation_alias=AliasChoices("parameters", "generics"),
        description="Top-level parameters (Verilog) or generics (VHDL): a mapping, or a list of "
        "`{name, value}` objects. `generics` is accepted as the same setting's other name.",
    )
    defines: Dict[str, DefineType] = Field(default={})

    @property
    def generics(self) -> Dict[str, DefineType]:
        """`parameters`, by its VHDL name: one setting, stored once."""
        return self.parameters

    @generics.setter
    def generics(self, value: Any) -> None:
        self.parameters = value

    @field_validator("parameters", mode="before")
    @classmethod
    def _validate_parameters(cls, value):
        return _normalize_parameters(value)

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value):
        """Normalize source entries into file resources."""

        def src_with_type(src, src_type):
            if src_type:
                return {"file": src, "type": src_type}
            return src

        def ds(src: Union[str, Path], typ=None):
            """Create a design source from a path and optional source type."""
            return DesignSource(src, typ=typ)

        if isinstance(value, (str, Path, DesignSource)):
            value = [value]
        sources: List[DesignSource] = []

        def source_already_exists(src: DesignSource) -> bool:
            for s in sources:
                if s.file.absolute() == src.file.absolute():
                    return True
            return False

        if isinstance(value, (str, Path, FileResource, Mapping)):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            raise ValueError(
                f"'sources' must be a list of source files, got {type(value).__name__}: {value!r}"
            )
        for src in unique(list(value)):
            if isinstance(src, str):
                src_type = None
                # m = re.match(r"^([a-zA-Z0-9_]*)\:(.*)", src)
                # if m:
                #     src = m.group(2)
                #     src_type = SourceType.from_str(m.group(1))
                if _is_source_pattern(src):
                    glob_sources = _expand_source_glob(src, Path.cwd())
                    srcs = [ds(s, src_type) for s in glob_sources]
                    sources.extend(s for s in srcs if not source_already_exists(s))
                    continue  # skip the append at the bottom
                src = src_with_type(src, src_type)
            if not isinstance(src, DesignSource):
                try:
                    if isinstance(src, (str, Path)):
                        src = ds(src, None)
                    else:
                        src = DesignSource(src)
                except IsADirectoryError as e:
                    raise ValueError(f"a source is a file, but {e.filename} is a directory") from e
                except FileNotFoundError as e:
                    raise ValueError(
                        f"source file does not exist: {e.filename} (a source that is generated "
                        "later is given as `{ path = ... }`)"
                    ) from e
            if not source_already_exists(src):
                sources.append(src)
        return sources


class Clock(XedaBaseModel):
    port: str
    name: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _name_validate(cls, value, info) -> Optional[str]:
        values = info.data if isinstance(info.data, dict) else {}
        return value or values.get("port", None)


class Generator(XedaBaseModel):
    cwd: Optional[str] = None
    executable: Optional[str] = None
    class_: Optional[str] = Field(None, alias="class")
    args: List[str] = Field(
        default_factory=list,
        description="The arguments the executable is run with: a list, or one string split on "
        "whitespace (as `command` is).",
    )
    command: Optional[str] = None
    check: bool = True
    env: Optional[Dict[str, str]] = None
    # sweepable parameters used in command
    parameters: dict = {}
    sources: List[Path] = Field(
        default_factory=list,
        description="The sources this generator reads, as a design's `sources` name them: a path "
        "relative to the design root, or a pattern where `*` is the only pattern character. Each "
        "one's content is what decides whether the generator runs again, so each has to exist. "
        "A directory counts as every file in it, so name what the generator reads from outside "
        "the design too (a library tree, an editable clone); xeda assumes nothing about the "
        "generator's language or environment.",
    )
    always_runs: bool = Field(
        default=False,
        description="Run this generator on every design load: what it reads cannot be judged "
        "(it is not files, or they cannot be listed), so xeda keeps no record of it. A "
        "generator that declares no `sources` runs on every load anyway.",
    )
    generated_sources: List[str] = Field(
        default_factory=list,
        description="The sources this generator produces, as a design's `sources` name them, "
        "when it produces only some of `rtl.sources` (or a directory holding some): what is "
        "judged against the record of its last generation. Each entry has to be one of "
        "`rtl.sources` or hold one. Empty, every one of `rtl.sources` is judged, so editing a "
        "source the generator does not write runs it again.",
    )

    @model_validator(mode="before")
    @classmethod
    def _removed_generator_fields(cls, data):
        """A removed field is named with what replaces it, as every removed setting is."""
        if "run_only_if_sources_modified" in data:
            # The old switch was on by default: written `true` it asks for what a generator is
            # now always judged by, so it is to be deleted, never turned into `always_runs`.
            if data["run_only_if_sources_modified"] is True:
                advice = (
                    "delete it (a generator is judged by the content of what it reads and "
                    "produces now, never by a modification time; `always_runs: true` would mean "
                    "the opposite)"
                )
            else:
                advice = (
                    "use `always_runs: true` (a generator is judged by the content of what it "
                    "reads and produces now, never by a modification time)"
                )
            raise ValueError(f"`run_only_if_sources_modified` was removed: {advice}")
        return data

    @field_validator("args", mode="before")
    @classmethod
    def _args_to_words(cls, value):
        """Store the arguments as words whichever way they were written: a string is split on
        whitespace, exactly as `command` is, and a list is the words already, so every reader
        (`execution_command`, the freshness identity) sees a list."""
        return value.split() if isinstance(value, str) else value

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value):
        """Expand source patterns and normalize the resulting paths."""
        # sources can contain globs which are expanded
        if isinstance(value, str):
            value = [value]
        sources: List[Path] = []
        for src in unique(value):
            if isinstance(src, str):
                if _is_source_pattern(src):
                    sources.extend(
                        Path(m).resolve()
                        for m in _expand_source_glob(src, Path.cwd(), "generator source")
                    )
                else:
                    sources.append(_expand_design_path(src, Path.cwd()).resolve())
            elif isinstance(src, Path):
                # Resolved like the string spelling, so a relative one does not keep depending
                # on the working directory it was given in.
                sources.append(src.resolve())
            else:
                raise ValueError(f"Invalid source type: {type(src)} for source '{src}'")
        # remove duplicates, but keep order
        sources = unique(sources)
        # The generator's inputs: whether to run it again is decided by their content
        # (`generation.generation_identity`), so each one has to be there to be read.
        missing = [str(src) for src in sources if not src.exists()]
        if missing:
            raise ValueError(f"generator source file does not exist: {', '.join(missing)}")
        return sources

    def execution_command(self, design_root: Optional[Path] = None) -> List[str]:
        """The argv this generator runs, shared by execution and freshness identity."""
        if self.command:
            return self.command.split()
        elif not self.executable:
            raise ValueError("executable is not set")
        else:
            return [self.executable, *self.args]

    def execution_executable_path(
        self, design_root: Optional[Path] = None, command: Optional[Sequence[str]] = None
    ) -> Path:
        """Resolve the direct executable as the child process will, without running it."""
        selected_command = (
            list(command) if command is not None else self.execution_command(design_root)
        )
        if not selected_command:
            raise ValueError("generator command is empty")
        executable = selected_command[0]
        if self.cwd is None:
            cwd = Path(design_root or Path.cwd()).resolve()
        else:
            cwd = Path(self.cwd)
            if not cwd.is_absolute():
                cwd = Path(design_root or Path.cwd()).resolve() / cwd
            cwd = cwd.resolve()
        env = self.env
        path = env.get("PATH") if env is not None else os.environ.get("PATH")
        if path is None:
            path = os.defpath
        if (
            os.path.isabs(executable)
            or os.sep in executable
            or (os.altsep and os.altsep in executable)
        ):
            candidate = Path(executable)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if candidate.is_file() and os.access(candidate, os.X_OK):
                # Preserve a selected symlink alias in argv[0]. Identity reads still follow
                # the link and hash the target's contents.
                return candidate
        else:
            search_path = os.pathsep.join(
                str((cwd / entry).resolve()) if not Path(entry).is_absolute() else entry
                for entry in path.split(os.pathsep)
            )
            found = shutil.which(executable, path=search_path)
            if found:
                return Path(found).absolute()
        raise ValueError(
            f"cannot identify generator executable `{executable}` from cwd `{cwd}` and PATH"
        )

    def run(self):
        self.run_cmd(self.execution_command())

    def run_cmd(self, cmd, check=None, stdout=None, stderr=None):
        cmd = list(cmd)
        executable = None
        if cmd:
            # Resolve exactly the executable used by freshness identity. This also makes child
            # cwd and PATH selection consistent on Windows, where Popen ignores both for lookup.
            # Pass it separately to preserve argv[0] and symlink-alias behavior for wrappers.
            executable = str(self.execution_executable_path(Path.cwd(), cmd))
        log.info("Running command: '%s'", " ".join(cmd))
        if stdout is None:
            # A generator's subprocess inherits our stdout, which would corrupt a `--json`
            # document. `tool_output_redirect()` is None unless output has been redirected, so
            # normal runs keep inheriting as before.
            stdout = tool_output_redirect()
        # Checked here, never by `subprocess.run`: a generator that fails names itself as the
        # other two spellings of one do (`NonZeroExitCode`), not as a raw `CalledProcessError`.
        wanted = self.check if check is None else check
        p = subprocess.run(
            cmd,
            cwd=self.cwd,
            executable=executable,
            check=False,
            stdout=stdout,
            stderr=stderr,
            env=self.env,
        )
        if wanted and p.returncode:
            raise NonZeroExitCode(cmd, p.returncode)
        return p

    @property
    def name(self) -> str:
        """The kind of generator (its class), never which one: not for display, see `describe`."""
        return str(self.__class__.__qualname__ or "generator")

    def describe(
        self, design_name: Optional[str] = None, design_root: Optional[Path] = None
    ) -> str:
        """Which generator this is, for a message: the design it belongs to and what it runs,
        `the generator of design 'hdmi_demo' (python3 hdmi_demo.py --build)`. A command that
        cannot be built yet (no executable, no Chisel project) leaves the kind of generator."""
        owner = f" of design '{design_name}'" if design_name else ""
        return f"the generator{owner} ({self.command_text(design_root)})"

    def command_text(self, design_root: Optional[Path] = None) -> str:
        """What this generator runs, for a message; its kind when that cannot be built yet."""
        try:
            return shlex.join(self.execution_command(design_root))
        except ValueError:
            return self.name


class ChiselGenerator(Generator):
    main: Optional[str] = None
    project: Optional[str] = None
    build_system: str = "mill"
    check: bool = True

    def command_text(self, design_root: Optional[Path] = None) -> str:
        if self.build_system == "bloop" and not self.project:
            return self.name
        return super().command_text(design_root)

    def run(self):
        if self.build_system == "mill":
            return self.run_mill()
        elif self.build_system == "bloop":
            return self.run_bloop()
        else:
            raise Exception(f"Unsupported build system: {self.build_system}")

    def execution_command(self, design_root: Optional[Path] = None) -> List[str]:
        if self.cwd is None:
            cwd = Path(design_root or Path.cwd()).resolve()
        else:
            cwd = Path(self.cwd)
            if not cwd.is_absolute():
                cwd = Path(design_root or Path.cwd()).resolve() / cwd
        if self.build_system == "mill":
            mill_exec = "./mill" if (cwd / "mill").exists() else "mill"
            cmd = [mill_exec]
            if not self.project:
                raise ValueError("`project` must be specified for Chisel generator")
            cmd += [f"{self.project}.runMain", self.main] if self.main else [f"{self.project}.run"]
            return [*cmd, *self.args]
        if self.build_system == "bloop":
            if not self.project:
                # `run_bloop` discovers the project by invoking `bloop projects`; the executable
                # whose content matters is still selected here without that side effect.
                base = ["bloop", "projects"]
                return base
            cmd = ["bloop", "run", self.project]
            if self.main:
                cmd += ["--main", self.main]
            if self.args:
                cmd += ["--", *self.args]
            return cmd
        raise ValueError(f"Unsupported build system: {self.build_system}")

    def run_mill(self):
        return self.run_cmd(self.execution_command())

    def run_bloop(self):
        if self.project is None:
            p = self.run_cmd(["bloop", "projects"], stdout=subprocess.PIPE)
            projects_str = p.stdout.decode()
            projects = projects_str.split()
            if projects:
                log.info(f"Found projects: {', '.join(projects)}")
                self.project = projects[0]
            else:
                log.error("No projects found!")
                raise ValueError("No projects found!")
        if not self.project:
            raise ValueError("`project` must be specified for Chisel generator")
        return self.run_cmd(self.execution_command())


class RtlSettings(DVSettings):
    """design.rtl"""

    top: Optional[str] = Field(
        None,
        description="Toplevel RTL module/entity",
    )
    # `SerializeAsAny`: a generator may be a `ChiselGenerator`, and v2 serializes a nested model
    # by its *annotated* type unless told otherwise. Declared on the field rather than asked for
    # by a blanket dump flag, which duck-types *every* value and so skips the serializer an
    # arbitrary type like `DesignSource` attaches to its own schema -- that is what made
    # `Design.model_dump_json()` raise `PydanticSerializationError` outright.
    generator: Union[str, List[str], SerializeAsAny[Generator], None] = None
    attributes: Dict[str, Dict[str, Any]] = Field(
        dict(),
        description="""
        attributes may include HDL attributes for modules, ports, etc, but their actual meaning and behavior is decided by the specific target flow
        Attributes should be specified as a mapping of attr_name->(path->attr_value), i.e.:
        - key: is the _name_ of the attribute
        - value is a mapping of path->attr_value, i.e.:
            - the key is the path or scope on which the attribute applies
            - value is the actual value of the attribute
        """,
    )
    # The only stored representation of design clock ports. ``clock`` and ``clock_port`` are
    # input/API compatibility shorthands exposed as derived properties below.
    clocks: List[Clock] = []

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Advertise the accepted single-clock shorthands without storing duplicate state."""
        schema = handler(core_schema)
        properties = schema.setdefault("properties", {})
        clock_item = deepcopy(properties["clocks"]["items"])
        properties["clock"] = {
            "anyOf": [clock_item, {"type": "string"}],
            "description": "Single design clock shorthand. Prefer an object with `port`.",
            "x-xeda-input-only": True,
        }
        properties["clock_port"] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "deprecated": True,
            "description": "Compatibility shorthand for `clock.port`; prefer `clock.port`.",
            "x-xeda-input-only": True,
        }
        return schema

    @model_validator(mode="before")
    @classmethod
    def rtl_settings_validate(cls, values, info):  # pylint: disable=no-self-argument
        """Normalize exactly one accepted clock input into the canonical ``clocks`` list."""
        if info.field_name is not None and info.data is None:
            # Assignment to a real field. The clocks field validator handles clocks itself;
            # unrelated assignments must not reconstruct or detach the established list.
            return values

        present = [name for name in ("clock", "clock_port", "clocks") if name in values]
        if len(present) > 1:
            raise ValueError(
                "Specify only one of `clock`, `clock_port`, or `clocks`; prefer `clock` for "
                "one clock and `clocks` for several"
            )
        if present:
            spelling = present[0]
            if spelling == "clocks":
                return values
            value = values.pop(spelling)
            if spelling == "clock_port":
                # Historically an empty compatibility string meant that the design had no
                # declared clock. Preserve that meaning instead of constructing a clock with an
                # unusable empty port.
                value = {"port": value} if value else None
            values["clocks"] = [value] if value is not None else []
        return values

    @field_validator("clocks", mode="before")
    @classmethod
    def _normalize_clocks(cls, clocks):
        if clocks is None:
            return []
        if isinstance(clocks, (str, dict, Clock)):
            clocks = [clocks]
        if not isinstance(clocks, list):
            raise ValueError(f"Expecting 'clocks' to be a list but found {clocks}")
        return [{"port": clock} if isinstance(clock, str) else clock for clock in clocks if clock]

    @property
    def clock(self) -> Optional[Clock]:
        """The first design clock, or ``None`` when the design has none."""
        return self.clocks[0] if self.clocks else None

    @clock.setter
    def clock(self, value: Clock | Dict[str, Any] | str | None) -> None:
        if value is None:
            self.clocks = []
        elif isinstance(value, Clock):
            self.clocks = [value.model_copy(deep=True)]
        elif isinstance(value, str):
            self.clocks = [Clock(port=value)] if value else []
        else:
            self.clocks = [Clock.model_validate(value)]

    @property
    def clock_port(self) -> Optional[str]:
        """Compatibility access to the first design clock's port, or ``None`` when there is none."""
        clock = self.clock
        return clock.port if clock is not None else None

    @clock_port.setter
    def clock_port(self, value: Optional[str]) -> None:
        """Assigning replaces `clocks` with the single-clock shorthand, same as the `clock` setter."""
        self.clock = value


class CocotbTestbench(XedaBaseModel):
    module: Optional[str] = None
    toplevel: Optional[str] = None
    testcase: List[str] = Field(
        default=[],
        description="List of test-cases for this design. Will be overridden by flow settings: cocotb.testcase",
    )


class TbSettings(DVSettings):
    """design.tb"""

    sources: List[DesignSource] = Field([], description=SOURCES_DESCRIPTION)
    top: Tuple012 = Field(
        tuple(),
        description="Toplevel testbench module(s), specified as a tuple of strings. In addition to the primary toplevel, a secondary toplevel module can also be specified.",
    )
    uut: Optional[str] = Field(
        None, description="instance name of the unit under test in the testbench"
    )
    cocotb: Optional[CocotbTestbench] = Field(
        None, description="testbench is based on cocotb framework"
    )

    @field_validator("top", mode="before")
    @classmethod
    def _tb_top_validate(cls, value) -> Tuple012:
        if value:
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (tuple, list, Sequence)):
                if len(value) > 2:
                    raise ValueError("At most 2 simulation top modules are supported.")
                return tuple(value)
        return tuple()

    @field_validator("cocotb", mode="before")
    @classmethod
    def _auto_set_cocotb(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        if value is False:
            return None

        def has_cocotb(tb):
            sources = tb.get("sources", [])
            for src in sources:
                if isinstance(src, DesignSource) and src.type == SourceType.Cocotb:
                    return True
                if isinstance(src, dict) and src.get("type") in ["cocotb", SourceType.Cocotb]:
                    return True
                if isinstance(src, str) and src.startswith("cocotb:") and src.endswith(".py"):
                    return True
            return False

        if value is True or (value is None and has_cocotb(values)):
            return CocotbTestbench()
        return value


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(
        None,
        description="Standard version",
        alias="version",
    )

    @field_validator("standard", mode="before")
    @classmethod
    def two_digit_standard(cls, value):
        if not value:
            return None
        if isinstance(value, int):
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError("standard should be of type string")
        return value

    @classmethod
    def from_version(cls, version: str | int):
        return cls(version=cls.two_digit_standard(version))  # type: ignore


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(XedaBaseModel):
    vhdl: VhdlSettings = VhdlSettings()  # type: ignore
    verilog: LanguageSettings = LanguageSettings()  # type: ignore

    @field_validator("verilog", "vhdl", mode="before")
    @classmethod
    def _language_settings(cls, value, info):
        if isinstance(value, (str, int)):
            if info.field_name == "vhdl":
                return VhdlSettings.from_version(value)
            elif info.field_name == "verilog":
                return LanguageSettings.from_version(value)
        return value


class RtlDep(XedaBaseModel):
    pos: int = 0


class TbDep(XedaBaseModel):
    pos: int = 0


#: Where a Git dependency without a `clone_dir` is cloned, under the run root.
DEPENDENCY_CLONES = ".dependencies"


@dataclass(frozen=True)
class LoadContext:
    """What a launcher lends the designs it loads: xeda's own space, and what the launch asked
    for. `run_root(True)` creates and marks the run root, `run_root(False)` gives one that is
    already there, or None: it is asked to create one only to keep something there -- never to
    look for it -- so a load that keeps nothing creates nothing, and a pure plan never asks.
    What a load keeps there: the directory a Git dependency without a `clone_dir` is cloned into
    (`DEPENDENCY_CLONES`) and the record of a generator's last generation
    (`generation.CACHE_DIRECTORY`). Both outlive a design that fails *after* them, as they
    should: the work they record was really done."""

    run_root: Callable[[bool], Optional[Path]]
    #: `--rebuild-all` (which `--clean` implies): a generator runs whatever its record says
    rebuild_all: bool = False


#: How a design load reaches xeda's own space, set by the launcher around loading its designs
#: (`loading_in_run_root`) and unset elsewhere.
load_context: ContextVar[Optional[LoadContext]] = ContextVar("load_context", default=None)

_planning_load: ContextVar[bool] = ContextVar("planning_load", default=False)


@contextmanager
def refusing_load_side_effects() -> Iterator[None]:
    """Refuse generation and Git fetching while loading a design for a pure plan."""
    token = _planning_load.set(True)
    try:
        yield
    finally:
        _planning_load.reset(token)


@contextmanager
def loading_in_run_root(
    provider: Callable[[bool], Optional[Path]], rebuild_all: bool = False
) -> Iterator[None]:
    """Let the designs loaded meanwhile keep what a load keeps in a run root -- a Git
    dependency's clone, a generator's record -- in the run root `provider` names, asking it only
    when something is kept there, and for one it may create only then. With `rebuild_all`, a
    generator runs whatever its record says, as every flow of the launch does."""
    token = load_context.set(LoadContext(provider, rebuild_all))
    try:
        yield
    finally:
        load_context.reset(token)


class DesignReference(XedaBaseModel):
    uri: str
    rtl: RtlDep = RtlDep()
    tb: TbDep = TbDep()
    #: where a git dependency is cloned (`<local_cache>/<host>/<path>`) when it names no
    #: `clone_dir`; unset, a launcher clones into its run root (`loading_in_run_root`).
    #: A directory the user configures here is theirs to direct: xeda clones and pulls there.
    local_cache: Optional[Path] = None

    @staticmethod
    def from_data(data) -> DesignReference:
        if isinstance(data, DesignReference):
            return data
        if isinstance(data, str):
            data = dict(uri=data)
        elif isinstance(data, Mapping):
            # The URI form is normalized below by removing the ``git+`` discriminator. Keep
            # that normalization local: dependency dictionaries commonly come straight from
            # the caller's parsed design document, and must not be rewritten as a side effect
            # of constructing a model.
            data = dict(data)
        else:
            raise ValueError(
                "a design dependency must be a URI string, mapping, or DesignReference, "
                f"got {type(data).__name__}"
            )
        if "uri" in data:
            uri_str = data["uri"]
            GIT_PREFIX = "git+"
            if isinstance(uri_str, str) and uri_str.startswith(GIT_PREFIX):
                uri_str = uri_str[len(GIT_PREFIX) :]
                data["uri"] = uri_str
                return GitReference(**data)  # type: ignore
        if "repo_url" in data:
            return GitReference(**data)  # type: ignore
        return DesignReference(**data)  # type: ignore

    def fetch_design(self) -> Design:
        design_path = Path(self.uri)
        if not design_path.exists():
            raise ValueError(f"file {design_path} does not exist!")
        return Design.from_file(design_path)


def _clone_name_text(what: str, text: str) -> str:
    """Refuse text that would name a directory outside the place it is joined onto."""
    if not isinstance(text, str):
        raise ValueError(f"the Git {what} must be a string, not {text!r}")
    if "\\" in text or "\0" in text or any(part in (".", "..") for part in text.split("/")):
        raise ValueError(
            f"the Git {what} {text!r} has a `.` or `..` component, a backslash or a NUL: "
            "it would name a directory outside the clone cache"
        )
    return text


def clone_name_parts(
    repo_url: str, commit: Optional[str], branch: Optional[str]
) -> tuple[str, str]:
    """The host and the relative path (below the host) a Git reference is cloned into.

    The repository's host, path, commit and branch name directories in the clone cache, so none
    may carry a `.` or `..` component or a backslash, and the host and the path may not be empty.
    A branch may hold `/` (`release/1.0`): it then names nested directories, all inside the cache.
    """
    uri = urlparse(repo_url)
    if not uri.netloc:
        raise ValueError(f"invalid URL: {uri}")
    host = _clone_name_text("host", uri.netloc)
    path = _clone_name_text("repository path", uri.path.lstrip("/"))
    if not path:
        raise ValueError(f"the Git URL {repo_url!r} names no repository path")
    if commit:
        path += "_commit=" + _clone_name_text("commit", commit)
    elif branch:
        path += "_" + _clone_name_text("branch", branch)
    return host, path


def clone_location(
    cache: Union[str, Path],
    repo_url: str,
    commit: Optional[str],
    branch: Optional[str],
    *,
    owner: Optional[RunDirectory] = None,
) -> Path:
    """Where a Git reference is cloned: `<cache>/<host>/<path>`, inside `cache`.

    A cache under a run root is named through its `owner` (the run root as a `RunDirectory`) by
    `RunDirectory.unlinked`, the one rule every cache there follows: inside the run root, and
    reached through no symbolic link. The names themselves (`clone_name_parts`) cannot leave the
    cache. A cache the user named (`local_cache`) is theirs to direct, so only the names are
    checked against it.
    """
    host, path = clone_name_parts(repo_url, commit, branch)
    location = Path(cache) / host / path
    if owner is not None:
        return owner.unlinked(location)
    base = os.path.abspath(cache)
    inside = os.path.abspath(location)
    if inside == base or os.path.commonpath([base, inside]) != base:
        raise ValueError(
            f"{repo_url} would be cloned to {location}, outside the clone cache {cache}"
        )
    return location


class GitReference(DesignReference):
    """
    uri: [https,git,...]://<hostname>[:port]/path/to/repo.git[?[branch=mybranch],[commit=mycommit]]#path/to/design_file.toml
    example:
        https://github.com/GMUCERG/TinyJAMBU-SCA.git?branch=dev#./TinyJAMBU-DOM1-v1.toml
    """

    repo_url: str
    design_file: str
    commit: Optional[str] = None
    branch: Optional[str] = None
    clone_dir: Optional[Path] = None

    @field_validator("clone_dir", mode="before")
    @classmethod
    def validate_clone_dir(cls, value, info):
        values = info.data if isinstance(info.data, dict) else {}
        repo_url = values.get("repo_url")
        if not value and repo_url:
            local_cache = values.get("local_cache")
            if local_cache:
                return clone_location(
                    local_cache, repo_url, values.get("commit"), values.get("branch")
                )
        return value

    @model_validator(mode="before")
    @classmethod
    def validate_repo(cls, values):
        repo_url = None
        design_file_path = None
        branch = None
        commit = None
        if "uri" in values:
            uri_str = values["uri"]
            if not isinstance(uri_str, str):
                raise ValueError("a git dependency URI must be a string")
            # <scheme>://<netloc>/<path>;<params>?<query>#<fragment>
            uri = urlparse(uri_str)
            if not uri.scheme or not uri.netloc:
                raise ValueError(f"invalid git URL: {uri}")
            # git design file path should be relative to root
            design_file_path = uri.fragment.lstrip("/.")  # Removes /, ../, etc.
            if not design_file_path:
                raise ValueError(
                    inspect.cleandoc(
                        """path to design_file must be specified using URL fragment (#...), e.g.,
                    https://github.com/SOME_USERNAME/SOME_REPOSITORY.git#PATH_TO_DESIGN_FILE when design file is
                    'sub_dir1/design_file2.toml' relative to the the repository's root."""
                    )
                )
            if uri.query:
                query = parse_qs(uri.query)
                br = query.get("branch")
                if br:
                    branch = br[-1]
                cmt = query.get("commit")
                if cmt:
                    commit = cmt[-1]  # last arg
            repo_url = uri._replace(fragment="", query="").geturl()

        # Explicit keys first, then the caller's own values win -- `dict(repo_url=..., **values)`
        # raised `TypeError: got multiple values for keyword argument 'repo_url'` for exactly the
        # mapping form `DesignReference.from_data()` selects when it sees a `repo_url` key.
        derived = {
            "repo_url": repo_url,
            "design_file": design_file_path,
            "branch": branch,
            "commit": commit,
        }
        merged = {k: v for k, v in derived.items() if v is not None}
        result = {**derived, **merged, **values}
        if isinstance(result.get("repo_url"), str) and not result.get("clone_dir"):
            # Names are checked where they name a directory: a `clone_dir` is used as given,
            # and then nothing is named from the URL, the branch or the commit. A branch or a
            # commit that is not text is the field's own error, reported at the field.
            named = [
                value if isinstance(value, str) else None
                for value in (result.get("commit"), result.get("branch"))
            ]
            clone_name_parts(result["repo_url"], *named)
        if not result.get("uri"):
            # The mapping form (`{repo_url = ..., design_file = ...}`) that
            # `DesignReference.from_data()` routes here carries no `uri`, but the base class
            # requires one. Reconstruct it so both spellings describe the same reference.
            base = result.get("repo_url")
            if not base:
                raise ValueError("a git dependency needs either 'uri' or 'repo_url'")
            uri_query = "&".join(
                f"{k}={result[k]}" for k in ("branch", "commit") if result.get(k) is not None
            )
            uri_fragment = result.get("design_file") or ""
            result["uri"] = "{}{}{}".format(
                base,
                f"?{uri_query}" if uri_query else "",
                f"#{uri_fragment}" if uri_fragment else "",
            )
        return result

    def fetch_design(self) -> Design:
        import git
        from git.repo import Repo

        if _planning_load.get():
            raise ValueError("Cannot plan a design that needs a Git dependency fetch")
        clone_dir = self.clone_dir
        if clone_dir is None:
            cache = self.local_cache
            owner = None
            if cache is None:
                context = load_context.get()
                run_root = context.run_root(True) if context is not None else None
                if run_root is not None:
                    cache = run_root / DEPENDENCY_CLONES
                    owner = RunDirectory(run_root, run_root)
            if cache is None:
                raise ValueError(
                    f"{self.repo_url} needs a directory to be cloned into: give its `clone_dir` "
                    "(or `local_cache`), or load the design through xeda run / a launcher, which "
                    "clones into its run root"
                )
            clone_dir = clone_location(cache, self.repo_url, self.commit, self.branch, owner=owner)
        repo = None
        if clone_dir.exists():
            try:
                repo = Repo(clone_dir)
                if not repo.git_dir:
                    raise ValueError(f"repo={repo} is missing 'git_dir'")
                log.info("Updating existing git repository at %s", clone_dir)
                repo.remotes.origin.fetch()
                if not self.commit:
                    repo.git.pull()
            except git.InvalidGitRepositoryError:
                log.error("Path %s is not a valid git repository.", clone_dir)
        if repo is None:
            log.info(
                "Cloning git repository url:%s branch:%s commit:%s",
                self.repo_url,
                self.branch,
                self.commit,
            )
            repo = Repo.clone_from(
                self.repo_url,
                clone_dir,
                depth=1,
                branch=self.branch,
            )
        if repo is None:
            raise ValueError("repo is None!")
        if self.commit:
            log.info("Checking out commit: %s", self.commit)
            repo.git.checkout(self.commit)
        elif self.branch:
            log.info("Checking out branch: %s", self.branch)
            repo.git.checkout(self.branch)

        design_path = clone_dir / self.design_file
        return Design.from_file(design_path)


DesignType = TypeVar("DesignType", bound="Design")

#: The top-level shorthands `Design.process_compatibility` folds into `rtl`: a design file (and
#: a target) may write them at its root.
FLAT_RTL_KEYS = (
    "sources",
    "top",
    "clock",
    "clock_port",
    "clocks",
    "parameters",
    "generics",
    "defines",
    "generator",
)

#: Keys a target may not hold: they belong to the design itself.
TARGET_FORBIDDEN_KEYS = frozenset({"name", "targets", "target", "design_root"})
# Settings shared by the flows of a graph (`board`, `fpga`, ...): written once, they would apply to
# every flow that declares them. A target takes them only under its own `flows.<flow>`, for now.
TARGET_FLOW_SETTING_KEYS = frozenset({"board", "fpga", "custom_boards_file"})

_NO_SINGULAR_TARGET = (
    "`target` is not a key of a design: targets are written as `targets.<name>`, and one is "
    "selected with `--target NAME` (`target=` in the API)"
)


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _spell_as(base: dict[str, Any], overlay: dict[str, Any], model: type[XedaBaseModel]) -> None:
    """Rename `overlay`'s keys to the spelling `base` uses for the same field of `model`
    (`flow`/`flows`, `parameters`/`generics`), so the two meet when merged."""
    names = input_names(model)
    spelled = {names[key]: key for key in base if key in names}
    for key in list(overlay):
        ours = spelled.get(names.get(key, ""), key)
        if ours != key and ours not in overlay:
            overlay[ours] = overlay.pop(key)


def target_name_problem(name: Any) -> str | None:
    """Why `name` cannot name a target, or None. A target's run directories are
    `<run root>/<design>/<name>/<flow>`, so it is a name (as a design's is) that is no flow's: the
    flows' own directories lie beside it. The loader and the launcher's path boundary
    (`DefaultRunner.run_path_of`) judge a name by this one rule."""
    if not isinstance(name, str) or not DESIGN_NAME.fullmatch(name):
        return (
            f"{name!r} is not a target name: it names the target's run directories, so it "
            "starts with a letter and holds only letters, digits, `_` and `-`"
        )
    if _names_a_flow(name):
        return (
            f"{name!r} is the name of a flow, and a target's run directories lie beside "
            "the flows' own: give the target another name"
        )
    return None


def _names_a_flow(name: str) -> bool:
    """Whether `name` is a registered flow's name or alias, or a removed flow's
    (`settings_layers.REMOVED_FLOWS`), as the command line would take it (dashes for
    underscores, any letter case)."""
    # Imported here, and by name: the flows import this module, and are registered by being
    # imported.
    importlib.import_module("xeda.flows")
    registered_flows = importlib.import_module("xeda.flow").registered_flows

    folded = name.replace("-", "_").lower()
    # a removed flow's name is a flow's too: its run directories may still be under `<design>/`
    removed = importlib.import_module("xeda.flow_runner.settings_layers").REMOVED_FLOWS
    return folded in removed or any(flow.lower() == folded for flow in registered_flows)


class Design(XedaBaseModel):
    name: str = Field(
        description="Unique name for the design, which should consist of letters, numbers, underscore(_), and dash(-). Name regex: [a-zA-Z][a-zA-Z0-9_\\-]*."
    )

    design_root: Optional[Path] = Field(None, json_schema_extra={"hidden_from_schema": True})
    description: Optional[str] = Field(None, description="A brief description of the design.")
    authors: List[str] = Field(
        [],
        alias="author",
        description="""List of authors/developers in "Name <email>" format ('mailbox' format, RFC 5322), e.g. ["Jane Doe <jane@example.com>", "John Doe <john@example.com>"]""",
    )
    # `SerializeAsAny`: a dependency is usually a `GitReference`, and v2 serializes a nested
    # model by its *annotated* type unless told otherwise -- which would silently drop
    # `repo_url`, `design_file`, `branch`/`commit` and the clone settings that
    # `send_design()` relies on.
    dependencies: List[SerializeAsAny[DesignReference]] = []
    rtl: RtlSettings
    tb: TbSettings = TbSettings()  # type: ignore
    language: Language = Field(
        Language(),
        alias="hdl",
        description="HDL language settings",
    )
    flow: Dict[str, Dict[str, Any]] = Field(
        dict(),
        alias="flows",
        description="Design-specific flow settings. The keys are the name of the flow and values are design-specific overrides for that flow.",
    )
    license: Union[str, List[str], None] = None
    version: Optional[str] = None
    url: Optional[str] = None
    target: str | None = Field(
        None,
        description="Name of the target this design was selected as (`targets.<name>` in its "
        "design file), recorded by the loader for documents to report. It is not part of the "
        "design's identity, and a design file does not write it.",
        json_schema_extra={"hidden_from_schema": True},
    )

    @field_validator("name", mode="after")
    @classmethod
    def _name_is_a_directory_name(cls, value: str) -> str:
        if not DESIGN_NAME.fullmatch(value):
            raise ValueError(
                f"{value!r} is not a design name: it names the design's run directories, so it "
                "starts with a letter and holds only letters, digits, `_` and `-` (set `name` in "
                "the design file)"
            )
        return value

    @field_validator("flow", mode="before")
    @classmethod
    def _flow_settings(cls, value):
        if value:
            value = settings_to_dict(value)
            value = {k: v for k, v in value.items() if v is not None}
        else:
            value = {}
        return value

    @field_validator("dependencies", mode="before")
    @classmethod
    def _dependencies_from_str(cls, value):
        if value and isinstance(value, list):
            value = [DesignReference.from_data(v) for v in value]
        return value

    @field_validator("authors", mode="before")
    @classmethod
    def _authors_from_str(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    @classmethod
    def process_compatibility(cls, data: dict[str, Any], defaults: bool = True) -> dict[str, Any]:
        """Fold the flat top-level form into `rtl` and `tb`. A target's overlay is folded by this
        very function, with `defaults=False`: only the keys it wrote, so it overrides no more
        than it says."""
        if "rtl" not in data:
            given = {name: data.pop(name) for name in FLAT_RTL_KEYS if name in data}
            # Multiple spellings written together (`parameters` and `generics`, the clock
            # inputs) are all kept: the corresponding model validator then reports the
            # ambiguity instead of silently choosing one.
            if defaults:
                data["rtl"] = {
                    "sources": [],
                    "generator": None,
                    "defines": {},
                    "top": None,
                    **given,
                }
            elif given:
                data["rtl"] = given
        tb = data.get("tb", {})
        tests = data.pop("tests", [])
        if tests and not isinstance(tests, list):
            tests = [tests]
        test = data.pop("test", None)
        if test:
            tests.append(test)
        if tests and not tb:
            # TODO add support for multiple tests per design
            test = tests[0]
            if not isinstance(test, dict):
                raise ValueError(f"test: {test} is not a dictionary")
            data["tb"] = test
        return data

    @classmethod
    def select_target(cls, data: Mapping[str, Any], target: str | None = None) -> dict[str, Any]:
        """The design `data` describes once one of its `targets` is selected: an ordinary
        design description, with the target's name recorded as `target`.

        A target is an overlay: it takes the keys of the design file itself (all but
        `TARGET_FORBIDDEN_KEYS`), folded as the design's own are (`process_compatibility`) and
        merged over them. Mappings merge key by key at every depth; `rtl.sources` and
        `tb.sources` are appended after the design's; any other list replaces the design's.

        With one target and no `target` given, that target is selected. Without `targets`, the
        data is returned as it is, and naming a `target` is an error. A design cannot write
        `target`, and an explicit null target table is invalid. Every problem is a
        `DesignValidationError` located at the offending key.
        """
        data = dict(data)

        def invalid(loc: str, msg: str) -> DesignValidationError:
            return DesignValidationError([(loc, msg, "", "value_error")], data=data)

        if "target" in data:
            raise invalid("target", _NO_SINGULAR_TARGET)

        if "targets" in data and data["targets"] is None:
            raise invalid(
                "targets", "`targets` must be a table of named targets (`targets.<name>`)"
            )
        if "targets" not in data or data["targets"] == {}:
            data.pop("targets", None)
            if target is not None:
                raise invalid(
                    "targets",
                    f"target {target!r} was asked for, but the design has no `targets`",
                )
            return data
        targets = data.pop("targets")
        if not isinstance(targets, Mapping):
            raise invalid(
                "targets",
                f"`targets` is a table of named targets (`targets.<name>`), got {type(targets).__name__}",
            )
        overlays = {name: cls._target_overlay(name, overlay) for name, overlay in targets.items()}
        folded: dict[str, str] = {}
        for name in overlays:
            other = folded.setdefault(name.casefold(), name)
            if other != name:
                raise invalid(
                    "targets",
                    f"the targets {other!r} and {name!r} differ only in letter case: their run "
                    "directories would be one on a file system that ignores it",
                )
        names = ", ".join(overlays)
        if target is None:
            if len(overlays) > 1:
                raise invalid(
                    "targets",
                    f"the design has several targets ({names}): select one with `--target NAME` "
                    "(`target=` in the API)",
                )
            target = next(iter(overlays))
        elif target not in overlays:
            close = difflib.get_close_matches(str(target), list(overlays), n=1)
            raise invalid(
                "targets",
                f"{target!r} is not a target of the design"
                + (f" (did you mean {close[0]!r}?)" if close else "")
                + f". Its targets are: {names}",
            )
        overlay = overlays[target]
        try:
            base = cls.process_compatibility(deepcopy(data))
        except ValueError as e:
            raise invalid("", str(e)) from e
        for part, model in (("rtl", RtlSettings), ("tb", TbSettings)):
            ours, theirs = base.get(part), overlay.get(part)
            if isinstance(ours, dict) and isinstance(theirs, dict):
                _spell_as(ours, theirs, model)
                if "sources" in theirs:
                    theirs["sources"] = [
                        *_as_list(ours.get("sources", [])),
                        *_as_list(theirs["sources"]),
                    ]
        _spell_as(base, overlay, cls)
        return {**hierarchical_merge(base, overlay), "target": target}

    @classmethod
    def _target_overlay(cls, name: Any, overlay: Any) -> dict[str, Any]:
        """One target's overlay, checked and folded as a design's own top level is."""
        loc = f"targets.{name}"

        def invalid(key: str | None, msg: str) -> DesignValidationError:
            return DesignValidationError([(f"{loc}.{key}" if key else loc, msg, "", "value_error")])

        problem = target_name_problem(name)
        if problem is not None:
            raise invalid(None, problem)
        if not isinstance(overlay, Mapping):
            raise invalid(
                None,
                "a target is a table of the design's own keys (`sources`, `defines`, `rtl`, "
                f"`tb`, `flows`, ...), got {type(overlay).__name__}",
            )
        overlay = expand_hierarchy(dict(overlay))
        known = {*input_names(cls), *FLAT_RTL_KEYS, "test", "tests"} - TARGET_FORBIDDEN_KEYS
        for key in overlay:
            if key in TARGET_FORBIDDEN_KEYS:
                raise invalid(
                    key,
                    f"`{key}` is not allowed in a target: it belongs to the design itself"
                    + (" (a target has no targets of its own)" if key == "targets" else ""),
                )
            if key not in known and cls.model_config.get("extra") != "allow":
                close = difflib.get_close_matches(str(key), sorted(known), n=1)
                if key in TARGET_FLOW_SETTING_KEYS:
                    raise invalid(
                        key,
                        f"`{key}` is a setting of a flow, and not supported at a target's top "
                        f"level (yet): write it under the target's flows, as `flows.<flow>.{key}`",
                    )
                raise invalid(
                    key,
                    "not a key of a design, so not of a target either"
                    + (f" (did you mean `{close[0]}`?)" if close else ""),
                )
        try:
            return cls.process_compatibility(overlay, defaults=False)
        except ValueError as e:
            raise invalid(None, str(e)) from e

    @classmethod
    def process_generation(cls, data: Dict[str, Any]):
        """Run declared generators and collect their produced sources."""
        design_root = data.get("design_root")
        if not design_root:
            design_root = Path.cwd()
        else:
            design_root = Path(design_root)
        # Normalize before changing cwd: direct Design constructors accept relative roots too.
        # Resolving inside the lease after WorkingDirectory would rebase it a second time.
        design_root = design_root.resolve()
        rtl = data.get("rtl", {})
        assert isinstance(rtl, dict), f"rtl must be a dictionary, but found {type(rtl)}"
        generator = rtl.pop("generator", None)
        if generator:
            if _planning_load.get():
                generator_lease: AbstractContextManager[None] = nullcontext()
            else:
                from .flow_runner.run_lock import generator_design_lock

                generator_lease = generator_design_lock(design_root)
            with WorkingDirectory(design_root), generator_lease:
                # A generator is told the design root as `$DESIGN_ROOT` names it in the design's
                # own paths: replacing whatever the shell exports, which is another directory's.
                env = {**os.environ, "DESIGN_ROOT": str(design_root)}
                if isinstance(generator, str):
                    if _planning_load.get():
                        raise ValueError("Cannot plan a design that needs a generator")
                    log.info("Running generator command: %s", generator)
                    exit_code = subprocess.call(
                        generator,
                        shell=True,
                        cwd=design_root,
                        env=env,
                        stdout=tool_output_redirect(),
                    )
                    if exit_code != 0:
                        log.error("Generator '%s' failed with exit code %d", generator, exit_code)
                        raise NonZeroExitCode(generator, exit_code)
                elif isinstance(generator, (dict, Generator)):
                    if isinstance(generator, (dict)):
                        clazz = generator.get("class") or generator.get("use")
                        if clazz:
                            if not isinstance(clazz, str):
                                raise ValueError(f"class={clazz} must be a string")
                            if clazz.lower() == "chisel":
                                generator = ChiselGenerator(**generator)
                            else:
                                raise Exception(f"unknown generator class: {clazz}")
                        else:
                            generator = Generator(**generator)

                    # Judged before the defaults below complete it: its identity is the
                    # configuration the design states, never this shell's environment.
                    def generated() -> Optional[List[Path]]:
                        declared = generator.generated_sources or rtl.get("sources", [])
                        return _source_paths_as_given(declared, design_root)

                    planning = _planning_load.get()
                    context = load_context.get()
                    design_name = data.get("name")
                    description = generator.describe(
                        design_name if isinstance(design_name, str) else None, design_root
                    )
                    with judging_generation(
                        generator,
                        design_root,
                        generated,
                        description=description,
                        run_root=context.run_root if context else None,
                        planning=planning,
                        rebuild_all=bool(context and context.rebuild_all),
                    ) as generation:
                        if generation.reason is None:
                            log.info(
                                "Not running %s: its generated sources are what its "
                                "last generation left",
                                description,
                            )
                        else:
                            if planning:
                                raise ValueError("Cannot plan a design that needs a generator")
                            if generator.cwd is None:
                                generator.cwd = str(design_root)
                            if generator.env is None:
                                generator.env = dict(env)
                            else:
                                # An `env` the design states is the generator's whole
                                # environment, and a `DESIGN_ROOT` in it is the design's own word.
                                generator.env.setdefault("DESIGN_ROOT", str(design_root))
                            log.info("Running %s: %s", description, generation.reason)
                            generator.run()
                            generation.produced()
                else:
                    if _planning_load.get():
                        raise ValueError("Cannot plan a design that needs a generator")
                    args = generator
                    # gen_script = Path(args[0])
                    # extension = gen_script.suffix
                    # if extension == ".py":
                    #     if not gen_script.exists():
                    #         log.critical("Generator script not found: %s", gen_script)
                    #         raise FileNotFoundError(gen_script)
                    #     args.insert(0, sys.executable)
                    subprocess.run(
                        args,
                        check=True,
                        cwd=design_root,
                        env=env,
                        stdout=tool_output_redirect(),
                    )
                # Whatever the design declares as a source has to be there now. Expanded again
                # rather than reused from the skip check above, because the generator is exactly
                # what a glob was waiting for. A source written `{ path = ... }` skips the check
                # the sources validator does, so without this a generator that produced nothing
                # surfaced much later and much worse: an errno from inside the design hash.
                produced = _source_paths_as_given(rtl.get("sources", []), design_root)
                missing = [str(src) for src in produced or [] if not src.exists()]
                if missing:
                    raise ValueError(
                        f"generator ({_describe_generator(generator)}) did not produce "
                        f"{'sources' if len(missing) > 1 else 'the source'} the design declares: "
                        + ", ".join(missing)
                    )
                if (
                    isinstance(generator, Generator)
                    and generator.generated_sources
                    and produced is not None
                ):
                    # Checked here, where the tree is complete: a pattern of `rtl.sources` the
                    # generator is still to fill matches nothing before it has run.
                    declared = [os.path.normpath(src) for src in produced]
                    claimed = _source_paths_as_given(generator.generated_sources, design_root)
                    # An entry is one of them, or a directory holding one (a build directory).
                    outside = [
                        str(src)
                        for src in claimed or []
                        if not any(
                            name == os.path.normpath(src)
                            or name.startswith(os.path.normpath(src) + os.sep)
                            for name in declared
                        )
                    ]
                    if outside:
                        raise ValueError(
                            f"generator ({_describe_generator(generator)}) declares "
                            "`generated_sources` that are not among `rtl.sources`: "
                            + ", ".join(outside)
                        )

    @classmethod
    def process_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        data = cls.process_compatibility(data)
        log.debug("Design data: %s", data)
        cls.process_generation(data)
        return data

    def __init__(
        self,
        design_root: Union[str, os.PathLike, None] = None,
        **data: Any,
    ) -> None:
        # Compatibility processing, generators, and source normalization all consume or enrich
        # nested mappings. A caller's design description is input, not workspace owned by Xeda.
        """Copy caller data before compatibility and source processing."""
        data = deepcopy(data)
        if not design_root:
            design_root = data.pop("design_root", Path.cwd())
        if not design_root:
            raise ValueError("design_root is not set")

        # Everything up to `super().__init__` runs outside pydantic's validators, so a malformed
        # design has to be turned into a `DesignValidationError` here by hand -- otherwise
        # `rtl = ["x"]` or a mistyped `design_root` escaped as a bare AssertionError,
        # FileNotFoundError or TypeError traceback instead of naming the offending key.
        def invalid(loc: Optional[str], msg: str) -> DesignValidationError:
            return DesignValidationError(
                [(loc, msg, "", "value_error")], data=data, design_root=design_root
            )

        try:
            design_root = Path(design_root).resolve()
        except TypeError:
            raise invalid("design_root", f"not a path: {design_root!r}") from None
        if not design_root.is_dir():
            raise invalid("design_root", f"directory does not exist: {design_root}")
        if not data.get("design_root"):
            data["design_root"] = design_root
        if "targets" in data:
            data = self.select_target(data)
        try:
            data = Design.process_dict(data)
        except (ValueError, TypeError, AssertionError) as e:
            raise invalid(None, str(e)) from e
        with WorkingDirectory(design_root):
            try:
                super().__init__(**data)
            except ValidationError as e:
                raise DesignValidationError(
                    validation_errors(e.errors()), data=data, design_root=design_root  # type: ignore
                ) from e

            for dep in self.dependencies:
                try:
                    dep_design = dep.fetch_design()
                except ValueError as e:
                    raise invalid("dependencies", str(e)) from e
                log.info("adding dependency sources from %s", dep_design.name)
                pos = dep.rtl.pos
                sources = list(self.rtl.sources)
                if pos == -1:  # -1 means append 'after' the last element
                    sources.extend(dep_design.rtl.sources)
                    if not self.rtl.top and dep_design.rtl.top:
                        self.rtl.top = dep_design.rtl.top
                    if not self.rtl.parameters and dep_design.rtl.parameters:
                        self.rtl.parameters = dep_design.rtl.parameters
                    if not self.rtl.clocks and dep_design.rtl.clocks:
                        self.rtl.clocks = dep_design.rtl.clocks
                else:
                    if pos < 0:
                        pos += 1  # afterwards: pos=-2 means the position 'before' the last element
                    sources[pos:pos] = dep_design.rtl.sources
                # Assigned, not edited in place, so the merge is validated like sources given
                # any other way: a file the design and the dependency both list is compiled once.
                self.rtl.sources = sources
                if not self.tb.sources and dep_design.tb.sources:
                    self.tb.sources = dep_design.tb.sources
                if not self.tb.top and dep_design.tb.top:
                    self.tb.top = dep_design.tb.top
            # Merged is consumed: the design now holds its dependencies' sources itself, and it
            # is recorded as built. A record that kept them as well merged them again on every
            # reload (a `settings.json`, the archive `send_design` ships), duplicating sources.
            if self.dependencies:
                self.dependencies = []

    def header_dirs(self, rtl: bool = True, tb: bool = False) -> List[Path]:
        """The directory of every Verilog/SystemVerilog header the design lists, once each, in
        source order: the include search path (`-I`, `+incdir+`) a tool needs to find them."""
        headers = self.sources_of_type(
            SourceType.VerilogHeader, SourceType.SVHeader, rtl=rtl, tb=tb
        )
        return unique([src.path.parent for src in headers])

    def sources_of_type(
        self, *source_types: Union[str, SourceType], rtl=True, tb=False
    ) -> List[DesignSource]:
        source_types_str = [str(st).lower() for st in source_types]
        sources = []
        if rtl:
            sources.extend(self.rtl.sources)
        if tb and self.tb:
            sources.extend([src for src in self.tb.sources if src not in self.rtl.sources])
        if len(source_types) == 1 and isinstance(source_types[0], str) and source_types[0] == "*":
            return sources
        return [src for src in sources if str(src.type).lower() in source_types_str]

    def sim_sources_of_type(self, *source_types: Union[str, SourceType]) -> List[DesignSource]:
        if not self.tb:
            return []
        return self.sources_of_type(*source_types, rtl=True, tb=True)

    @property
    def sim_sources(self) -> List[DesignSource]:
        return self.sim_sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl
        )

    @property
    def sim_tops(self) -> Tuple012:
        if self.tb:
            if self.tb.cocotb and self.rtl.top:
                return (self.rtl.top,)
            if self.tb.top is not None:
                return self.tb.top
        return tuple()

    @property
    def root_path(self) -> Path:
        if not self.design_root:
            raise ValueError("design_root is not set")
        return self.design_root

    @classmethod
    def from_file(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[str, os.PathLike, None] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
        target: str | None = None,
    ) -> DesignType:
        """Load and validate a design description from a TOML, JSON or YAML file.

        `target` selects one of the design's `targets` (`select_target`); a design with one
        target needs none. `overrides` are applied last, over the selected target.

        A file that cannot be read as a design raises `DesignFileParseError`, and one whose
        design does not validate `DesignValidationError`; both name the file.
        """
        if overrides is None:
            overrides = {}
        if remove_extra is None:
            remove_extra = []
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        design_dict = _read_design_file(design_file)
        design_dict = expand_hierarchy(design_dict)
        if allow_extra:
            cls = model_with_allow_extra(cls)
        try:
            design_dict = cls.target_selected(design_dict, target, overrides)
            if "name" not in design_dict:
                design_name = design_file.stem
                design_name = removesuffix(design_name, ".xeda")
                log.debug(
                    "'design.name' not specified! Inferring design name: `%s` from design file name.",
                    design_name,
                )
                design_dict["name"] = design_name
            if not allow_extra:
                for k in remove_extra:
                    design_dict.pop(k, None)
            # Default value for design_root is the folder containing the design description file.
            dr = design_dict.pop("design_root", None)
            if design_root is None:
                design_root = dr
            if design_root is None:
                design_root = design_file.parent
            return cls(design_root=design_root, **design_dict)
        except DesignValidationError as e:
            raise DesignValidationError(  # add design_file to the emitted exception
                e.errors,
                data=e.data,
                design_root=e.design_root,
                design_name=e.design_name,
                file=str(design_file.absolute()),
            ) from e
        except Exception as e:
            log.error("Error processing design file: %s", design_file.absolute())
            raise e

    @classmethod
    def target_selected(
        cls,
        data: Mapping[str, Any],
        target: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A design description as written in a file (a design file, or a project's `designs`
        entry), with its target selected and then `overrides` applied: what every loader of
        written designs builds the `Design` from. Written input has no `target` key: the
        loader records the name there."""
        selected = cls.select_target(data, target)
        overrides = deepcopy(dict(overrides or {}))
        if "target" in overrides:
            raise DesignValidationError(
                [
                    (
                        "target",
                        "`target` is recorded by selection and cannot be overridden",
                        "",
                        "value_error",
                    )
                ],
                data=dict(data),
            )
        if selected.get("target") is not None:
            # the selected design is folded, so the flat form of an override is folded too
            overrides = cls.process_compatibility(overrides, defaults=False)
        return hierarchical_merge(selected, overrides)

    def source_path_as_named(self, src: FileResource) -> Path:
        """`src` as this design names it: relative to the design root when it is under it,
        otherwise the path the design file wrote."""
        if src.file and self.design_root:
            try:
                return src.file.absolute().relative_to(self.root_path.absolute())
            except ValueError:
                pass
        return src.get_specified_path()

    def source_artifact_name(self, src: FileResource, suffix: str = "") -> str:
        """A filename-safe name for something a flow writes *about* one source.

        Sources are distinct files, so their paths already tell them apart; a flow that writes
        one artifact per source -- GHDL converting each VHDL file to its own Verilog file -- has
        to fold that path into a single filename, and a stem alone does not: `rtl/a/fifo.vhd`
        and `rtl/b/fifo.vhd` would both want to be `fifo.v`, and one would overwrite the other.

        Folding a path into one filename cannot be both readable and injective: `my-fifo.vhd`
        and `my_fifo.vhd`, `fifo.vhd` and `fifo.vhdl`, or a dependency's `rtl/fifo.vhd` (named
        relative to *its* root) beside this design's all fold alike. So a name that another of
        the design's sources would also get carries a short digest of its file's path; every
        other name is only the fold. Distinct sources get distinct names by construction, and a
        source gets the same name whichever of its lists it is found through.

        This is naming, not identity: nothing here may be read back as design state.
        """
        name = self._folded_source_name(src)
        this = src.file.resolve()
        if any(
            other.file.resolve() != this and self._folded_source_name(other) == name
            for other in (*self.rtl.sources, *self.tb.sources)
        ):
            name += "_" + hashlib.sha256(str(this).encode()).hexdigest()[:8]
        return name + suffix

    def _folded_source_name(self, src: FileResource) -> str:
        """`src`'s path as this design names it, folded into one filename-safe token."""
        parts = (
            re.sub(r"\W+", "_", part).strip("_")
            for part in self.source_path_as_named(src).with_suffix("").parts
        )
        return "_".join(part for part in parts if part)

    @property
    def rtl_fingerprint(self) -> Dict[str, Any]:
        """Location-independent inputs that can change RTL compilation or synthesis.

        Source order is significant (notably for VHDL), and source metadata tells tools how to
        compile identical bytes. Every source's path counts relative to the design root
        (`_source_fingerprint`), as does a parameter whose value is a path
        (`_parameters_fingerprint`). Moving the whole design does not change its identity.
        """
        return {
            "sources": [self._source_fingerprint(src) for src in self.rtl.sources],
            "parameters": self._parameters_fingerprint(self.rtl),
            "defines": dict(self.rtl.defines),
            "top": self.rtl.top,
            "attributes": self.rtl.attributes,
            "clocks": [clock.model_dump() for clock in self.rtl.clocks],
            "language": self.language.model_dump(),
        }

    @property
    def tb_fingerprint(self) -> Dict[str, Any]:
        """Describe testbench inputs used to identify a run."""
        return {
            "sources": [self._source_fingerprint(src) for src in self.tb.sources],
            "parameters": self._parameters_fingerprint(self.tb),
            "defines": dict(self.tb.defines),
            "top": self.tb.top,
            "uut": self.tb.uut,
            "cocotb": self.tb.cocotb.model_dump() if self.tb.cocotb is not None else None,
        }

    def _parameters_fingerprint(self, dv: DVSettings) -> Dict[str, Any]:
        """Parameter values as the tool sees them, except that a path under the design root
        counts relative to it (`$DESIGN_ROOT/rom.mem`) -- the rule `flowrun_hash` applies to
        settings -- so a design given a file under its root keeps its identity wherever it is
        moved. A path outside the root is the location the design names, and counts as such.
        """
        roots = [("DESIGN_ROOT", self.root_path)] if self.design_root else []
        return {k: location_free(v, roots) for k, v in dv.parameters.items()}

    def relative_to_root(self, path: Union[str, os.PathLike]) -> Optional[Path]:
        """`path` relative to the design root, or None when it is not an absolute path under
        it: whether a path is part of the design's own tree, by the rule its fingerprint uses
        (`location_free`). The root is resolved when the design is built, and so is every path
        a design resolves against it."""
        path = Path(path)
        if self.design_root and path.is_absolute() and path.is_relative_to(self.root_path):
            return path.relative_to(self.root_path)
        return None

    def _source_fingerprint(self, source: DesignSource) -> Dict[str, Any]:
        """What a source *is*: its content, type, compile metadata and path relative to the
        design root. A source's location may affect include resolution, commands in a sourced
        constraint or script, and tool lookup. Counting every declared source by path avoids
        reusing a cached result for a different layout.

        Always relative to the root, outside it too (`../lib/defs.vh`): what a lookup by place
        depends on is where files sit relative to each other, so moving a design together with
        what it names keeps its identity, and so does however the path was written.
        """
        return {
            "content": source.content_hash,
            "type": str(source.type) if source.type is not None else None,
            "standard": source.standard,
            "variant": source.variant,
            "path": Path(os.path.relpath(source.file, self.root_path)).as_posix(),
        }

    @property
    def rtl_hash(self) -> str:
        fingerprint = self.rtl_fingerprint
        log.debug("RTL fingerprint: %s", fingerprint)
        return semantic_hash(fingerprint)[:32]  # 128 bits

    @property
    def tb_hash(self) -> str:
        fingerprint = self.tb_fingerprint
        log.debug("TB fingerprint: %s", fingerprint)
        return semantic_hash(fingerprint)[:32]  # 128 bits

    def parts_hash(self, parts: Iterable[str]) -> str:
        """The hash of the design's `parts` (`DESIGN_PARTS`: `rtl`, `tb`), each as its own hash
        (`rtl_hash`, `tb_hash`) and in a fixed order, whatever order or collection `parts` is
        given in. It is the design identity of a flow that reads exactly those parts
        (`Flow.design_parts`): what a part it does not read says never moves it. Both parts
        make the whole design's hash."""
        wanted = frozenset(parts)
        unknown = wanted - DESIGN_PARTS
        if unknown:
            raise ValueError(
                f"{', '.join(sorted(unknown))} is not a part of a design "
                f"(its parts are {', '.join(_DESIGN_PART_ORDER)})"
            )
        return semantic_hash(
            {
                f"{part}_hash": getattr(self, f"{part}_hash")
                for part in _DESIGN_PART_ORDER
                if part in wanted
            }
        )
