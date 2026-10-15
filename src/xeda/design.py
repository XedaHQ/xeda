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
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from glob import escape as glob_escape
from glob import glob
from os.path import isfile
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
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
from pydantic_core import ErrorDetails, InitErrorDetails, PydanticCustomError, core_schema

from .dataclass import (
    WHOLE,
    AliasChoices,
    Field,
    SerializeAsAny,
    ValidationError,
    XedaBaseModel,
    canonical_tree,
    field_validator,
    input_names,
    model_validator,
    model_with_allow_extra,
    shape_problems,
    unspecified,
    validation_errors,
)
from .digest import content_digest
from .generation import judging_generation
from .proc_utils import tool_output_redirect
from .run_dir import DIR_NAME_HASH_LEN, RunDirectory, run_directory_name
from .utils import (
    ConflictingKeys,
    NonZeroExitCode,
    WorkingDirectory,
    XedaException,
    expand_env_vars,
    expand_hierarchy,
    flows_table_problems,
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
    TOML/JSON/YAML, has a suffix Xeda does not read, or does not hold a table of design fields.

    The message names the file and, when the parser knows them, the 1-based `line` and `column`.
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


#: The `type` of a design validation error that says a file the design names is not there.
MISSING_FILE_ERROR = "missing_file"


class MissingFile(ValueError):
    """The error for a file that the design names and that does not exist.

    A generator may write the file, so a launch that defers a generator tells this error from
    every other (`MISSING_FILE_ERROR`)."""


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

    @property
    def only_missing_files(self) -> bool:
        """Whether every error is a file the design names that is not there (`MissingFile`)."""
        return bool(self.errors) and all(kind == MISSING_FILE_ERROR for *_, kind in self.errors)

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
        ) + (
            f"\nDesign:\n{pformat(_shown_dependencies(self.data))}\n"
            if self.data and self.design_in_msg
            else ""
        )


def _design_errors(
    errors: List[ErrorDetails],
) -> List[Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """Return `validation_errors(errors)`, with the type `MISSING_FILE_ERROR` on a `MissingFile`."""
    return [
        (
            (loc, msg, context, MISSING_FILE_ERROR)
            if isinstance((raw.get("ctx") or {}).get("error"), MissingFile)
            else (loc, msg, context, kind)
        )
        for raw, (loc, msg, context, kind) in zip(errors, validation_errors(errors))
    ]


#: The format in which Xeda reads a design file, by its suffix. `Design.from_file` reads by it,
#: and the launcher tells a design file from a design's name by it.
DESIGN_FILE_FORMATS: dict[str, str] = {
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def names_a_design_file(path: str | os.PathLike) -> bool:
    """Return whether `path` is meant as a design file and not as a design's name in a project.

    A path with a design-file suffix in any case is a design file. `Design.from_file` rejects a
    mis-cased suffix and names the right spelling, so the path is not looked up in a project and
    reported missing there."""
    return Path(path).suffix.lower() in DESIGN_FILE_FORMATS


def design_file_format(path: str | os.PathLike) -> str:
    """Return the format (`toml`, `json` or `yaml`) in which design file `path` is read.

    The suffix decides. Suffixes are case-sensitive, like every name Xeda reads: a suffix such as
    `.TOML` is rejected, and the error names `.toml`.
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
    """Return `(message, line, column)` of a TOML error.

    Python 3.14 gives them as attributes. Earlier versions give them only in the
    `(at line L, column C)` suffix of the message."""
    line, column = getattr(e, "lineno", None), getattr(e, "colno", None)
    msg = getattr(e, "msg", None)
    if isinstance(msg, str) and line is not None:
        return msg, line, column
    m = re.fullmatch(r"(.*?)\s*\(at line (\d+), column (\d+)\)", str(e), re.DOTALL)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    return str(e), None, None


def _read_design_file(path: Path) -> dict[str, Any]:
    """Read the table of design fields in design file `path`.

    Every failure is a `DesignFileParseError` that names the file (and the line and column, when
    the parser knows them). The failures are a suffix that Xeda does not read, a file that cannot
    be opened or is not UTF-8, TOML/JSON/YAML that does not parse, and a document that is not a
    table.
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
        # `lineno` and `colno` are 1-based already.
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
    """Return `path` with its environment variables expanded.

    `$DESIGN_ROOT` and `$DESIGN_DIR` are `root`, the directory that a relative path resolves
    against, so `$DESIGN_ROOT/a.vhd` and `a.vhd` are always the same file. `$PWD` is left as
    written. `escape` applies to every substituted value (`glob.escape`, for a pattern)."""
    return expand_env_vars(
        str(path), {"PWD": None, "DESIGN_ROOT": root, "DESIGN_DIR": root}, escape=escape
    )


def _is_source_pattern(source: str) -> bool:
    """Return whether a source names its files by pattern.

    Only `*` is pattern syntax. The characters `?`, `[` and `]` are ordinary characters of a file
    name (`foo[1].v`, a bus index). As glob syntax they would make such a name match another file
    (`foo1.v`) when one exists. No file name worth using contains `*` (Windows forbids it)."""
    return "*" in source


def _expand_source_glob(pattern: str, root: Path, what: str = "source") -> List[str]:
    """Return the files that a source pattern (`_is_source_pattern`) names, in a stable order.

    Variables expand before globbing, or `glob` looks for a directory that is literally named
    `$DESIGN_ROOT`. The result is sorted because `glob` returns files in file-system order, while
    source order matters: VHDL units compile in that order, and the order is part of the design
    hash. An unsorted pattern would give the same design another identity on another file system.
    A pattern that names no file is an error. A silent empty match would reach a tool as a
    missing top module.

    The value of a variable is a place, not more pattern, so it is escaped. Without the escape, a
    design root named `proj[1]` would match a sibling `proj1`, which holds another project's
    sources. Only files match, as in a shell glob of source files. A directory that is named
    like a source is skipped.
    """
    expanded = str(_expand_design_path(pattern, root))
    matched = _globbed_source_files(pattern, root)
    if not matched:
        # A generator's own inputs are not its output.
        error = MissingFile if what == "source" else ValueError
        raise error(
            f"no file matches the {what} pattern '{pattern}'"
            + (f" (expanded to '{expanded}')" if expanded != pattern else "")
        )
    return matched


def _globbed_source_files(pattern: str, root: Path) -> List[str]:
    """Return the files that match a pattern in which only `*` is pattern syntax.

    The text escapes `?` and `[`, and variable values are literal path components."""
    written = re.sub(r"[?\[]", r"[\g<0>]", pattern)
    expanded = _expand_design_path(written, root, escape=glob_escape)
    return sorted(m for m in glob(str(expanded)) if isfile(m))


def as_source_list(sources: Any) -> Any:
    """Return `sources` as the list it stands for.

    One source (a path, a table or a resource) stands for a list of one. This function is the only
    place that knows the form. The `sources` validator, the decision to run a generator again and
    the declared load all use it. Anything else is returned unchanged."""
    if isinstance(sources, (str, os.PathLike, Mapping, FileResource)):
        return [sources]
    return sources


def _source_paths_as_given(sources: Any, root: Path) -> Optional[List[Path]]:
    """Return the files that an unvalidated `rtl.sources` names, to decide if a generator must run.

    `process_generation` runs before the sources validator, so it sees whatever the design file
    wrote: a path, a `$DESIGN_ROOT` spelling, a pattern or a table with `file` or `path`. This
    function expands each spelling. It returns `None` for anything it cannot interpret. The
    validator reports that a moment later, and until then the safe answer is to run the generator.
    """
    sources = as_source_list(sources)
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
            # A pattern that matches nothing yet is a reason to run the generator.
            for match in _globbed_source_files(src, root):
                path = Path(match)
                paths.append(path if path.is_absolute() else root / path)
        else:
            expanded = _expand_design_path(src, root)
            paths.append(expanded if expanded.is_absolute() else root / expanded)
    return paths


def _describe_generator(generator: Any) -> str:
    """Return the text that names a generator in an error, whichever input form it has."""
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
        """Create a resource for a file of the design.

        The argument is a path, or a table with either a `file` (which must exist) or a `path`
        (which is not checked, for a file that a generator creates later). A plain path is a
        `file`, and a missing one raises `FileNotFoundError`.

        A relative path resolves against `_root_path`, by default the working directory. `Design`
        sets `_root_path` to the design root while it validates (see `_expand_design_path`).
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
                    "Either 'file' (existing file) or 'path' (unchecked path) must be set "
                    "for a FileResource."
                )
            path = path_value
        root = Path(_root_path) if _root_path else Path.cwd()
        # The path as written, before any variable is expanded or the path is made absolute. The
        # design's identity does not depend on it (see `Design._source_fingerprint`). A flow that
        # names a file after where it came from (GHDL, for two VHDL sources with the same stem)
        # needs what the design said, not an absolute location.
        self._specified_path = Path(path)
        path = _expand_design_path(path, root)
        if not path.is_absolute():
            path = root / path
        if resolve and not path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        if resolve and not path.is_file():
            raise IsADirectoryError(errno.EISDIR, "a directory, not a file", str(path))
        self.file = path.resolve() if resolve else path.absolute()
        #: True for a file that must exist (`file`), false for one that need not exist yet (`path`).
        self._checked = bool(resolve)

    @property
    def path(self) -> Path:
        return self.file

    @property
    def content_hash(self) -> str:
        """The hash of the file's content, which identifies the design.

        The file is read on each call. A file that an application edits between two launches of
        the same `Design` gives its content at the time of the launch, not at the first launch.

        A file that does not exist has no content and no identity, so the design cannot be
        hashed, cached or run. A `path` entry defers the check until the run is identified. By
        then, whatever was to write the file has had its chance.
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
                "A file given as `path` is not checked while the design loads, because a "
                "generator is expected to write it. Nothing wrote it."
            ) from e

    # Two resources are equal when they name the same place, whatever the file holds. This also
    # covers a `path` file that is not written yet: comparing contents would raise when a source
    # is de-duplicated on re-validation, and the same file has the same contents anyway. `file`
    # is absolute, and resolved for a checked resource.
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
        """Return this resource as a JSON value that loads back as the same resource.

        A bare path string reloads as a checked `file`. A resource built from `path` (a file that
        need not exist yet, such as a generator's output or a file that a testbench writes) must
        keep saying `path`. Otherwise reloading the document fails on a file that the design never
        promised to have.
        """
        return str(self.file) if self._checked else {"path": str(self.file)}

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> Any:
        """Validate as an arbitrary type: an instance check.

        Values reach this schema already coerced by the `mode="before"` validators of the fields
        that declare them, so an `isinstance` check is enough. The serializer uses
        `when_used="json"`, so `model_dump()` returns the object itself, and serializing to JSON
        emits what `as_json_value()` builds: the path, or the table that the resource cannot be
        rebuilt without.
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

        Without this, `Design.model_json_schema()` raises `PydanticInvalidForJsonSchema`, and
        editors and coding agents would have no machine-readable description of a design file.
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
                    # Use `oneOf`, not `anyOf`. `FileResource` rejects an object that gives
                    # neither key and one that gives both, so exactly one branch must match.
                    # Under `anyOf`, an object with both keys satisfies both branches and passes
                    # the schema, while `Design.from_file` rejects it.
                    "oneOf": [{"required": ["file"]}, {"required": ["path"]}],
                },
            ],
        )
        return field_schema


class SourceType(str, Enum):
    """The type of a design source file.

    Each value is the member name, so serialized settings and results describe themselves. The
    declaration order matters (see `from_str`): new members are appended after the last one and
    are never inserted or reordered.
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
    # The ordinals 1 to 13 above are frozen, because old `settings.json` files record them. Every
    # later member is appended.
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
    # Types by stage. `FpgaNetlist` is a Verilog netlist of FPGA primitives (what Vivado writes),
    # and `VerilogNetlist` is a standard-cell netlist. `FpgaTimingNetlist` is the timing netlist
    # of the same design, simulated with an SDF. A checkpoint is written at several stages, and
    # only the stage says what reads it. So no flow takes `Checkpoint` (no stage), and `.dcp`
    # names no type. An OpenROAD database (`.odb`) and the SDC that a stage writes with it are
    # a pair per stage (placed, clock tree, global routed, routed). So no flow takes `Odb`, and
    # `.odb` names no type. The SDC of a stage needs its own type, because clock tree synthesis
    # changes the SDC, and a design's `.sdc` file must never stand in for it.
    FpgaNetlist = "FpgaNetlist"
    SynthCheckpoint = "SynthCheckpoint"
    RoutedCheckpoint = "RoutedCheckpoint"
    FpgaTimingNetlist = "FpgaTimingNetlist"
    OpenroadDbPlaced = "OpenroadDbPlaced"
    OpenroadDbCts = "OpenroadDbCts"
    OpenroadDbGlobalRouted = "OpenroadDbGlobalRouted"
    OpenroadDbRouted = "OpenroadDbRouted"
    OpenroadSdcPlaced = "OpenroadSdcPlaced"
    OpenroadSdcCts = "OpenroadSdcCts"
    OpenroadSdcGlobalRouted = "OpenroadSdcGlobalRouted"
    OpenroadSdcRouted = "OpenroadSdcRouted"

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
        # Older versions of Xeda built this enum with `auto()`, so their `settings.json` files
        # record 1-based ordinals ("5" for Vhdl) in place of names. The declaration order above
        # must not change while these files are supported.
        if source_type.isdigit():
            members = list(cls.__members__.values())
            idx = int(source_type)
            if 1 <= idx <= len(members):
                return members[idx - 1]
        return None


#: The type that each suffix infers. The suffix is written without its dot and in its own letter
#: case, because inference is case-sensitive. A member that no suffix infers is given only by
#: `type` (`TYPE_ONLY`). `tests/test_source_types.py` pins both.
SOURCE_SUFFIXES: dict[str, SourceType] = {
    "v": SourceType.Verilog,
    "vh": SourceType.VerilogHeader,
    "sv": SourceType.SystemVerilog,
    "svh": SourceType.SVHeader,
    "vhd": SourceType.Vhdl,
    "vhdl": SourceType.Vhdl,
    "bsv": SourceType.Bluespec,
    "bs": SourceType.Bluespec,
    "bh": SourceType.Bluespec,
    "xdc": SourceType.Xdc,
    "sdc": SourceType.Sdc,
    "mem": SourceType.MemoryFile,
    "init": SourceType.MemoryFile,
    "hex": SourceType.MemoryFile,
    "tcl": SourceType.Tcl,
    "sc": SourceType.Chisel,
    "cc": SourceType.Cpp,
    "cpp": SourceType.Cpp,
    "cxx": SourceType.Cpp,
    "py": SourceType.Cocotb,
    "lpf": SourceType.Lpf,
    "pcf": SourceType.Pcf,
    "pdc": SourceType.Pdc,
    "asc": SourceType.IceAsc,
    "fasm": SourceType.Fasm,
    "bit": SourceType.Bitstream,
    "sof": SourceType.Bitstream,
    "blif": SourceType.Blif,
    "edf": SourceType.Edif,
    "edif": SourceType.Edif,
    "ucf": SourceType.Ucf,
    "xcf": SourceType.Xcf,
    "qsf": SourceType.Qsf,
    "ldc": SourceType.Ldc,
    "fdc": SourceType.Fdc,
    "sdf": SourceType.Sdf,
    "spef": SourceType.Spef,
    "saif": SourceType.Saif,
    "vcd": SourceType.Vcd,
    "fst": SourceType.Fst,
    "ghw": SourceType.Ghw,
    "vpd": SourceType.Vpd,
    "fsdb": SourceType.Fsdb,
    "lib": SourceType.Liberty,
    "def": SourceType.Def,
    "gds": SourceType.Gds,
    "cdl": SourceType.Cdl,
    "c": SourceType.C,
    "h": SourceType.CHeader,
    "hpp": SourceType.CHeader,
    "o": SourceType.ObjectFile,
    "a": SourceType.ObjectFile,
    "vlt": SourceType.Vlt,
}

#: Suffixes that name several kinds of file. A source with one of them needs its `type`. Each
#: suffix maps to the members that it most often stands for, which the error message lists.
AMBIGUOUS_SUFFIXES: dict[str, tuple[SourceType, ...]] = {
    "json": (SourceType.JsonNetlist,),
    "bin": (SourceType.Bitstream, SourceType.Chipdb),
    "cfg": (SourceType.EcpConfig,),
    "config": (SourceType.EcpConfig,),
    "dcp": (SourceType.SynthCheckpoint, SourceType.RoutedCheckpoint),
    "odb": (
        SourceType.OpenroadDbPlaced,
        SourceType.OpenroadDbCts,
        SourceType.OpenroadDbGlobalRouted,
        SourceType.OpenroadDbRouted,
    ),
}

#: Members that no suffix infers. They come from an explicit `type` or from a declared output.
TYPE_ONLY: frozenset[SourceType] = frozenset(
    {
        SourceType.JsonNetlist,
        SourceType.EcpConfig,
        SourceType.VerilogNetlist,
        SourceType.VhdlNetlist,
        SourceType.Chipdb,
        SourceType.Data,
        SourceType.FpgaNetlist,
        SourceType.FpgaTimingNetlist,
        SourceType.Checkpoint,
        SourceType.SynthCheckpoint,
        SourceType.RoutedCheckpoint,
        SourceType.Odb,
        SourceType.OpenroadDbPlaced,
        SourceType.OpenroadDbCts,
        SourceType.OpenroadDbGlobalRouted,
        SourceType.OpenroadDbRouted,
        SourceType.OpenroadSdcPlaced,
        SourceType.OpenroadSdcCts,
        SourceType.OpenroadSdcGlobalRouted,
        SourceType.OpenroadSdcRouted,
    }
)


#: The source types of the languages whose omission would leave a design incomplete. A flow that
#: declares `reads_sources` refuses a language it cannot read. Other source types may belong to
#: another flow.
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
    """Return the member that `text` names, as `SourceType.from_str` reads it.

    `text` is a member name in any letter case, or the ordinal of an old `settings.json`. Raise a
    `ValueError` that names the closest members when there is no such member."""
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


def source_type_of(path: Path) -> SourceType:
    """Return the type that the suffix of `path` says (`SOURCE_SUFFIXES`).

    Raise a `ValueError` that asks for an explicit `type` when the suffix names several kinds of
    file, is in another letter case than the one Xeda knows, or is one that Xeda infers nothing
    from."""
    suffix = path.suffix[1:]
    if suffix in SOURCE_SUFFIXES:
        return SOURCE_SUFFIXES[suffix]
    if suffix in AMBIGUOUS_SUFFIXES:
        candidates = AMBIGUOUS_SUFFIXES[suffix]
        raise ValueError(
            f"{path}: `.{suffix}` names several kinds of file "
            f"({', '.join(m.name for m in candidates)} among them): give its type as a table, "
            f"in YAML {{file: {path.name}, type: {candidates[0].name}}}"
        )
    if suffix.lower() in SOURCE_SUFFIXES:
        member = SOURCE_SUFFIXES[suffix.lower()]
        raise ValueError(
            f"{path}: Xeda infers no source type from `.{suffix}` (`.{suffix.lower()}` is "
            f"{member.name}): rename it, or give its type as a table, in YAML {{file: {path.name}, "
            f"type: {member.name}}}"
        )
    what = f"`.{suffix}`" if suffix else "a file without a suffix"
    raise ValueError(
        f"{path}: Xeda infers no source type from {what}: give its type as a table, in YAML "
        f"{{file: {path.name}, type: Data}}. `Data` has no automatic HDL frontend (test "
        f"vectors, a script's input). The types: {', '.join(m.name for m in SourceType)}"
    )


#: The keys of a source table: the file (`file`, or `path` for one not written yet) and its `type`.
SOURCE_TABLE_KEYS = frozenset({"file", "path", "type"})

#: The keys that a source table no longer takes, with what to write instead.
REMOVED_SOURCE_KEYS: dict[str, str] = {
    "standard": (
        "a source has no standard of its own: `language.vhdl.standard` sets the VHDL standard "
        "of the whole design"
    ),
    "variant": (
        "a source has no variant: a Bluespec source's suffix (`.bsv`, `.bs` or `.bh`) says what "
        "it is, and `type: SystemVerilog` makes a SystemVerilog source"
    ),
}


def source_table_problems(table: Mapping[str, Any]) -> list[str]:
    """Return one message for each wrong key of a source table.

    A removed key (`REMOVED_SOURCE_KEYS`) says what to write instead. Any other key that is not in
    `SOURCE_TABLE_KEYS` is a mistake that nothing would read."""
    problems = []
    for key in table:
        if key in SOURCE_TABLE_KEYS:
            continue
        if key in REMOVED_SOURCE_KEYS:
            problems.append(f"`{key}` was removed from a source: {REMOVED_SOURCE_KEYS[key]}")
        else:
            problems.append(
                f"unknown key `{key}` in a source (a source table has `file` or `path`, and "
                f"`type`)"
            )
    return problems


class DesignSource(FileResource):
    type: SourceType

    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Union[str, SourceType, None] = None,
        _root_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        """Build a design source while retaining its stated type."""
        if isinstance(path, dict):
            problems = source_table_problems(path)
            if problems:
                raise ValueError("; ".join(problems))
            path = dict(path)
            stated_type = path.pop("type", None)
            if typ is None:
                typ = stated_type
        super().__init__(path, _root_path=_root_path, **kwargs)

        if isinstance(typ, SourceType):
            self.type = typ
        elif isinstance(typ, str):
            self.type = source_type_named(typ)
        elif typ is None:
            self.type = source_type_of(self.file)
        else:
            raise ValueError(f"a source's `type` is the name of a source type, not {typ!r}")
        # The type that the design stated, as opposed to the one that the file name implied. Only
        # the stated type is written back out: reloading infers the other one from the same
        # suffix again. A stated `type` that contradicts the suffix
        # (`{file: legacy.v, type: SystemVerilog}`) would be lost if the dump left it out, and it
        # is part of the design's identity.
        self._stated: dict[str, Any] = {"type": str(self.type)} if typ is not None else {}

    def as_json_value(self) -> Union[str, Dict[str, Any]]:
        """Serialize a source with only the metadata explicitly supplied."""
        value = super().as_json_value()
        if not self._stated:
            return value
        if isinstance(value, str):
            value = {"file": value}
        return {**value, **self._stated}

    def __eq__(self, other: Any) -> bool:  # pylint: disable=useless-super-delegation
        # Added attributes do not change semantic equality.
        return super().__eq__(other)

    def __hash__(self) -> int:  # pylint: disable=useless-super-delegation
        # Added attributes do not change semantic identity.
        return super().__hash__()

    def __repr__(self) -> str:
        return f"file:{self.file} type:{self.type}"

    @classmethod
    def _json_schema(cls) -> Dict[str, Any]:
        field_schema = super()._json_schema()
        field_schema["description"] = (
            "A design source file: either a path string (relative paths are resolved against the "
            "design file's directory) or an object. The source type is inferred from the file "
            "extension unless 'type' is given explicitly."
        )
        object_schema = field_schema["anyOf"][1]
        # A source table takes exactly these keys, as `source_table_problems` judges it.
        object_schema["additionalProperties"] = False
        object_schema["properties"].update(
            {
                "type": {
                    "type": "string",
                    "enum": [t.name for t in SourceType],
                    "description": "Source type. Inferred from the file extension when omitted.",
                },
            }
        )
        return field_schema


DefineType = Any

# A tuple of 0, 1 or 2 strings.
Tuple012 = Union[Tuple[str, ...], Tuple[str], Tuple[str, str]]  # xtype: ignore


_PARAMETERS_FORM_ERROR = (
    "parameters/generics must be a dictionary or a list of objects with 'name' and 'value' "
    "attributes"
)


def _parameters_as_mapping(value: Any) -> Any:
    """Return the two interchangeable parameter/generic input forms as the one mapping form.

    Anything that is neither is returned unchanged, for the caller to reject or ignore.
    """
    if not isinstance(value, list):
        return value
    normalized = {}
    for entry in value:
        # Check before `.get`. A bare list such as `parameters: [W]` would raise
        # `AttributeError: 'str' object has no attribute 'get'`, and the validator guard does not
        # turn that into a validation error.
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

    A parameter written as a file (a table with `file`, which must exist, or with `path`, which
    need not exist yet) becomes the absolute path of that file. A tool takes a parameter as plain
    text, and the tool runs in a run directory, not in the design's directory. That text is the
    one value of the parameter, and nothing else records the table that it was written as. The
    design's identity must not depend on where the design lives, so
    `Design._parameters_fingerprint` leaves that part out. A remote run re-roots such a path from
    the value alone (`send_design`).
    """
    if value is None:
        # Do not test `not value`: an empty list (`parameters: []`) is the list form and must
        # become `{}` like any other list.
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
                raise MissingFile(f"parameter {key!r}: file does not exist: {e.filename}") from e
    return value


def _parameters_form(value: Any) -> Any:
    """Return the table that a list of `{name, value}` objects stands for (`field_shorthands`)."""
    return _parameters_as_mapping(value) if isinstance(value, list) else None


#: What `sources` accepts, shown by `xeda design-schema`.
SOURCES_DESCRIPTION = (
    "Source files, in compile order: a path relative to the design root (`$DESIGN_ROOT` and "
    "`$DESIGN_DIR` name it too), or a table, in YAML `{file: ..., type: ...}` (`path` in place of "
    "`file` for one not written yet). A path containing `*` "
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
        description="Parameters of the top module (Verilog parameters or VHDL generics): a "
        "mapping, or a list of `{name, value}` objects. `generics` is accepted as another name "
        "for the same setting.",
    )
    defines: Dict[str, DefineType] = Field(
        default={},
        description="Verilog preprocessor macros, as a mapping of macro name to value. A value of "
        "`null` defines the macro without a value.",
    )

    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "parameters": _parameters_form
    }
    #: A target's sources come after the design's (`merge_layers(append=True)`); an override's
    #: replace them, as every other layer's list does.
    appended_fields: ClassVar[tuple[str, ...]] = ("sources",)
    #: One source stands for a list of one (`as_source_list`).
    single_item_fields: ClassVar[tuple[str, ...]] = ("sources",)

    @property
    def generics(self) -> Dict[str, DefineType]:
        """Return `parameters` by its VHDL name. It is one setting, stored once."""
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

        value = as_source_list(value)
        sources: List[DesignSource] = []

        def source_already_exists(src: DesignSource) -> bool:
            for s in sources:
                if s.file.absolute() == src.file.absolute():
                    return True
            return False

        if not isinstance(value, (list, tuple, set)):
            raise ValueError(
                f"'sources' must be a list of source files, got {type(value).__name__}: {value!r}"
            )
        for src in unique(list(value)):
            if isinstance(src, str):
                src_type = None
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
                    raise MissingFile(
                        f"source file does not exist: {e.filename} (give a source that a "
                        "generator writes later as a table, in YAML `{path: <file>}`)"
                    ) from e
            if not source_already_exists(src):
                sources.append(src)
        return sources


#: The two spellings of a design's clock, which are one setting.
CLOCK_SPELLINGS = ("clock", "clocks")


class Clock(XedaBaseModel):
    port: str
    name: Optional[str] = None

    @classmethod
    def as_mapping(cls, value: Any) -> Any:
        """Read a bare string as the clock's port: `clock: CLK` means `clock: {port: CLK}`."""
        return {"port": value} if isinstance(value, str) else None

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
    # Sweepable parameters used in the command.
    parameters: dict = {}
    sources: List[Path] = Field(
        default_factory=list,
        description="The sources this generator reads, as a design's `sources` name them: a path "
        "relative to the design root, or a pattern where `*` is the only pattern character. Each "
        "one's content is what decides whether the generator runs again, so each has to exist. "
        "A directory counts as every file in it, so name what the generator reads from outside "
        "the design too (a library tree, an editable clone). Xeda assumes nothing about the "
        "generator's language or environment.",
    )
    #: One path stands for a list of one (the `sources` validator below).
    single_item_fields: ClassVar[tuple[str, ...]] = ("sources",)
    always_runs: bool = Field(
        default=False,
        description="Run this generator on every design load: what it reads cannot be judged "
        "(it is not files, or they cannot be listed), so Xeda keeps no record of it. A "
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
        """Name a removed field with what replaces it, as for every removed setting."""
        if "run_only_if_sources_modified" in data:
            # The old switch was on by default. Written `true`, it asks for what now always
            # applies (judging by content), so the advice is to delete it, never to turn it
            # into `always_runs`.
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
        """Store the arguments as a list of words, however they were written.

        A string is split on whitespace, as `command` is, and a list is already the words. Every
        reader (`execution_command`, the freshness identity) sees a list."""
        return value.split() if isinstance(value, str) else value

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value):
        """Expand source patterns and normalize the resulting paths."""
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
                # Resolve it like the string spelling, so a relative path does not keep
                # depending on the working directory it was given in.
                sources.append(src.resolve())
            else:
                raise ValueError(f"Invalid source type: {type(src)} for source '{src}'")
        # Remove duplicates and keep the order.
        sources = unique(sources)
        # The content of the generator's inputs decides whether to run it again
        # (`generation.generation_identity`), so each input must exist.
        missing = [str(src) for src in sources if not src.exists()]
        if missing:
            raise ValueError(f"generator source file does not exist: {', '.join(missing)}")
        return sources

    def execution_command(self, design_root: Optional[Path] = None) -> List[str]:
        """Return the argv that this generator runs, for execution and the freshness identity."""
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
            # A generator's subprocess inherits the stdout of Xeda, which would corrupt a
            # `--json` document. `tool_output_redirect()` is None unless output has been
            # redirected, so normal runs keep inheriting.
            stdout = tool_output_redirect()
        # Check the status here, not in `subprocess.run`, so that a failing generator raises
        # `NonZeroExitCode`, like the other two spellings of a generator, and not a raw
        # `CalledProcessError`.
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
        """The kind of generator (its class name), not which generator it is.

        Do not use it for display; see `describe`."""
        return str(self.__class__.__qualname__ or "generator")

    def describe(
        self, design_name: Optional[str] = None, design_root: Optional[Path] = None
    ) -> str:
        """Return which generator this is, for a message.

        The text names the design that the generator belongs to and what it runs, for example
        `the generator of design 'hdmi_demo' (python3 hdmi_demo.py --build)`. A command that
        cannot be built yet (no executable, no Chisel project) gives the kind of generator
        instead."""
        owner = f" of design '{design_name}'" if design_name else ""
        return f"the generator{owner} ({self.command_text(design_root)})"

    def command_text(self, design_root: Optional[Path] = None) -> str:
        """Return what this generator runs, for a message, or its kind if that is not known yet."""
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
    """The RTL: sources, top module, clocks, generator, attributes, parameters and defines."""

    top: Optional[str] = Field(
        None,
        description="The RTL top module (a Verilog module or a VHDL entity).",
    )
    # `SerializeAsAny`: a generator may be a `ChiselGenerator`, and pydantic 2 serializes a
    # nested model by its annotated type unless told otherwise. The field declares it. A blanket
    # dump flag would duck-type every value and so skip the serializer that an arbitrary type
    # like `DesignSource` attaches to its own schema, which made `Design.model_dump_json()` raise
    # `PydanticSerializationError`.
    generator: Union[str, List[str], SerializeAsAny[Generator], None] = None
    attributes: Dict[str, Dict[str, Any]] = Field(
        dict(),
        description="""
        HDL attributes for modules, ports and other objects. The flow that reads an attribute
        decides its meaning and behavior.
        Write the attributes as a mapping of attr_name->(path->attr_value):
        - the key is the name of the attribute
        - the value is a mapping of path->attr_value:
            - the key is the path or scope on which the attribute applies
            - the value is the value of the attribute
        """,
    )
    # The only stored form of the design's clock ports. `clock` is an input and API shorthand for
    # the first of them, and a property below derives it.
    clocks: List[Clock] = []

    #: The inputs that no longer exist, with what replaces each. Giving one is an error that says
    #: so, in place of "extra inputs are not permitted". It is the design's equivalent of
    #: `Flow.Settings.removed_settings`. A flat top-level key, a target's key and
    #: `--design-overrides` reach the same check, because each is folded into `rtl` first.
    removed_inputs: ClassVar[Dict[str, str]] = {"clock_port": "`clock: <port>`"}
    #: One clock stands for a list of one (`clock_tables`).
    single_item_fields: ClassVar[tuple[str, ...]] = ("clocks",)

    @classmethod
    def removed_input_problems(cls, values: Mapping[str, Any]) -> list[tuple[str, str]]:
        """Return the removed inputs that `values` writes, each with its error message."""
        return [
            (name, f"`{name}` was removed: use {replacement}")
            for name, replacement in cls.removed_inputs.items()
            if name in values
        ]

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Advertise the accepted single-clock shorthand without storing duplicate state."""
        schema = handler(core_schema)
        properties = schema.setdefault("properties", {})
        clock_item = deepcopy(properties["clocks"]["items"])
        properties["clock"] = {
            "anyOf": [clock_item, {"type": "string"}, {"type": "null"}],
            "description": "Single design clock shorthand. Prefer an object with `port`. "
            "`null` means no clock.",
            "x-xeda-input-only": True,
        }
        return schema

    @model_validator(mode="before")
    @classmethod
    def rtl_settings_validate(cls, values, info):  # pylint: disable=no-self-argument
        """Normalize the single accepted clock input into the canonical `clocks` list."""
        if info.field_name is not None and info.data is None:
            # Assignment to a real field. The clocks field validator handles clocks itself;
            # unrelated assignments must not reconstruct or detach the established list.
            return values

        for name, message in cls.removed_input_problems(values):
            # Report it at the key as written, as for a wrong form of a clock.
            raise ValidationError.from_exception_data(
                cls.__name__,
                [
                    InitErrorDetails(
                        type=PydanticCustomError(
                            "removed_input", "{message}", {"message": message}
                        ),
                        loc=(name,),
                        input=values[name],
                    )
                ],
            )
        present = [name for name in CLOCK_SPELLINGS if name in values]
        if len(present) > 1:
            raise ValueError(
                "Specify only one of `clock` or `clocks`; prefer `clock` for "
                "one clock and `clocks` for several"
            )
        if present:
            spelling = present[0]
            if spelling != "clocks":
                value = values.pop(spelling)
                if cls.clock_mistake(spelling, value):
                    # Report it at the key as written, not at the `clocks` it would become.
                    what = cls.clock_expected()
                    raise ValidationError.from_exception_data(
                        cls.__name__,
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "clock_form",
                                    "{message}",
                                    {"message": f"`{spelling}` is {what}, not {value!r:.60}"},
                                ),
                                loc=(spelling,),
                                input=value,
                            )
                        ],
                    )
                values["clocks"] = cls.clock_tables(spelling, value)
        return values

    @field_validator("clocks", mode="before")
    @classmethod
    def _normalize_clocks(cls, clocks):
        return cls.clock_tables("clocks", clocks)

    @classmethod
    def clock_tables(cls, spelling: str, value: Any) -> list[Any]:
        """Return the clocks that `value` gives when it is written as `spelling` of the clock.

        Each clock is the table it stands for. The validators read a clock with this function,
        and so does the merge of a target over its design (`merge_inputs`). `clock: null` and
        `clock: ""` mean no clock. An item that is not a form of a clock is kept unchanged for
        validation to report. Only text or a table is a clock; a number is not. A `clocks` value
        that is neither a list nor one clock raises a `ValueError`."""
        if spelling == "clocks":
            if value is None:
                return []
            if isinstance(value, (str, Mapping, Clock)):
                value = [value]
            if not isinstance(value, list):
                raise ValueError(f"Expecting 'clocks' to be a list but found {value}")
            items = value
        else:
            items = [value]
        # An empty table, like `null` and `""`, says nothing: there is no clock.
        return [
            Clock.as_mapping(item) if isinstance(item, str) else item
            for item in items
            if item is not None and item != "" and not (isinstance(item, Mapping) and not item)
        ]

    @classmethod
    def clock_expected(cls) -> str:
        """Return, in words, what the design's `clock` takes."""
        return "a port name or a table"

    @classmethod
    def clock_mistake(cls, spelling: str, value: Any) -> bool:
        """Return whether `value`, written as `spelling` of the design's clock, is not a clock.

        A clock is a port name or a table (`clock_tables`)."""
        try:
            tables = cls.clock_tables(spelling, value)
        except ValueError:
            return True
        return not all(isinstance(table, (Mapping, Clock)) for table in tables)

    @classmethod
    def form_problems(cls, values: Mapping[str, Any]) -> list[tuple[str, Any]]:
        """Return the clock spellings of `values` that are not a clock, as written.

        `clock` is not a field, so `shape_problems` asks here."""
        return [
            (spelling, values[spelling])
            for spelling in CLOCK_SPELLINGS
            if spelling in values and cls.clock_mistake(spelling, values[spelling])
        ]

    @classmethod
    def merge_inputs(cls, merged: Dict[str, Any], values: Dict[str, Any]) -> None:
        """Merge the design's clock from a higher layer (`values`) into the layer below (`merged`).

        The design's clock is `clock` or `clocks`, two spellings of one list of clocks. Where
        `values` writes one of them and `merged` has one below, the two meet as lists of tables.
        `clocks`, in any form, replaces the list below. `clock` names the first design clock (as
        the `clock` property does) and refines it key by key. `clock: null` means no clock, which
        replaces. A layer that writes two spellings, or a clock that is not a form of a clock,
        stays as it is, so validation reports it. A lower clock that is not a form of a clock is
        kept, and the clock of the higher layer is dropped."""
        written = [name for name in CLOCK_SPELLINGS if name in values]
        below = [name for name in CLOCK_SPELLINGS if name in merged]
        if not written or not below:
            return
        if len(written) > 1:
            for name in below:
                del merged[name]
            return
        if len(below) > 1:
            return
        (name,), (lower,) = written, below

        def tables(spelling: str, value: Any) -> list[Any] | None:
            try:
                clocks = cls.clock_tables(spelling, value)
            except ValueError:
                return None
            return clocks if all(isinstance(clock, Mapping) for clock in clocks) else None

        lowest, higher = tables(lower, merged[lower]), tables(name, values[name])
        if lowest is None:
            del values[name]
            return
        del merged[lower]
        if higher is None:
            return
        del values[name]
        if name == "clocks" or not lowest or not higher:
            merged["clocks"] = higher
        else:
            merged["clocks"] = [hierarchical_merge(dict(lowest[0]), dict(higher[0])), *lowest[1:]]

    @property
    def clock(self) -> Optional[Clock]:
        """The first design clock, or `None` when the design has none."""
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


class CocotbTestbench(XedaBaseModel):
    @classmethod
    def as_mapping(cls, value: Any) -> Any:
        """Return the table that a shorthand value stands for.

        `cocotb: true` is a cocotb testbench with every default, `{}`. `cocotb: false` is no
        cocotb testbench. It is not a table, so it replaces and is replaced."""
        if value is True:
            return {}
        return WHOLE if value is False else None

    module: Optional[str] = None
    toplevel: Optional[str] = None
    testcase: List[str] = Field(
        default=[],
        description="The names of the cocotb test cases to run for this design. The flow "
        "setting `cocotb.testcase` overrides them.",
    )


class TbSettings(DVSettings):
    """The testbench: sources, top modules, unit under test, cocotb, parameters and defines."""

    sources: List[DesignSource] = Field([], description=SOURCES_DESCRIPTION)
    top: Tuple012 = Field(
        tuple(),
        description="The testbench top modules: the primary top module and, optionally, a "
        "secondary one. Give one string, or a list of at most two.",
    )
    uut: Optional[str] = Field(
        None, description="The instance name of the unit under test in the testbench."
    )
    cocotb: Optional[CocotbTestbench] = Field(
        None,
        description="Set this when the testbench is based on the cocotb framework: `true` for "
        "the defaults, or a table of cocotb settings. `false` means no cocotb testbench. A source "
        "of type `Cocotb` sets it automatically.",
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
        form = CocotbTestbench.as_mapping(value)
        if form is WHOLE:
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

        if form is not None or (value is None and has_cocotb(values)):
            return form or {}
        return value


class LanguageSettings(XedaBaseModel):
    standard: Optional[str] = Field(
        None,
        description="The version of the language standard.",
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
    def as_mapping(cls, value: Any) -> Any:
        """Return the table that a standard written alone stands for.

        `vhdl: "08"` means `vhdl: {standard: "08"}`. A bool is not a standard."""
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        return {"standard": cls.two_digit_standard(value)}


class VhdlSettings(LanguageSettings):
    synopsys: bool = False


class Language(XedaBaseModel):
    vhdl: VhdlSettings = VhdlSettings()  # type: ignore
    verilog: LanguageSettings = LanguageSettings()  # type: ignore

    @field_validator("verilog", "vhdl", mode="before")
    @classmethod
    def _language_settings(cls, value, info):
        model = {"vhdl": VhdlSettings, "verilog": LanguageSettings}.get(info.field_name)
        expanded = model.as_mapping(value) if model is not None else None
        return value if expanded is None else expanded


class RtlDep(XedaBaseModel):
    pos: int = 0


class TbDep(XedaBaseModel):
    pos: int = 0


#: Where a Git dependency without a `clone_dir` is cloned, under the run root.
DEPENDENCY_CLONES = ".dependencies"


@dataclass(frozen=True)
class LoadContext:
    """What a launcher lends to the designs it loads: access to the run root, and what the launch
    asked for.

    `run_root(True)` creates and marks the run root. `run_root(False)` returns the run root if it
    is already there, and `None` if not. A load asks for a run root to create only to keep
    something there, never to look for one. So a load that keeps nothing creates nothing, and a
    pure plan never asks.

    A load keeps two things in the run root: the directory that a Git dependency without a
    `clone_dir` is cloned into (`DEPENDENCY_CLONES`) and the record of the last generation of a
    generator (`generation.CACHE_DIRECTORY`). Both outlive a design that fails after them,
    because the work they record was done."""

    run_root: Callable[[bool], Optional[Path]]
    #: `--rebuild-all` (which `--clean` implies). A generator runs whatever its record says.
    rebuild_all: bool = False


#: How a design load reaches the run root. The launcher sets it around the loading of its designs
#: (`loading_in_run_root`), and it is unset elsewhere.
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


@dataclass
class DeferredLoad:
    """What a design load left undone (see `deferring_load_side_effects`).

    It lists the generators that the load did not run and the Git dependencies that it did not
    fetch, each described, and says whether the loaded design still equals the design that a full
    load gives (`complete`)."""

    deferred: List[str] = field(default_factory=list)
    #: False once the load left out something that only the deferred work can tell: the sources,
    #: top module and testbench of a Git dependency, or a source pattern whose files have no
    #: type before the generator writes them.
    complete: bool = True

    def defer(self, what: str, complete: bool = True) -> None:
        self.deferred.append(what)
        self.complete = self.complete and complete

    def failure_may_depend_on_deferred_output(self, error: BaseException) -> bool:
        """Return whether a load that failed with `error` may succeed once the deferred work ran.

        That holds when something was deferred and every error is a file that the design names
        and that does not exist (`MissingFile`), which the deferred work may write. Any other
        error (a malformed source, a wrong value) is also an error of the full load, and the full
        load would run the generator first."""
        return (
            bool(self.deferred)
            and isinstance(error, DesignValidationError)
            and error.only_missing_files
        )


_deferred_load: ContextVar[Optional[DeferredLoad]] = ContextVar("deferred_load", default=None)


@contextmanager
def deferring_load_side_effects() -> Iterator[DeferredLoad]:
    """Load designs without running a generator or fetching a Git dependency, and report what
    was left undone.

    A load with nothing to defer is a full load. A load with a deferred generator gives the
    declared design: the sources that the generator writes are listed as the design declares
    them, with their types, whether or not they exist yet (as `path` entries). So whatever needs
    only the design's declarations can be judged before the generator runs. Nothing is run,
    fetched, created or written. The generator's tree is locked as for any load, except in a
    plan."""
    record = DeferredLoad()
    token = _deferred_load.set(record)
    try:
        yield record
    finally:
        _deferred_load.reset(token)


def _declared_sources(sources: Any, record: DeferredLoad) -> Any:
    """Return `rtl.sources` as the design declares them, for a load that defers their generator.

    Every file is a `path` entry, which need not exist yet, with its type. A pattern stands for
    the files that exist once the generator has run, so it is left out and the load is
    incomplete."""
    sources = as_source_list(sources)
    if not isinstance(sources, (list, tuple)):
        return sources  # the sources validator reports it
    declared: List[Any] = []
    for src in sources:
        if isinstance(src, (str, os.PathLike)):
            if isinstance(src, str) and _is_source_pattern(src):
                # The files it stands for (their number, and a duplicate of a file listed
                # beside it) are known once the generator has written them.
                record.complete = False
                continue
            declared.append({"path": str(src)})
        elif isinstance(src, Mapping) and "file" in src and "path" not in src:
            declared.append({"path": src["file"], **{k: v for k, v in src.items() if k != "file"}})
        else:
            declared.append(src)
    return declared


@contextmanager
def loading_in_run_root(
    provider: Callable[[bool], Optional[Path]], rebuild_all: bool = False
) -> Iterator[None]:
    """Let the designs loaded meanwhile keep what a load keeps in a run root (a Git dependency's
    clone, a generator's record) in the run root that `provider` names.

    A load asks `provider` only when something is kept there, and only then may the run root be
    created. With `rebuild_all`, a generator runs whatever its record says, as every flow of the
    launch does."""
    token = load_context.set(LoadContext(provider, rebuild_all))
    try:
        yield
    finally:
        load_context.reset(token)


class DesignReference(XedaBaseModel):
    uri: str
    rtl: RtlDep = RtlDep()
    tb: TbDep = TbDep()
    #: The directory where a Git dependency is cloned (`<local_cache>/<host>/<name>`, see
    #: `clone_name_parts`) when it names no `clone_dir`. When unset, a launcher clones into its
    #: run root (`loading_in_run_root`). A directory that the user configures here is the user's
    #: to direct: Xeda clones and pulls there.
    local_cache: Optional[Path] = None

    @field_validator("uri")
    @classmethod
    def _a_plain_reference_is_a_design_file(cls, value: str) -> str:
        """Refuse a URL, because a reference of this class names a design file on disk.

        A URL names a Git repository, which is written `git+<url>`. The error names that
        spelling, so the URL is not taken for a path that does not exist."""
        if cls is DesignReference and _URL_SCHEME.match(value):
            shown = redacted_url(value)
            raise ValueError(
                f"the design dependency {shown!r} is a URL, not the path of a design file: "
                f"write a Git repository as `git+{shown}`"
            )
        return value

    def __repr_args__(self) -> Iterator[tuple[Optional[str], Any]]:
        """Yield the one text form of a reference, for `repr`, `str`, f-strings, log lines and
        every container that holds one.

        The form shows the URL without the credentials (`redacted_url`). The fields keep the URL
        as written, because the clone needs it."""
        for name, value in super().__repr_args__():
            if name in ("uri", "repo_url") and isinstance(value, str):
                value = redacted_url(value)
            yield name, value

    @staticmethod
    def from_data(data) -> DesignReference:
        if isinstance(data, DesignReference):
            return data
        if isinstance(data, str):
            data = dict(uri=data)
        elif isinstance(data, Mapping):
            # The URI form is normalized below by removing the `git+` discriminator. Keep that
            # normalization local: dependency dictionaries commonly come straight from the
            # caller's parsed design document, and must not be rewritten as a side effect of
            # constructing a model.
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
            raise MissingFile(f"file {redacted_url(str(design_path))} does not exist!")
        return Design.from_file(design_path)


#: The number of hexadecimal digits of the digest of what is cloned, which end the name of its
#: directory.
CLONE_DIGEST_LENGTH = 16
#: The most characters that a readable part of a clone directory's name keeps. A readable part is
#: a host, or a repository path with its branch or commit.
CLONE_NAME_LIMIT = 80
#: A drive (`C:`) at the start of a path component. On Windows, it replaces the path before it.
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


#: The user name and password of a URL's authority (`scheme://user:password@host`), up to its
#: last `@`.
_URL_USERINFO = re.compile(r"(?<=//)[^/?#]*(?=@)")


#: The start of a URL: a scheme and `//`. A design file's path has none.
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def redacted_url(url: str) -> str:
    """Return `url` as a log line or an error message may show it.

    The user name and the password of its authority are replaced by `***`. A token is given as
    either, so neither is kept."""
    return _URL_USERINFO.sub("***", url)


def _shown_dependencies(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return `data`, a design's mapping, as a log line may show it.

    The URL of each dependency (a string, or the `uri` or `repo_url` of a mapping) has no
    credentials."""
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, (list, tuple)):
        return data

    def shown(entry: Any) -> Any:
        if isinstance(entry, str):
            return redacted_url(entry)
        if isinstance(entry, Mapping):
            return {
                key: (
                    redacted_url(value)
                    if key in ("uri", "repo_url") and isinstance(value, str)
                    else value
                )
                for key, value in entry.items()
            }
        return entry

    return {**data, "dependencies": [shown(entry) for entry in dependencies]}


def _clone_name_text(what: str, text: str, *, drive: bool = True) -> str:
    """Refuse text that names a place outside the directory it is joined onto: a `.` or `..`
    component, a drive such as `C:` at the start of a component, a leading `/`, a backslash or
    a NUL. A path that is absolute, or drive-qualified, replaces the one it is joined onto.
    `drive=False` leaves out the drive: a host and its port (`h:8443`) are one component."""
    if not isinstance(text, str):
        raise ValueError(f"the Git {what} must be a string, not {text!r}")
    parts = text.split("/")
    if (
        text.startswith("/")
        or "\\" in text
        or "\0" in text
        or any(part in (".", "..") or (drive and _DRIVE_PREFIX.match(part)) for part in parts)
    ):
        raise ValueError(
            f"the Git {what} {text!r} has a `.` or `..` component, a drive such as `C:`, a "
            "leading `/`, a backslash or a NUL: it would name a directory outside the clone cache"
        )
    return text


def _clone_token(text: str) -> str:
    """Return `text` as one readable, filename-safe part of a directory name.

    Each run of characters other than ASCII letters, digits, `.` and `-` becomes one `_`.
    Nothing starts or ends with `.` or `_`, and no more than `CLONE_NAME_LIMIT` characters are
    kept."""
    folded = re.sub(r"[^A-Za-z0-9.-]+", "_", text).strip("._")
    return folded[:CLONE_NAME_LIMIT].rstrip("._")


def clone_name_parts(
    repo_url: str, commit: Optional[str], branch: Optional[str]
) -> tuple[str, str]:
    """Return the names of the two directories that a Git reference is cloned into,
    `<host>/<name>`.

    `<name>` is the repository path and the commit (or else the branch), folded into one
    readable token, then `_` and a digest of what is cloned: the repository URL, the branch and
    the commit. Folding cannot keep two paths apart (`a/b` and `a_b` read alike), so the digest
    does. Each reference gets its own directory, and the same reference always gets the same
    one. The directory is one level below its host's, so no clone lies inside another.

    `_clone_name_text` refuses a host, path, commit or branch that names a place outside the
    cache, before it is folded. The path must not be empty. The host and its port (`h:8443`)
    fold into one token, so a host that reads as a drive is not refused. The user name and the
    password of a URL (`https://user:password@host/...`) are not part of a name, because a name
    shows in paths and logs. The digest still covers the whole URL, so URLs that differ only in
    those are cloned apart.
    """
    uri = urlparse(repo_url)
    if not uri.netloc:
        raise ValueError(f"invalid URL: {redacted_url(repo_url)}")
    host = _clone_name_text("host", uri.netloc.rpartition("@")[2], drive=False)
    path = _clone_name_text("repository path", uri.path.lstrip("/"))
    if not path:
        raise ValueError(f"the Git URL {redacted_url(repo_url)!r} names no repository path")
    if commit:
        reference = "commit=" + _clone_name_text("commit", commit)
    elif branch:
        reference = _clone_name_text("branch", branch)
    else:
        reference = ""
    identity = json.dumps([repo_url, branch or None, commit or None])
    digest = hashlib.sha256(identity.encode()).hexdigest()[:CLONE_DIGEST_LENGTH]
    readable = _clone_token(f"{path}_{reference}")
    name = f"{readable}_{digest}" if readable else digest
    return _clone_token(host) or "host", name


def clone_location(
    cache: Union[str, Path],
    repo_url: str,
    commit: Optional[str],
    branch: Optional[str],
    *,
    owner: Optional[RunDirectory] = None,
) -> Path:
    """Return where a Git reference is cloned: `<cache>/<host>/<name>` (`clone_name_parts`).

    The location lies inside `cache`, and a name that would leave it is refused. A cache under a
    run root is named through its `owner` (the run root as a `RunDirectory`) by
    `RunDirectory.unlinked`, the one rule that every cache there follows: inside the run root and
    reached through no symbolic link. A cache that the user named (`local_cache`) is the user's
    to direct, so only the names are checked against it.
    """
    host, name = clone_name_parts(repo_url, commit, branch)
    location = Path(cache) / host / name
    base = os.path.abspath(cache)
    inside = os.path.abspath(location)
    try:
        contained = inside != base and os.path.commonpath([base, inside]) == base
    except ValueError:  # another drive, as on Windows
        contained = False
    if not contained:
        raise ValueError(
            f"{redacted_url(repo_url)} would be cloned to {location}, outside the clone cache "
            f"{cache}"
        )
    return owner.unlinked(location) if owner is not None else location


class GitReference(DesignReference):
    """A design file in a Git repository, named by a URI.

    The URI is `<scheme>://<host>[:port]/<path>.git[?branch=<branch>&commit=<commit>]#<file>`,
    where `<file>` is the path of the design file in the repository. Both query parameters are
    optional. A design file writes the URI with a `git+` prefix. For example:
    `https://github.com/GMUCERG/TinyJAMBU-SCA.git?branch=dev#./TinyJAMBU-DOM1-v1.toml`.
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
                raise ValueError(f"invalid git URL: {redacted_url(uri_str)}")
            # The design file path is relative to the repository root.
            design_file_path = uri.fragment.lstrip("/.")  # Removes /, ../, etc.
            if not design_file_path:
                raise ValueError(
                    inspect.cleandoc(
                        """path to design_file must be specified using URL fragment (#...), e.g.,
                    https://github.com/SOME_USERNAME/SOME_REPOSITORY.git#PATH_TO_DESIGN_FILE
                    when design file is 'sub_dir1/design_file2.toml'
                    relative to the repository's root."""
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

        # Build the explicit keys first and let the caller's own values win.
        # `dict(repo_url=..., **values)` would raise `TypeError: got multiple values for keyword
        # argument 'repo_url'` for the mapping form that `DesignReference.from_data()` selects
        # when it sees a `repo_url` key.
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
            # The mapping form (`{repo_url: ..., design_file: ...}`) that
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
                    f"{redacted_url(self.repo_url)} needs a directory to be cloned into: give "
                    "its `clone_dir` (or `local_cache`), or load the design through xeda run / "
                    "a launcher, which clones into its run root"
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
                redacted_url(self.repo_url),
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

#: The top-level shorthands that `Design.process_compatibility` folds into `rtl`. A design file
#: (and a target) may write them at its root.
FLAT_RTL_KEYS = (
    "sources",
    "top",
    "clock",
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


def _flows_table(value: Any) -> dict[str, Any]:
    """Return the `flows` table of a design, or of a target's overlay, as the design holds it.

    A dotted key is a section's setting, and a section left empty (`None`) is dropped. A table of
    the wrong shape (`flows_table_problems`) raises a `ValueError` that names it. The design
    validator, the judging of the design before a target is merged into it, and every target's
    overlay all read a table with this function."""
    if isinstance(value, Mapping):
        value = settings_to_dict(value)
    problems = flows_table_problems(value)
    if problems:
        raise ValueError("; ".join(f"`{path}` {problem}" for path, problem in problems))
    return {k: v for k, v in (value or {}).items() if v is not None}


def _known_flow(name: str) -> Any:
    """Return the registered flow that `name` stands for (as the command line takes it), or None."""
    importlib.import_module("xeda.flows")
    return importlib.import_module("xeda.flow_runner.settings_layers").registered_flow(name)


def _tables_by_flow(table: Mapping[str, Any]) -> dict[str, Any]:
    """Return a `flows` table with each flow under its canonical name.

    A name given twice and the name of a removed flow raise the errors of `merge_flow_sections`."""
    layers = importlib.import_module("xeda.flow_runner.settings_layers")
    layers.merge_flow_sections(table, flow_class_for=_known_flow)  # raises on those mistakes
    by_flow = {}
    for name, section in table.items():
        flow_cls = _known_flow(name)
        by_flow[flow_cls.name if flow_cls is not None else name] = section
    return by_flow


def _flow_section_problems(
    table: Mapping[str, Any],
) -> list[tuple[tuple[str, ...], Any]]:
    """Return the paths `(flow, setting, ...)` where a section of `table` holds a wrong value.

    `table` is a `flows` table. A value is wrong when it is not a table at a setting that takes
    one. A flow that is not registered is not judged."""
    problems = []
    for name, section in _tables_by_flow(table).items():
        flow_cls = _known_flow(name)
        if flow_cls is not None and isinstance(section, Mapping):
            problems += [
                ((name, *path), v) for path, v in shape_problems(flow_cls.Settings, section)
            ]
    return problems


def target_name_problem(name: Any) -> str | None:
    """Return why `name` cannot name a target, or None.

    A target's run directories are `<run root>/<design>/<name>/<flow>`. So the name is a name (as
    a design's is) that is neither a flow's name nor the name of a flow's run directory, because
    the flows' own directories (`<flow>`, or `<flow>_<hash>` with hashed run directories) lie
    beside it. The loader and the launcher's path boundary (`DefaultRunner.run_path_of`) judge a
    name by this rule, and `xeda scrub` uses it to tell a target's directory from a run
    directory."""
    if not isinstance(name, str) or not DESIGN_NAME.fullmatch(name):
        return (
            f"{name!r} is not a target name: it names the target's run directories, so it "
            "starts with a letter and holds only letters, digits, `_` and `-`"
        )
    flow = _flow_named(name)
    if flow is not None:
        return (
            f"{name!r} is the name of a flow, and a target's run directories lie beside "
            "the flows' own: give the target another name"
            if flow == _fold(name)
            else f"{name!r} is the name of a run directory of the flow `{flow}` "
            f"(`{flow}_` and a {DIR_NAME_HASH_LEN}-character hash), and a target's run directories "
            "lie beside the flows' own: give the target another name"
        )
    return None


def _fold(name: str) -> str:
    """Return `name` as the command line takes a flow's name.

    Dashes become underscores and the text is lower case."""
    return name.replace("-", "_").lower()


def _flow_named(name: str) -> str | None:
    """Return the flow whose name `name` is, or whose run directory's name `name` is, else None.

    A run directory's name is the flow's name, an underscore and a hash
    (`run_dir.run_directory_name`). A flow is a registered flow's name or alias, or a removed
    flow's name (`settings_layers.REMOVED_FLOWS`), as the command line takes it (dashes become
    underscores, any letter case)."""
    # Import here and by name: the flows import this module and are registered by being
    # imported.
    importlib.import_module("xeda.flows")
    registered_flows = importlib.import_module("xeda.flow").registered_flows
    # A removed flow's name is a flow's name too: its run directories may still be under
    # `<design>/`.
    removed = importlib.import_module("xeda.flow_runner.settings_layers").REMOVED_FLOWS
    flows = {_fold(flow) for flow in registered_flows} | set(removed)

    folded = _fold(name)
    # A flow's run directories are named by the rule that a launch and scrub both apply.
    return next((flow for flow in sorted(flows) if run_directory_name(flow).match(folded)), None)


class Design(XedaBaseModel):
    name: str = Field(
        description="The unique name of the design. It starts with a letter and has only "
        "letters, digits, `_` and `-`. Name regex: [a-zA-Z][a-zA-Z0-9_\\-]*."
    )

    design_root: Optional[Path] = Field(None, json_schema_extra={"hidden_from_schema": True})
    description: Optional[str] = Field(None, description="A brief description of the design.")
    authors: List[str] = Field(
        [],
        alias="author",
        description='The authors of the design, each in "Name <email>" format (the RFC 5322 '
        'mailbox format), for example ["Jane Doe <jane@example.com>", '
        '"John Doe <john@example.com>"].',
    )
    # `SerializeAsAny`: a dependency is usually a `GitReference`, and pydantic 2 serializes a
    # nested model by its annotated type unless told otherwise. That would silently drop
    # `repo_url`, `design_file`, `branch`, `commit` and the clone settings that `send_design()`
    # relies on.
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
        description="Design-specific flow settings. Each key is the name of a flow, and each "
        "value is the design's settings for that flow.",
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

    #: A design whose `flows` is `null` has none.
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"flow": unspecified}

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
        return _flows_table(value)

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
        """Fold the flat top-level form into `rtl` and `tb`.

        The same function folds a target's overlay, with `defaults=False`: only the keys that the
        overlay wrote, so it overrides no more than it says."""
        if "rtl" in data:
            # Beside an `rtl` section the flat keys are not folded, but a removed one still says
            # what replaced it, in place of "extra inputs are not permitted".
            for _, message in RtlSettings.removed_input_problems(data):
                raise ValueError(message)
        else:
            # A removed input is folded too: `rtl` is where its removal is reported.
            given = {
                name: data.pop(name)
                for name in (*FLAT_RTL_KEYS, *RtlSettings.removed_inputs)
                if name in data
            }
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
        """Return the design that `data` describes once one of its `targets` is selected.

        The result is an ordinary design with the target's name recorded as `target`.

        A target is an overlay: it takes the keys of the design file itself (all but
        `TARGET_FORBIDDEN_KEYS`), folded as the design's own are (`process_compatibility`) and
        merged over them. Mappings merge key by key at every depth; `rtl.sources` and
        `tb.sources` are appended after the design's; any other list replaces the design's.

        With one target and no `target` given, that target is selected. Without `targets`, the
        data is returned as it is, and naming a `target` is an error. A design cannot write
        `target`, and `targets: null` is invalid. Every problem is a
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
        # The flows table is judged first, as the design validator judges it: the design is as
        # valid as it is, whichever target is selected.
        for key, value in base.items():
            if input_names(cls).get(key) == "flow":
                try:
                    _flows_table(value)
                except ValueError as e:
                    raise invalid(key, str(e)) from e
        try:
            merged = cls._merge_target(base, overlay, append=True)
        except (ValueError, XedaException) as e:
            raise invalid("flows", str(e)) from e
        return {**merged, "target": target}

    @classmethod
    def _merge_target(
        cls, base: Mapping[str, Any], overlay: Mapping[str, Any], *, append: bool
    ) -> dict[str, Any]:
        """Return the design `base` with an overlay (a target, or the `--design-overrides`)
        merged over it, both folded as `process_compatibility` folds them.

        One rule serves every key: the rule that merges the settings layers of a flow
        (`settings_layers.merge_layers`). Each side is read as the tables that its shorthands
        stand for (a clock as its port, `parameters` as a list of objects, `vhdl` as its
        standard) and under the field names that its aliases stand for. Tables merge key by key
        at every depth, and everything else that the overlay writes replaces. A target passes
        `append`: its sources come after the design's, because it adds the files of a board. The
        sources of any other overlay replace the design's. Each flow's section is merged by that
        flow's own rules, including an alias and a clock shorthand. A value of the design that is
        not a table where one is expected is kept as it is, with nothing of the overlay merged
        over it, so validation reports it as it would with no target."""
        layers = importlib.import_module("xeda.flow_runner.settings_layers")
        base, overlay = canonical_tree(cls, base), canonical_tree(cls, overlay)
        flows: list[dict[str, Any] | None] = []
        for side in (base, overlay):
            names = [key for key in side if input_names(cls).get(key) == "flow"]
            flows.append(_flows_table(side.pop(names[0])) if len(names) == 1 else None)
        low, high = flows
        merged = layers.merge_layers(base, overlay, settings_cls=cls, append=append)
        if low is None and high is None:
            return merged
        low_by_flow = _tables_by_flow(low or {})
        high_by_flow = _tables_by_flow(high or {})
        merged_flows: dict[str, Any] = {}
        for name in dict.fromkeys([*low_by_flow, *high_by_flow]):
            below, above = low_by_flow.get(name), high_by_flow.get(name)
            flow_cls = _known_flow(name)
            if below is None or above is None:
                merged_flows[name] = deepcopy(below if above is None else above)
                continue
            settings_cls = flow_cls.Settings if flow_cls is not None else None
            merged_flows[name] = layers.merge_layers(below, above, settings_cls=settings_cls)
        merged["flow"] = merged_flows
        return merged

    @classmethod
    def _target_overlay(cls, name: Any, overlay: Any) -> dict[str, Any]:
        """Check and fold one target's overlay, as a design's own top level is."""
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
        try:
            overlay = expand_hierarchy(dict(overlay))
        except ConflictingKeys as e:
            raise invalid(None, str(e)) from e
        known = {*input_names(cls), *FLAT_RTL_KEYS, "test", "tests"} - TARGET_FORBIDDEN_KEYS
        for name, message in RtlSettings.removed_input_problems(overlay):
            raise invalid(name, message)
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
        for key, value in overlay.items():
            if input_names(cls).get(key) == "flow":
                try:
                    _flows_table(value)
                except ValueError as e:
                    raise invalid(key, str(e)) from e
        try:
            folded = cls.process_compatibility(overlay, defaults=False)
        except ValueError as e:
            raise invalid(None, str(e)) from e
        if isinstance(folded.get("rtl"), Mapping):
            for name, message in RtlSettings.removed_input_problems(folded["rtl"]):
                raise invalid(f"rtl.{name}", message)
        # A key that a source table does not take (a removed key included) is also the target's
        # own mistake, reported whether or not the target is selected.
        for part in ("rtl", "tb"):
            section = folded.get(part)
            entries = (
                as_source_list(section.get("sources")) if isinstance(section, Mapping) else None
            )
            for entry in entries if isinstance(entries, (list, tuple)) else ():
                for message in source_table_problems(entry) if isinstance(entry, Mapping) else ():
                    raise invalid(f"{part}.sources", message)
        # A value that is not a table where one is expected is the target's own mistake,
        # reported where it is written, whether or not the target is selected.
        for path, value in shape_problems(cls, folded):
            if input_names(cls).get(path[0]) != "flow":  # a `flows` table is judged on its own
                # This is the design's clock, not any key of that name (an `rtl.attributes`
                # entry).
                takes = (
                    RtlSettings.clock_expected()
                    if path == ("rtl", "clock")
                    else "a table or a short form of one"
                )
                raise invalid(".".join(path), f"takes {takes}, not {value!r:.60}")
        for key, value in folded.items():
            if input_names(cls).get(key) == "flow":
                for path, value in _flow_section_problems(_flows_table(value)):
                    raise invalid(
                        ".".join((key, *path)),
                        f"takes a table or a short form of one, not {value!r:.60}",
                    )
        return folded

    @classmethod
    def process_generation(cls, data: Dict[str, Any]):
        """Run declared generators and collect their produced sources."""
        design_root = data.get("design_root")
        if not design_root:
            design_root = Path.cwd()
        else:
            design_root = Path(design_root)
        # Normalize before changing the working directory: direct `Design` constructors accept
        # relative roots too. Resolving inside the lease after `WorkingDirectory` would rebase
        # the root a second time.
        design_root = design_root.resolve()
        rtl = data.get("rtl", {})
        assert isinstance(rtl, dict), f"rtl must be a dictionary, but found {type(rtl)}"
        generator = rtl.pop("generator", None)
        deferring = _deferred_load.get()

        def defer(what: str) -> None:
            """Leave the generator for the load that follows the declared one."""
            assert deferring is not None
            deferring.defer(what)
            rtl["sources"] = _declared_sources(rtl.get("sources", []), deferring)

        if generator:
            if _planning_load.get():
                generator_lease: AbstractContextManager[None] = nullcontext()
            else:
                from .flow_runner.run_lock import generator_design_lock

                generator_lease = generator_design_lock(design_root)
            with WorkingDirectory(design_root), generator_lease:
                # Tell the generator the design root as `$DESIGN_ROOT` names it in the design's
                # own paths. This replaces whatever the shell exports, which is another
                # directory's.
                env = {**os.environ, "DESIGN_ROOT": str(design_root)}
                if isinstance(generator, str):
                    if deferring is not None:
                        return defer(f"generator command `{generator}`")
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

                    # Judge it before the defaults below complete it: its identity is the
                    # configuration that the design states, never the environment of this shell.
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
                        read_only=deferring is not None,
                        rebuild_all=bool(context and context.rebuild_all),
                    ) as generation:
                        if generation.reason is None:
                            log.info(
                                "Not running %s: its generated sources are what its "
                                "last generation left",
                                description,
                            )
                        else:
                            if deferring is not None:
                                return defer(description)
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
                    if deferring is not None:
                        return defer(f"generator `{_describe_generator(generator)}`")
                    if _planning_load.get():
                        raise ValueError("Cannot plan a design that needs a generator")
                    args = generator
                    subprocess.run(
                        args,
                        check=True,
                        cwd=design_root,
                        env=env,
                        stdout=tool_output_redirect(),
                    )
                # Whatever the design declares as a source must exist now. Expand it again and do
                # not reuse the result of the skip check above, because the generator is what a
                # pattern was waiting for. A source written as `path` skips the check of the
                # sources validator. Without this check, a generator that produced nothing would
                # fail much later, with an errno from inside the design hash.
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
                    # Check here, where the tree is complete: a pattern of `rtl.sources` that the
                    # generator is still to fill matches nothing before the generator has run.
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
        log.debug("Design data: %s", _shown_dependencies(data))
        cls.process_generation(data)
        return data

    def __init__(
        self,
        design_root: Union[str, os.PathLike, None] = None,
        **data: Any,
    ) -> None:
        """Copy caller data before compatibility and source processing."""
        # Compatibility processing, generators and source normalization all consume or enrich
        # nested mappings. The data of the caller is input, not a workspace that Xeda owns.
        data = deepcopy(data)
        if not design_root:
            design_root = data.pop("design_root", Path.cwd())
        if not design_root:
            raise ValueError("design_root is not set")

        # Everything up to `super().__init__` runs outside the validators of pydantic, so this
        # code turns a malformed design into a `DesignValidationError` by hand. Otherwise
        # `rtl: [x]` or a mistyped `design_root` would escape as a bare `AssertionError`,
        # `FileNotFoundError` or `TypeError` traceback, and the error would not name the
        # offending key.
        def invalid(
            loc: Optional[str], msg: str, kind: str = "value_error"
        ) -> DesignValidationError:
            return DesignValidationError([(loc, msg, "", kind)], data=data, design_root=design_root)

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
                    _design_errors(e.errors()), data=data, design_root=design_root
                ) from e

            deferring = _deferred_load.get()
            for dep in self.dependencies:
                if deferring is not None and isinstance(dep, GitReference):
                    # Its sources, top module and testbench are known only once it is fetched.
                    deferring.defer(f"Git dependency {redacted_url(dep.repo_url)}", complete=False)
                    continue
                try:
                    dep_design = dep.fetch_design()
                except ValueError as e:
                    kind = MISSING_FILE_ERROR if isinstance(e, MissingFile) else "value_error"
                    raise invalid("dependencies", str(e), kind) from e
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
                # Assign the list and do not edit it in place, so the merge is validated like
                # sources given in any other way: a file that the design and the dependency both
                # list is compiled once.
                self.rtl.sources = sources
                if not self.tb.sources and dep_design.tb.sources:
                    self.tb.sources = dep_design.tb.sources
                if not self.tb.top and dep_design.tb.top:
                    self.tb.top = dep_design.tb.top
            # The merge consumes the dependencies: the design now holds their sources itself, and
            # it is recorded as built. A record that kept the dependencies as well would merge
            # them again on every reload (a `settings.json`, the archive that `send_design`
            # ships) and duplicate sources.
            if self.dependencies:
                self.dependencies = []

    def header_dirs(self, rtl: bool = True, tb: bool = False) -> List[Path]:
        """Return the directory of every Verilog or SystemVerilog header that the design lists.

        Each directory appears once, in source order. A tool needs these directories as the
        include search path (`-I`, `+incdir+`) to find the headers."""
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
    def remove_keys(cls, data: dict[str, Any], names: Iterable[str]) -> None:
        """Remove the keys `names` from the design data `data`, each in every spelling.

        A field is one key, whichever alias or field name the data uses (`flows` and `flow`)."""
        spelled = input_names(cls)
        fields = {spelled.get(name, name) for name in names}
        for key in [key for key in data if spelled.get(key, key) in fields]:
            del data[key]

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
        """Load and validate a design from a TOML, JSON or YAML file.

        `target` selects one of the design's `targets` (`select_target`); a design with one
        target needs none. `overrides` are applied last, over the selected target.

        A file that cannot be read as a design raises `DesignFileParseError`. A file whose design
        does not validate raises `DesignValidationError`. Both errors name the file.
        """
        if overrides is None:
            overrides = {}
        if remove_extra is None:
            remove_extra = []
        if not isinstance(design_file, Path):
            design_file = Path(design_file)
        design_dict = _read_design_file(design_file)
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
                cls.remove_keys(design_dict, remove_extra)
            # The default design root is the directory that holds the design file.
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
    def expanded(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        """Return `data` (a design or an overlay on one) with each dotted key expanded.

        A dotted key becomes the tables it stands for (`tb.top: x` means `tb: {top: x}`). This is
        the only place that expands a written design, whether it came from a design file, a
        project's `designs` entry or a caller's overrides. A key given as a value and as a table
        raises a `DesignValidationError`."""
        try:
            return expand_hierarchy(dict(data))
        except ConflictingKeys as e:
            raise DesignValidationError(
                [(e.value_key, str(e), "", "value_error")], data=dict(data)
            ) from e

    @classmethod
    def target_selected(
        cls,
        data: Mapping[str, Any],
        target: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the design as written in a file (a design file, or a project's `designs` entry),
        with its target selected and then `overrides` applied.

        Every loader of written designs builds the `Design` from this result. Written input has
        no `target` key, because the loader records the name there. The overrides are an overlay
        on the design, like a target, and merge by the same rule (`_merge_target`), except that
        their `sources` replace the design's: only a target adds its sources to the design's."""
        selected = cls.select_target(cls.expanded(data), target)
        overrides = cls.expanded(deepcopy(dict(overrides or {})))
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
        if not overrides:
            return selected
        try:
            # Fold both as a target and its design are folded, so a flat key means the same in each.
            overrides = cls.process_compatibility(overrides, defaults=False)
            return cls._merge_target(
                cls.process_compatibility(deepcopy(selected)), overrides, append=False
            )
        except (ValueError, XedaException) as e:
            raise DesignValidationError(
                [("flows", str(e), "", "value_error")], data=dict(data)
            ) from e

    def source_path_as_named(self, src: FileResource) -> Path:
        """Return `src` as this design names it.

        The path is relative to the design root when `src` is under it, and otherwise the path
        that the design file wrote."""
        if src.file and self.design_root:
            try:
                return src.file.absolute().relative_to(self.root_path.absolute())
            except ValueError:
                pass
        return src.get_specified_path()

    def source_artifact_name(self, src: FileResource, suffix: str = "") -> str:
        """Return a filename-safe name for something that a flow writes about one source.

        Sources are distinct files, so their paths already tell them apart. A flow that writes
        one artifact per source (GHDL converting each VHDL file to its own Verilog file) must
        fold the path into a single filename. A stem alone does not work: `rtl/a/fifo.vhd` and
        `rtl/b/fifo.vhd` would both give `fifo.v`, and one would overwrite the other.

        Folding a path into one filename cannot be both readable and injective. These pairs fold
        alike: `my-fifo.vhd` and `my_fifo.vhd`, `fifo.vhd` and `fifo.vhdl`, and a dependency's
        `rtl/fifo.vhd` (named relative to its own root) beside this design's. So a name that
        another of the design's sources would also get carries a short digest of the file's
        path, and every other name is only the fold. Distinct sources get distinct names by
        construction, and a source gets the same name whichever of its lists it is found
        through.

        This is naming only. Nothing here may be read back as design state.
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
        """Return the path of `src`, as this design names it, as one filename-safe token."""
        parts = (
            re.sub(r"\W+", "_", part).strip("_")
            for part in self.source_path_as_named(src).with_suffix("").parts
        )
        return "_".join(part for part in parts if part)

    @property
    def rtl_fingerprint(self) -> Dict[str, Any]:
        """Location-independent inputs that can change RTL compilation or synthesis.

        Source order is significant (notably for VHDL), and the source type tells tools how to
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
        """The location-independent testbench inputs that identify a run."""
        return {
            "sources": [self._source_fingerprint(src) for src in self.tb.sources],
            "parameters": self._parameters_fingerprint(self.tb),
            "defines": dict(self.tb.defines),
            "top": self.tb.top,
            "uut": self.tb.uut,
            "cocotb": self.tb.cocotb.model_dump() if self.tb.cocotb is not None else None,
        }

    def _parameters_fingerprint(self, dv: DVSettings) -> Dict[str, Any]:
        """Return the parameter values as the tool sees them.

        A path under the design root counts relative to it (`$DESIGN_ROOT/rom.mem`), the rule
        that `flowrun_hash` applies to settings. So a design that is given a file under its root
        keeps its identity wherever it is moved. A path outside the root is the location that
        the design names, and counts as such.
        """
        roots = [("DESIGN_ROOT", self.root_path)] if self.design_root else []
        return {k: location_free(v, roots) for k, v in dv.parameters.items()}

    def relative_to_root(self, path: Union[str, os.PathLike]) -> Optional[Path]:
        """Return `path` relative to the design root, or None when it is not an absolute path
        under the root.

        It tells whether a path is part of the design's own tree, by the rule that the
        fingerprint uses (`location_free`). The root is resolved when the design is built, and so
        is every path that a design resolves against it."""
        path = Path(path)
        if self.design_root and path.is_absolute() and path.is_relative_to(self.root_path):
            return path.relative_to(self.root_path)
        return None

    def _source_fingerprint(self, source: DesignSource) -> Dict[str, Any]:
        """Return what identifies a source: its content, its type and its path relative to the
        design root.

        A source's location may affect include resolution, commands in a sourced constraint or
        script, and tool lookup. Counting every declared source by path avoids reusing a cached
        result for a different layout.

        The path is always relative to the root, outside it too (`../lib/defs.vh`). A lookup by
        place depends on where files sit relative to each other, so moving a design together with
        what it names keeps its identity, and so does any spelling of the path.
        """
        return {
            "content": source.content_hash,
            "type": str(source.type) if source.type is not None else None,
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

    def parts_hash(
        self, parts: Iterable[str], source_types: Collection[SourceType] | None = None
    ) -> str:
        """Return the hash of the design's `parts` (`DESIGN_PARTS`: `rtl`, `tb`).

        Each part enters as its own hash (`rtl_hash`, `tb_hash`), in a fixed order, whatever
        order or collection `parts` is given in. The result is the design identity of a flow that
        reads exactly those parts (`Flow.design_parts`): what a part that the flow does not read
        says never changes it. Passing both parts gives the hash of the whole design.

        With `source_types` (`Flow.design_source_types`), a part's hash covers only the sources
        of those types, in their order, and all of its metadata. A source of another type
        changes nothing. The hash then differs from the part's own (`rtl_hash`), which the design
        records unchanged."""
        wanted = frozenset(parts)
        unknown = wanted - DESIGN_PARTS
        if unknown:
            raise ValueError(
                f"{', '.join(sorted(unknown))} is not a part of a design "
                f"(its parts are {', '.join(_DESIGN_PART_ORDER)})"
            )

        def part_hash(part: str) -> str:
            if source_types is None:
                return getattr(self, f"{part}_hash")
            fingerprint = dict(getattr(self, f"{part}_fingerprint"))
            fingerprint["sources"] = [
                entry
                for entry, source in zip(fingerprint["sources"], getattr(self, part).sources)
                if source.type in source_types
            ]
            return semantic_hash(fingerprint)[:32]

        return semantic_hash(
            {f"{part}_hash": part_hash(part) for part in _DESIGN_PART_ORDER if part in wanted}
        )
