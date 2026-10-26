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
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from glob import escape as glob_escape
from glob import glob
from os.path import isfile
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Optional,
    NotRequired,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from urllib.parse import parse_qs, urlparse

import yaml
from pydantic_core import ErrorDetails, InitErrorDetails, PydanticCustomError, core_schema
from typing_extensions import TypedDict

from .dataclass import (
    WHOLE,
    ConfigDict,
    Field,
    PrivateAttr,
    SecondSpelling,
    SerializeAsAny,
    ValidationError,
    XedaBaseModel,
    escaped_names_atomic,
    field_validator,
    input_names,
    model_validator,
    model_with_allow_extra,
    removed_error,
    removed_inputs_in,
    shape_problems,
    unspecified,
    validation_errors,
)
from .digest import content_digest
from .frozen import Frozen, FrozenDict, FrozenModel, freeze, thaw
from .proc_utils import run_child
from .run_dir import DIR_NAME_HASH_LEN, run_directory_name
from .utils import (
    hierarchical_merge,
    ConflictingKeys,
    XedaException,
    expand_env_vars,
    expand_hierarchy,
    expand_vars,
    flows_table_problems,
    location_free,
    path_identity,
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
    "DesignPart",
    "Source",
    "DesignValidationError",
    "FileResource",
    "LanguageSettings",
    "Rtl",
    "SOURCE_SUFFIXES",
    "AMBIGUOUS_SUFFIXES",
    "TYPE_ONLY",
    "LANGUAGE_TYPES",
    "root_relative_text",
    "source_type_named",
    "source_type_of",
    "SourceType",
    "Testbench",
    "VhdlSettings",
    "cocotb_of",
    "generator_of",
    "language_of",
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
            f"\nDesign:\n{pformat(shown_dependencies(self.data))}\n"
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


#: The root of the design that is being loaded. A relative path in a design resolves against it
#: (`design_relative_root`). `Design.__init__` sets it around the validation and the merge of the
#: dependencies, so a load never changes the working directory of the process.
#: A dependency is a design with a root of its own, and a thread has its own value.
_loading_root: ContextVar[Optional[Path]] = ContextVar("loading_root", default=None)


@contextmanager
def loading_in_design_root(root: Path) -> Iterator[None]:
    """Resolve the relative paths of the designs that are built inside this block against `root`."""
    token = _loading_root.set(root)
    try:
        yield
    finally:
        _loading_root.reset(token)


def design_relative_root() -> Path:
    """Return the directory that a relative path in a design resolves against.

    That is the root of the design that is being loaded. Outside a load (a `FileResource` that
    code builds on its own), it is the working directory at this call."""
    root = _loading_root.get()
    return root if root is not None else Path.cwd()


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
    # A relative pattern matches below `root`, and a match is as the pattern spells it.
    return sorted(m for m in glob(str(expanded), root_dir=root) if isfile(root / m))


def as_source_list(sources: Any) -> Any:
    """Return `sources` as the list it stands for.

    One source (a path, a table or a resource) stands for a list of one. This function is the only
    place that knows the form. The `sources` validator, the decision to run a generator again and
    the declared load all use it. Anything else is returned unchanged."""
    if isinstance(sources, (str, os.PathLike, Mapping, FileResource)):
        return [sources]
    return sources


class FileResource:
    """A file of the design. It is immutable: no attribute changes after the constructor."""

    file: Path
    _specified_path: Path
    _checked: bool

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"a {type(self).__name__} is read-only: cannot set `{name}`")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"a {type(self).__name__} is read-only: cannot delete `{name}`")

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

        A relative path resolves against `_root_path`. By default that is the root of the design
        that is being loaded, and without one, the working directory (`design_relative_root`).
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
        root = Path(_root_path) if _root_path else design_relative_root()
        # The path as written, before any variable is expanded or the path is made absolute. The
        # design's identity does not depend on it (see `Design._source_fingerprint`). A flow that
        # names a file after where it came from (GHDL, for two VHDL sources with the same stem)
        # needs what the design said, not an absolute location.
        specified_path = Path(path)
        path = _expand_design_path(path, root)
        if not path.is_absolute():
            path = root / path
        if resolve and not path.exists():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        if resolve and not path.is_file():
            raise IsADirectoryError(errno.EISDIR, "a directory, not a file", str(path))
        attributes = vars(self)
        attributes["_specified_path"] = specified_path
        attributes["file"] = path.resolve() if resolve else path.absolute()
        #: True for a file that must exist (`file`), false for one that need not exist yet (`path`).
        attributes["_checked"] = bool(resolve)

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
    # and `VerilogNetlist` is a netlist mapped to the cells of a Liberty library.
    # `FpgaTimingNetlist` is the timing netlist of the same design, simulated with an SDF. A
    # checkpoint is written at several stages, and only the stage says what reads it. So no flow
    # takes `Checkpoint` (no stage), and `.dcp` names no type. An OpenROAD database (`.odb`) and
    # the SDC that a stage writes with it are a pair per stage (placed, clock tree, global routed,
    # routed). So no flow takes `Odb`, and `.odb` names no type. The SDC of a stage needs its own
    # type, because clock tree synthesis changes the SDC, and a design's `.sdc` file must never
    # stand in for it.
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
    # A Verilog netlist of the cells of yosys itself (gates, LUTs or coarse cells). No Liberty
    # library holds them, so `VerilogNetlist` (a netlist mapped to a Liberty library) is a
    # different type. `xeda.artifact_kinds` has one row for each netlist, checkpoint and database.
    GenericVerilogNetlist = "GenericVerilogNetlist"

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
        # A `settings.json` file can record a 1-based ordinal ("5" for Vhdl) in place of a name.
        # The declaration order above must not change while these files are supported.
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
        SourceType.GenericVerilogNetlist,
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

#: The keys that a source table refuses, with what to write instead.
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


def root_relative_text(root: Path, path: Path) -> str:
    """Return the path from `root` to `path` as POSIX text, whatever the system.

    The path may begin with `..` for a file outside the root. Where no relative path exists (on
    Windows, a file on another drive than the root), the absolute path stands: it is the location
    that the design names."""
    try:
        return Path(os.path.relpath(path, root)).as_posix()
    except ValueError:
        return Path(path).as_posix()


class Source(FileResource):
    """A source file of a design. It is immutable.

    A source is either written (a file of the design) or generated. A generated source belongs to
    the output of a producer: the design's generator, or a converter flow. Its `output_name` is its
    name in that output, relative to the output. It is the same for both producers.

    A generated source is in one of two states:

    - A declaration (`Source.declaration`) is what a design file lists for a generator. It is a
      name or a pattern (`gen/*.v`) in the output of the generator, and the generator has not run
      yet, or the design is not run. It names no file: `file` is None, and the source has no
      content.
    - A located source names a real file in the output: `file` is its absolute path, and
      `output_name` its name in the output.
    """

    type: SourceType
    _stated: dict[str, Any]
    #: Whether the source belongs to the output of a producer (the generator of the design, or a
    #: converter flow) and is no file that the design's author wrote.
    generated: bool
    #: The name (or, for a declaration, the pattern) of a generated source in the output of its
    #: producer; None for a written source. The identity of the design counts a generated source
    #: by this name, never by its path (`Design._source_fingerprint`), because the path into a
    #: storage or a run directory depends on where the run root is. It is no part of the table of
    #: a source in a design file.
    output_name: str | None
    #: The generator that writes a generated source, when it is not the generator of the design
    #: that lists the source: the root of the dependency whose generator it is. A design that
    #: merges a dependency lists the dependency's generated sources with the owner set, and the
    #: execution copy names their files in the storage of that owner. None: the design's own.
    owner: str | None
    #: The index of the dependency (`DependencyRecord`) whose root holds a written source, and its
    #: path relative to that root. Both are None for any other source. The identity of a run counts
    #: such a source by these two, since the place of a Git checkout in the run root holds the
    #: commit.
    dependency: int | None
    dependency_place: str | None

    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Union[str, SourceType, None] = None,
        _root_path: Optional[Path] = None,
        *,
        generated: bool = False,
        output_name: str | None = None,
        owner: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Build a design source while retaining its stated type."""
        if generated != (output_name is not None):
            raise ValueError("a generated source has an `output_name`, and no other source has")
        if isinstance(path, dict):
            problems = source_table_problems(path)
            if problems:
                raise ValueError("; ".join(problems))
            path = dict(path)
            stated_type = path.pop("type", None)
            if typ is None:
                typ = stated_type
        super().__init__(path, _root_path=_root_path, **kwargs)

        attributes = vars(self)
        if isinstance(typ, SourceType):
            attributes["type"] = typ
        elif isinstance(typ, str):
            attributes["type"] = source_type_named(typ)
        elif typ is None:
            attributes["type"] = source_type_of(self.file)
        else:
            raise ValueError(f"a source's `type` is the name of a source type, not {typ!r}")
        # The type that the design stated, as opposed to the one that the file name implied. Only
        # the stated type is written back out: reloading infers the other one from the same
        # suffix again. A stated `type` that contradicts the suffix
        # (`{file: legacy.v, type: SystemVerilog}`) would be lost if the dump left it out, and it
        # is part of the design's identity.
        attributes["_stated"] = {"type": str(self.type)} if typ is not None else {}
        attributes["generated"] = generated
        attributes["output_name"] = output_name
        attributes["owner"] = owner
        attributes["dependency"] = None
        attributes["dependency_place"] = None

    @classmethod
    def declaration(
        cls, name: str, typ: SourceType, *, stated: bool = False, owner: str | None = None
    ) -> Source:
        """Return the declaration of a generated source: a `name` or pattern in the output of the
        design's generator.

        `typ` is its type. `stated` says that the design gave the type, which a dump of the design
        writes back; a type that the suffix implies is inferred again on reload."""
        source = object.__new__(cls)
        attributes = vars(source)
        attributes["file"] = None
        attributes["_specified_path"] = Path(name)
        attributes["_checked"] = False
        attributes["type"] = typ
        attributes["_stated"] = {"type": str(typ)} if stated else {}
        attributes["generated"] = True
        attributes["output_name"] = name
        attributes["owner"] = owner
        attributes["dependency"] = None
        attributes["dependency_place"] = None
        return source

    def with_owner(self, owner: str | None) -> Source:
        """Return this declaration with another owner (see `owner`)."""
        assert self.file is None, "only a declaration changes owner"
        return Source.declaration(
            str(self.output_name), self.type, stated=bool(self._stated), owner=owner
        )

    def in_dependency(self, index: int, place: str) -> Source:
        """Return this written source as a file of the Git dependency `index`, at `place` in its
        checkout (see `dependency`)."""
        assert not self.generated, "only a written source is a file of a checkout"
        marked = object.__new__(type(self))
        vars(marked).update(vars(self), dependency=index, dependency_place=place)
        return marked

    @property
    def located(self) -> bool:
        """Whether the source names a file. A declaration of a generated source does not."""
        return self.file is not None

    @property
    def path(self) -> Path:
        """The absolute file of a located source; the name or pattern of a declaration."""
        if self.file is None:
            assert self.output_name is not None
            return Path(self.output_name)
        return self.file

    @property
    def content_hash(self) -> str:
        if self.file is None:
            raise FileNotFoundError(
                f"the generated source {self.output_name} names no file yet: the generator has "
                "not run, so the design that uses it cannot be identified by content"
            )
        return super().content_hash

    def __eq__(self, other: Any) -> bool:
        # Added attributes do not change semantic equality. A declaration is its name and type.
        if not isinstance(other, FileResource):
            return False
        if self.file is None or getattr(other, "file", None) is None:
            return (
                isinstance(other, Source)
                and self.file is None
                and other.file is None
                and self.output_name == other.output_name
                and self.owner == other.owner
                and self.type is other.type
            )
        return super().__eq__(other)

    def __hash__(self) -> int:
        # Added attributes do not change semantic identity.
        if self.file is None:
            return hash(("declaration", self.output_name, self.owner))
        return super().__hash__()

    def __str__(self) -> str:
        return str(self.output_name) if self.file is None else super().__str__()

    def as_json_value(self) -> Union[str, Dict[str, Any]]:
        """Serialize a source with only the metadata explicitly supplied.

        A declaration is written as its name, or as a table with its `path` and its stated type."""
        if self.file is None:
            if not self._stated:
                return str(self.output_name)
            return {"path": str(self.output_name), **self._stated}
        value = super().as_json_value()
        if not self._stated:
            return value
        if isinstance(value, str):
            value = {"file": value}
        return {**value, **self._stated}

    def __repr__(self) -> str:
        if self.file is None:
            return f"generated:{self.output_name} type:{self.type}"
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
    "parameters must be a dictionary or a list of objects with 'name' and 'value' attributes"
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
    the value alone (`remote.packaging.package_inputs`).
    """
    if value is None:
        # Do not test `not value`: an empty list (`parameters: []`) is the list form and must
        # become `{}` like any other list.
        return value
    value = _parameters_as_mapping(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{_PARAMETERS_FORM_ERROR}, got {type(value).__name__}: {value!r}")
    # A new table: the input stays as it is.
    value = dict(value)
    for key, parameter in list(value.items()):
        if isinstance(parameter, Mapping) and ("file" in parameter or "path" in parameter):
            try:
                value[key] = str(FileResource(dict(parameter)))
            except IsADirectoryError as e:
                raise ValueError(f"parameter {key!r}: {e.filename} is a directory") from e
            except FileNotFoundError as e:
                raise MissingFile(f"parameter {key!r}: file does not exist: {e.filename}") from e
    return value


def _parameters_form(value: Any) -> Any:
    """Return the table that a list of `{name, value}` objects stands for (`field_shorthands`)."""
    return _parameters_as_mapping(value) if isinstance(value, list) else None


def already_listed(sources: Iterable[Source], source: Source) -> bool:
    """Whether `sources` lists the file of `source` (or, for a declaration, the same declaration)."""
    if source.file is None:
        return any(listed == source for listed in sources)
    return any(
        listed.file is not None and listed.file.absolute() == source.file.absolute()
        for listed in sources
    )


def _distinct_sources(sources: Iterable[Source]) -> list[Source]:
    """Return `sources` in order, with a file that is listed twice kept once, at its first place."""
    kept: list[Source] = []
    for source in sources:
        if not already_listed(kept, source):
            kept.append(source)
    return kept


# ---------------------------------------------------------------- who writes a source entry


def _pattern_part(pattern: str, text: str) -> bool:
    """Whether the name part `text` fits the name part `pattern`, where `*` is the only pattern
    character and it never crosses a `/` (a part has none)."""
    return (
        re.fullmatch(".*".join(re.escape(piece) for piece in pattern.split("*")), text) is not None
    )


def _entry_parts(entry: str, root: Path | None) -> tuple[str, ...]:
    """Return the parts of a source entry, for comparing it with another one. It reads no file.

    A variable that names the design root expands. An absolute entry below the root is relative
    to it, so `$DESIGN_ROOT/gen/top.v` and `gen/top.v` are one entry."""
    text = os.path.normpath(entry)
    if root is not None:
        text = os.path.normpath(str(_expand_design_path(entry, root)))
        if os.path.isabs(text) and Path(text).is_relative_to(root):
            text = os.path.relpath(text, root)
    return Path(text).parts


def _relation(entry: tuple[str, ...], owner: tuple[str, ...]) -> str:
    """Return how the files of a source `entry` (a name, or a pattern) lie to the files of an
    `owner` (a name, a pattern, or a directory, which stands for every file below it).

    The answer is `within` (every file of the entry is the owner's), `apart` (no file is), or
    `overlap` (some are). Both are texts: no file is read."""
    if len(entry) < len(owner):
        return "apart"
    for mine, theirs in zip(entry, owner):
        if not (mine == theirs or _pattern_part(mine, theirs) or _pattern_part(theirs, mine)):
            return "apart"
    if all(
        mine == theirs or ("*" not in mine and _pattern_part(theirs, mine))
        for mine, theirs in zip(entry, owner)
    ):
        return "within"
    return "overlap"


def _generated_name(entry: str, parts: tuple[str, ...]) -> str:
    """Return the declaration name of a generated source entry (`parts`: `_entry_parts`).

    The name is relative to the output of the generator, so it is no absolute path and does not
    leave the output."""
    if not parts or Path(*parts).is_absolute() or parts[0] == "..":
        raise ValueError(
            f"the generated source `{entry}` is a name in the output of the generator, such as "
            "`gen/top.v`, and not a path to a place outside it. Without `generated_sources` "
            "every entry of `rtl.sources` is the generator's: list the generated entries in it to "
            "keep a hand-written one"
        )
    return "/".join(parts)


def _declared_type(name: str, stated: Any) -> SourceType:
    """Return the type of the generated source declaration `name`, which the entry may state."""
    if isinstance(stated, SourceType):
        return stated
    if stated is not None:
        return source_type_named(str(stated))
    try:
        return source_type_of(Path(name))
    except ValueError:
        raise ValueError(
            f"the generated source `{name}` has no definite type, because it names files that "
            f"do not exist yet and its suffix does not say what they are: give its type, in YAML "
            f"`{{path: {name}, type: Verilog}}` (or the type of its files)"
        ) from None


def generated_declarations(
    sources: Any, recipe: Mapping[str, Any] | None, root: Path | None
) -> list[Any]:
    """Return the entries `sources` of `rtl`, with those that the generator writes declared.

    An entry is the generator's when `recipe.generated_sources` names it or a directory that
    holds it, or when that list is empty (then every entry is). A generated entry becomes the
    declaration of a generated source (`Source.declaration`): a name or a pattern in the output of
    the generator, typed by its extension or by the `type` that it states. It is not expanded and
    need not exist. Any other entry is left as it is, for the sources validator.

    An entry is wholly the generator's or wholly written. One that would be both (a pattern that
    matches a generated file and a written one) is a `ValueError`, and so is a
    `generated_sources` entry that no entry of `sources` relates to. The function reads no file."""
    if recipe is None or not isinstance(recipe.get("generated_sources", []), (list, tuple)):
        return as_source_list(sources)
    owners = [str(item) for item in recipe.get("generated_sources", ()) if isinstance(item, str)]
    entries = as_source_list(sources)
    if not isinstance(entries, (list, tuple)):
        return entries
    owner_parts = [_entry_parts(owner, root) for owner in owners]
    related = [False] * len(owners)
    seen_declaration = False
    result: list[Any] = []
    for entry in entries:
        text: str | None = None
        stated: Any = None
        file_table = False  # a `file` must exist, so it is a written source
        if isinstance(entry, (str, os.PathLike)):
            text = os.fspath(entry)
        elif isinstance(entry, Mapping) and not isinstance(entry, FileResource):
            problems = source_table_problems(entry)
            if problems:
                raise ValueError("; ".join(problems))
            if ("file" in entry) != ("path" in entry):
                value = entry.get("file", entry.get("path"))
                if isinstance(value, (str, os.PathLike)):
                    text, stated = os.fspath(value), entry.get("type")
                    file_table = "file" in entry
        if text is None:
            seen_declaration = seen_declaration or isinstance(entry, Source)
            result.append(entry)
            continue
        parts = _entry_parts(text, root)
        if file_table and not owners:
            result.append(entry)
            continue
        if not owners:
            generated = True
        else:
            relations = [_relation(parts, owner) for owner in owner_parts]
            for index, relation in enumerate(relations):
                related[index] = related[index] or relation != "apart"
            if "within" in relations:
                generated = True
            elif "overlap" in relations:
                owner = owners[relations.index("overlap")]
                raise ValueError(
                    f"`{text}` is partly generated: `generated_sources` lists `{owner}`, which "
                    "holds some of its files but not all of them. List the generated files and "
                    "the written files in separate entries"
                )
            else:
                generated = False
        if not generated:
            result.append(entry)
            continue
        if file_table:
            raise ValueError(
                f"`{text}` is a `file`, which must exist when the design loads, and "
                "`generated_sources` makes it the generator's: write it as `path` if the "
                "generator writes it, or take it out of `generated_sources` if you wrote it"
            )
        name = _generated_name(text, parts)
        declared = Source.declaration(name, _declared_type(name, stated), stated=stated is not None)
        if declared not in result:
            result.append(declared)
    if not seen_declaration:
        outside = [owner for owner, hit in zip(owners, related) if not hit]
        if outside:
            raise ValueError(
                "`generated_sources` lists entries that are not among `rtl.sources`: "
                + ", ".join(outside)
            )
    return result


#: What `sources` accepts, shown by `xeda design-schema`.
SOURCES_DESCRIPTION = (
    "Source files, in compile order: a path relative to the design root (`$DESIGN_ROOT` and "
    "`$DESIGN_DIR` name it too), or a table, in YAML `{file: ..., type: ...}` (`path` in place of "
    "`file` for one not written yet). A path containing `*` "
    "is a pattern: its files are inserted in sorted order, and it must match at least one. `*` is "
    "the only pattern character: `?`, `[` and `]` are part of a file name, so `rtl/fifo[1].v` "
    "names exactly that file."
)


class DesignPart(XedaBaseModel):
    """Design/Verification settings. A part is immutable (`Design.model_copy` makes a changed one)."""

    model_config = ConfigDict(frozen=True)

    sources: Tuple[Source, ...] = Field(description=SOURCES_DESCRIPTION)
    parameters: Annotated[Mapping[str, DefineType], Frozen()] = Field(
        default={},
        description="Parameters of the top module (Verilog parameters or VHDL generics): a "
        "mapping, or a list of `{name, value}` objects.",
    )
    defines: Annotated[Mapping[str, DefineType], Frozen()] = Field(
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
            return Source(src, typ=typ)

        value = as_source_list(value)
        sources: List[Source] = []

        def source_already_exists(src: Source) -> bool:
            return already_listed(sources, src)

        if not isinstance(value, (list, tuple, set)):
            raise ValueError(
                f"'sources' must be a list of source files, got {type(value).__name__}: {value!r}"
            )
        for src in unique(list(value)):
            if isinstance(src, str):
                src_type = None
                if _is_source_pattern(src):
                    glob_sources = _expand_source_glob(src, design_relative_root())
                    srcs = [ds(s, src_type) for s in glob_sources]
                    sources.extend(s for s in srcs if not source_already_exists(s))
                    continue  # skip the append at the bottom
                src = src_with_type(src, src_type)
            if not isinstance(src, Source):
                try:
                    if isinstance(src, (str, Path)):
                        src = ds(src, None)
                    else:
                        src = Source(src)
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


class Clock(XedaBaseModel):
    # `Rtl.clocks` validates each of its entries as a `Clock` and holds it as a table.
    model_config = ConfigDict(frozen=True)

    port: str
    name: Optional[str] = None

    @classmethod
    def as_mapping(cls, value: Any) -> Any:
        """Read a bare string as the clock's port: `CLK` means `{port: CLK}`."""
        return {"port": value} if isinstance(value, str) else None

    @field_validator("name", mode="before")
    @classmethod
    def _name_validate(cls, value, info) -> Optional[str]:
        values = info.data if isinstance(info.data, dict) else {}
        return values.get("port", None) if value is None or value == "" else value


#: The shell that runs the text of a `shell` generator. It is the shell that runs a command line
#: on a POSIX system (`subprocess` with `shell=True` runs it too).
POSIX_SHELL = "/bin/sh"

#: The variables that Xeda sets in the environment of every generator.
GENERATOR_VARIABLES = frozenset({"DESIGN_ROOT", "XEDA_GENERATED"})


class Generator(XedaBaseModel):
    cwd: Optional[str] = None
    executable: Optional[str] = None
    args: List[str] = Field(
        default_factory=list,
        description="The arguments the executable is run with: a list, or one string split on "
        "whitespace (as `command` is).",
    )
    command: Optional[str] = None
    shell: Optional[str] = Field(
        None,
        description="A command line that a shell runs, such as `python soc.py --build`. Xeda "
        "expands nothing in it: the shell does. `shell` excludes `command` and `executable`.",
    )
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
        description="Run this generator on every launch: what it reads cannot be judged (it is "
        "not files, or they cannot be listed), so Xeda keeps no record that could show its "
        "output to be current. A generator that declares no `sources` runs on every launch "
        "anyway.",
    )
    generated_sources: List[str] = Field(
        default_factory=list,
        description="The entries of `rtl.sources` that this generator produces, when it produces "
        "only some of them. An entry names one of them, or a directory that holds some. Empty, "
        "every entry of `rtl.sources` is the generator's. An entry of `rtl.sources` is wholly "
        "the generator's or wholly written by hand: a pattern that would match both is an "
        "error. A generator's entry is a name (or a pattern) in the output of the generator.",
    )

    removed_inputs: ClassVar[Dict[str, str]] = {"use": SecondSpelling("`class`")}

    #: Whether the generator may run a shell command line (`shell`).
    takes_shell: ClassVar[bool] = True

    @model_validator(mode="after")
    def _shell_excludes_the_other_command_keys(self):
        """A table has one way to say what runs: `shell`, or `command`, or `executable`."""
        if self.shell is None:
            return self
        if not self.takes_shell:
            raise ValueError(f"a {type(self).__name__} runs its own command: `shell` is not valid")
        named = [
            key
            for key, value in (("command", self.command), ("executable", self.executable))
            if value is not None
        ]
        if named:
            raise ValueError(
                f"`shell` and `{'` and `'.join(named)}` exclude each other: "
                "a generator runs one command line"
            )
        if self.args:
            raise ValueError(
                "`shell` and `args` exclude each other: write the arguments in `shell`"
            )
        return self

    @field_validator("cwd")
    @classmethod
    def _cwd_is_below_the_design_root(cls, value):
        """The working directory of a generator is a directory below the design root of the
        design that states it. It is judged by its text: an absolute path, or one that leaves the
        root after normalization (`..`), is refused."""
        if value is not None:
            normalized = os.path.normpath(value)
            if os.path.isabs(value) or normalized == ".." or normalized.startswith(".." + os.sep):
                raise ValueError(
                    f"`cwd` is {value!r}: give a directory below the design root, such as "
                    "`build` (a relative path that stays inside it)"
                )
        return value

    @field_validator("env")
    @classmethod
    def _env_leaves_the_names_xeda_sets(cls, value):
        """Xeda sets `DESIGN_ROOT` and `XEDA_GENERATED` for every generator: `env` may not."""
        if value is not None:
            clash = sorted(set(value) & GENERATOR_VARIABLES)
            if clash:
                raise ValueError(
                    f"`env` sets {', '.join(clash)}, which Xeda sets for the generator itself"
                )
        return value

    @model_validator(mode="before")
    @classmethod
    def _removed_generator_fields(cls, data):
        """Name a removed field with what replaces it, as for every removed setting."""
        if "run_only_if_sources_modified" in data:
            # `true` asks for what always applies (judging by content), so the advice is to delete
            # it, never to turn it into `always_runs`.
            if data["run_only_if_sources_modified"] is True:
                advice = (
                    "delete it (a generator is judged by the content of what it reads and "
                    "produces, never by a modification time; `always_runs: true` would mean "
                    "the opposite)"
                )
            else:
                advice = (
                    "use `always_runs: true` (a generator is judged by the content of what it "
                    "reads and produces, never by a modification time)"
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
        """Expand source patterns and normalize the resulting paths.

        A relative source lies below the root of the design that is being loaded
        (`loading_in_design_root`; `generator_of` gives the root of its design). Without a root,
        only an absolute source is valid: no source resolves against the working directory."""
        root = _loading_root.get()

        def below_root(path: Path) -> Path:
            if path.is_absolute():
                return path
            if root is None:
                raise ValueError(
                    f"the generator source {str(path)!r} is relative, and no design root says "
                    "where it lies"
                )
            return root / path

        if isinstance(value, str):
            value = [value]
        sources: List[Path] = []
        for src in unique(value):
            if isinstance(src, str):
                if root is None and not os.path.isabs(src):
                    below_root(Path(src))  # raises: a relative source needs a root
                base = root if root is not None else Path(os.sep)
                if _is_source_pattern(src):
                    sources.extend(
                        below_root(Path(m)).resolve()
                        for m in _expand_source_glob(src, base, "generator source")
                    )
                else:
                    sources.append(below_root(_expand_design_path(src, base)).resolve())
            elif isinstance(src, Path):
                # Resolve it like the string spelling, so a relative path does not keep
                # depending on the working directory it was given in.
                sources.append(below_root(src).resolve())
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

    #: Whether the working directory of a generator without `cwd` is the design root. Otherwise
    #: it is the storage that holds the output of the generator.
    runs_in_design_root: ClassVar[bool] = False

    def lookup_directory(self, design_root: Path) -> Path:
        """Return the directory that a relative executable and a relative `PATH` entry resolve
        against: `cwd` below the design root, else the design root. `cwd` counts as the load
        check reads it, normalized (`build/../work` is `work`), so no other directory on its
        way needs to exist.

        It does not depend on the storage, so the identity of a generation does not either."""
        return design_root if self.cwd is None else design_root / os.path.normpath(self.cwd)

    def working_directory(self, design_root: Path, storage: Path) -> Path:
        """Return the directory that the generator runs in.

        That is `cwd` below the design root, else the storage that holds its output. A generator
        that needs its project (Chisel) runs in the design root unless `cwd` says otherwise."""
        if self.cwd is not None:
            return self.lookup_directory(design_root)
        return design_root if self.runs_in_design_root else storage

    def _words(
        self, words: Sequence[str], design_root: Path | None, storage: Path | None
    ) -> List[str]:
        """Return the arguments `words`, with `$DESIGN_ROOT` and `$XEDA_GENERATED` expanded.

        No other variable expands, and with no `storage` nothing does: the identity of a
        generation takes the words as the recipe writes them."""
        if design_root is None or storage is None:
            return list(words)
        values = {"DESIGN_ROOT": str(design_root), "XEDA_GENERATED": str(storage)}
        return [expand_vars(word, values) for word in words]

    def execution_command(
        self, design_root: Path | None = None, storage: Path | None = None
    ) -> List[str]:
        """Return the argv that this generator runs, for execution and the freshness identity.

        A `shell` generator runs as `/bin/sh -c <text>`, the shell that runs a command line on a
        POSIX system. The freshness identity names that same shell, not an `sh` on `PATH`. The
        arguments expand `$DESIGN_ROOT` and `$XEDA_GENERATED` when `design_root` and `storage` are
        given (the shell of a `shell` generator expands them from its environment)."""
        if self.shell:
            return [POSIX_SHELL, "-c", self.shell]
        if self.command:
            first, *rest = self.command.split()
            return [first, *self._words(rest, design_root, storage)]
        elif not self.executable:
            raise ValueError("executable is not set")
        else:
            return [self.executable, *self._words(self.args, design_root, storage)]

    def execution_executable_path(
        self,
        design_root: Path,
        command: Optional[Sequence[str]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> Path:
        """Resolve the direct executable as the child process will, without running it.

        A relative executable, and a relative entry of `PATH`, resolve against the directory
        `lookup_directory` names. `PATH` is that of the recipe's `env` when it has one, else the
        `PATH` of `environ`, the environment that the generator inherits."""
        selected_command = (
            list(command) if command is not None else self.execution_command(design_root)
        )
        if not selected_command:
            raise ValueError("generator command is empty")
        executable = selected_command[0]
        base = self.lookup_directory(design_root).resolve()
        env = self.env
        path = env.get("PATH") if env is not None else (environ or {}).get("PATH")
        if path is None:
            path = os.defpath
        if (
            os.path.isabs(executable)
            or os.sep in executable
            or (os.altsep and os.altsep in executable)
        ):
            candidate = Path(executable)
            if not candidate.is_absolute():
                candidate = base / candidate
            if candidate.is_file() and os.access(candidate, os.X_OK):
                # Preserve a selected symlink alias in argv[0]. Identity reads still follow
                # the link and hash the target's contents.
                return candidate
        else:
            search_path = os.pathsep.join(
                str((base / entry).resolve()) if not Path(entry).is_absolute() else entry
                for entry in path.split(os.pathsep)
            )
            found = shutil.which(executable, path=search_path)
            if found:
                return Path(found).absolute()
        raise ValueError(
            f"cannot identify generator executable `{executable}` from `{base}` and PATH"
        )

    def process_environment(
        self, design_root: Path, storage: Path, environ: Mapping[str, str]
    ) -> Dict[str, str]:
        """Return the environment of the generator process.

        That is `env` when the recipe has one (the generator inherits nothing then), else
        `environ`. Xeda sets `DESIGN_ROOT` and `XEDA_GENERATED` in both cases."""
        base = dict(self.env) if self.env is not None else dict(environ)
        base["DESIGN_ROOT"] = str(design_root)
        base["XEDA_GENERATED"] = str(storage)
        return base

    def run(self, design_root: Path, storage: Path, environ: Mapping[str, str]) -> None:
        """Run the generator in its working directory, with its environment."""
        command = self.execution_command(design_root, storage)
        self.run_cmd(command, design_root=design_root, storage=storage, environ=environ)

    def run_cmd(
        self,
        cmd,
        *,
        design_root: Path,
        storage: Path,
        environ: Mapping[str, str],
        check=None,
        stdout=None,
        stderr=None,
    ):
        cmd = list(cmd)
        executable = None
        if cmd:
            # Resolve exactly the executable used by freshness identity. This also makes child
            # cwd and PATH selection consistent on Windows, where Popen ignores both for lookup.
            # Pass it separately to preserve argv[0] and symlink-alias behavior for wrappers.
            executable = str(self.execution_executable_path(design_root, cmd, environ))
        log.info("Running command: '%s'", " ".join(cmd))
        # The output of the generator goes where tool output goes (`run_child`): it does not
        # corrupt a `--json` document and does not reach the standard streams of a library caller.
        # A failing generator raises `NonZeroExitCode`, as the other two spellings of a generator.
        return run_child(
            cmd,
            source=cmd[0] if cmd else "generator",
            cwd=self.working_directory(design_root, storage),
            executable=executable,
            check=self.check if check is None else check,
            stdout=stdout,
            stderr=stderr,
            env=self.process_environment(design_root, storage, environ),
        )

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
        if self.shell:
            return self.shell
        try:
            return shlex.join(self.execution_command(design_root))
        except ValueError:
            return self.name


class ChiselGenerator(Generator):
    takes_shell: ClassVar[bool] = False
    #: mill and bloop find their build files in their working directory.
    runs_in_design_root: ClassVar[bool] = True

    main: Optional[str] = None
    project: Optional[str] = None
    build_system: str = "mill"
    check: bool = True

    def command_text(self, design_root: Optional[Path] = None) -> str:
        if self.build_system == "bloop" and not self.project:
            return self.name
        return super().command_text(design_root)

    def run(self, design_root: Path, storage: Path, environ: Mapping[str, str]) -> None:
        if self.build_system == "mill":
            self.run_mill(design_root, storage, environ)
        elif self.build_system == "bloop":
            self.run_bloop(design_root, storage, environ)
        else:
            raise Exception(f"Unsupported build system: {self.build_system}")

    def execution_command(
        self, design_root: Path | None = None, storage: Path | None = None
    ) -> List[str]:
        args = self._words(self.args, design_root, storage)
        if self.build_system == "mill":
            lookup = None if design_root is None else self.lookup_directory(design_root)
            mill_exec = "./mill" if lookup is not None and (lookup / "mill").exists() else "mill"
            cmd = [mill_exec]
            if not self.project:
                raise ValueError("`project` must be specified for Chisel generator")
            cmd += [f"{self.project}.runMain", self.main] if self.main else [f"{self.project}.run"]
            return [*cmd, *args]
        if self.build_system == "bloop":
            if not self.project:
                # `run_bloop` discovers the project by invoking `bloop projects`; the executable
                # whose content matters is still selected here without that side effect.
                base = ["bloop", "projects"]
                return base
            cmd = ["bloop", "run", self.project]
            if self.main:
                cmd += ["--main", self.main]
            if args:
                cmd += ["--", *args]
            return cmd
        raise ValueError(f"Unsupported build system: {self.build_system}")

    def run_mill(self, design_root: Path, storage: Path, environ: Mapping[str, str]) -> None:
        self.run_cmd(
            self.execution_command(design_root, storage),
            design_root=design_root,
            storage=storage,
            environ=environ,
        )

    def run_bloop(self, design_root: Path, storage: Path, environ: Mapping[str, str]) -> None:
        if self.project is None:
            p = self.run_cmd(
                ["bloop", "projects"],
                design_root=design_root,
                storage=storage,
                environ=environ,
                stdout=subprocess.PIPE,
            )
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
        self.run_cmd(
            self.execution_command(design_root, storage),
            design_root=design_root,
            storage=storage,
            environ=environ,
        )


#: The classes of generator that a recipe names with `class`. Without `class`, a recipe is a
#: plain `Generator`.
GENERATOR_CLASSES = ("chisel",)


def generator_model(recipe: Any, root: Optional[Path] = None) -> Generator | None:
    """Return the generator that a table recipe stands for, or `None` for no recipe.

    The table names its class with `class` (`chisel`); without one it is a plain `Generator`. A
    mistake in the table raises the `ValidationError` of the model, and an unknown class raises a
    `ValueError`. The relative `sources` of the table lie below `root`."""
    if isinstance(recipe, Generator):
        return recipe
    if not isinstance(recipe, Mapping):
        return None
    table = thaw(recipe)
    clazz = table.pop("class", None)
    if clazz and not isinstance(clazz, str):
        raise ValueError(f"class={clazz} must be a string")
    if clazz and clazz not in GENERATOR_CLASSES:
        raise ValueError(f"unknown generator class: {clazz}")
    model = ChiselGenerator if clazz else Generator
    with nullcontext() if root is None else loading_in_design_root(root):
        return model(**table)


def _generator_recipe(value: Any, root: Optional[Path] = None) -> Optional[FrozenDict]:
    """Return the recipe of `rtl.generator` as a read-only table, or `None` for no generator.

    A design file writes the recipe in one of three forms, and the design keeps one:

    - a command line for a shell, ``"python soc.py"``, is ``{shell: "python soc.py"}``;
    - a list of arguments, ``[python3, soc.py]``, is ``{executable: python3, args: [soc.py]}``;
    - a table stays as it is. A `Generator` is read as the table that makes it again.

    An empty command line or list stands for no generator. A table is checked here
    (`generator_model`, with its sources below `root`), so a mistake in it fails when the design
    loads, though the table is what the design keeps."""
    if isinstance(value, Generator):
        table = value.model_dump(by_alias=True, exclude_none=True)
        if isinstance(value, ChiselGenerator):
            table["class"] = "chisel"
        value = table
    if value is None:
        return None
    if isinstance(value, str):
        return FrozenDict({"shell": value}) if value else None
    if isinstance(value, (list, tuple)):
        if not all(isinstance(argument, str) for argument in value):
            raise ValueError(f"a generator given as a list holds only text, got {value!r:.80}")
        if not value:
            return None
        return FrozenDict({"executable": value[0], "args": tuple(value[1:])})
    if isinstance(value, Mapping):
        if not value:
            return None
        recipe = freeze(value)
        for name, _, _ in Generator.removed_input_problems(recipe):
            # Report it at the key as written, as the design reports a removed key.
            raise ValidationError.from_exception_data(
                "Generator",
                [
                    InitErrorDetails(
                        type=removed_error(
                            name, Generator.removed_inputs[name], recipe[name], "removed_key"
                        ),
                        loc=(name,),
                        input=recipe[name],
                    )
                ],
            )
        generator_model(recipe, root)
        return recipe
    raise ValueError(
        f"a generator is a command line, a list of arguments or a table, not {value!r:.80}"
    )


def _generator_form(value: Any) -> Any:
    """Return the table that a command line or a list of arguments stands for as `rtl.generator`
    (`field_shorthands`); an empty one stands for the empty table, which is no generator."""
    recipe = _generator_recipe(value)
    return {} if recipe is None else thaw(recipe)


class _GeneratorRecipe:
    """The annotation of `Rtl.generator`: the recipe, frozen, with the schema of the three forms."""

    #: The models that validate a table recipe (the schema describes them).
    models = (Generator, ChiselGenerator)

    def __get_pydantic_core_schema__(self, source: Any, handler: Any) -> Any:
        return core_schema.no_info_plain_validator_function(
            _generator_recipe,
            json_schema_input_schema=handler(Union[str, List[str], Generator, None]),
            serialization=core_schema.plain_serializer_function_ser_schema(
                thaw, return_schema=core_schema.any_schema()
            ),
        )


CLOCK_SPELLINGS = ("clock", "clocks")


class Rtl(DesignPart):
    """The RTL: sources, top module, clocks, generator, attributes, parameters and defines."""

    top: Optional[str] = Field(
        None,
        description="The RTL top module (a Verilog module or a VHDL entity).",
    )
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"generator": _generator_form}
    #: The recipe of the generator as a table. A design file may write a command line or a list
    #: of arguments too, and the design keeps the table that stands for it
    #: (`_generator_recipe`). `generator_of(rtl, root=...)` gives the validated `Generator`.
    generator: Annotated[Optional[Mapping[str, Any]], _GeneratorRecipe()] = None
    attributes: Annotated[Mapping[str, Mapping[str, Any]], Frozen()] = Field(
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
    # Each clock is a table `{"port": ..., "name": ...}`; `Clock` validates it (`name` is the
    # port when the design gives none).
    clocks: Tuple[Annotated[Mapping[str, str], FrozenModel(Clock)], ...] = ()

    #: The inputs that Xeda refuses, with what to use instead for each: another spelling of a
    #: key that stays (`removed_alias`), or a key that is gone (`removed_key`).
    removed_inputs: ClassVar[Dict[str, str]] = {
        "generics": SecondSpelling("`rtl.parameters`"),
        "clock_port": "`rtl.clocks`, as `rtl.clocks: [{port: <port>}]`",
    }
    #: One clock stands for a list of one (`clock_tables`).
    single_item_fields: ClassVar[tuple[str, ...]] = ("clocks",)

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

    @model_validator(mode="before")
    @classmethod
    def _declare_generated_sources(cls, values):
        """Turn the entries of `sources` that the generator writes into declarations.

        The generator has not run, so such an entry names no file (`generated_declarations`). It is
        done here, where `sources` and `generator` are both in hand, and not in the validator of
        `sources`, which sees one field. A source that is a `Source` already stays as it is, so
        validating a design again changes nothing."""
        if (
            not isinstance(values, Mapping)
            or not values.get("generator")
            or "sources" not in values
        ):
            return values
        try:
            recipe = _generator_recipe(values["generator"])
        except (ValueError, TypeError):
            return values  # the field of the generator reports it
        if recipe is None:
            return values
        return {
            **values,
            "sources": generated_declarations(values["sources"], recipe, _loading_root.get()),
        }

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
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"Expecting 'clocks' to be a list but found {value}")
            items = value
        else:
            items = [value]
        # An empty table, like `null` and `""`, says nothing: there is no clock.
        return [
            (
                Clock.as_mapping(item)
                if isinstance(item, str)
                else dict(item) if isinstance(item, Mapping) else item
            )
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

        The design's clock is `clock` or `clocks`, two input forms of one list of clocks. Where
        `values` writes one of them and `merged` has one below, the two meet as lists of tables.
        `clocks`, in any form, replaces the list below. `clock` refines the first design clock
        key by key. `clock: null` means no clock, which replaces. A layer that writes two
        spellings, or a clock that is not a form of a clock, stays as it is, so validation
        reports it. A lower clock that is not a form of a clock is kept, and the clock of the
        higher layer is dropped."""
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


class CocotbTestbench(XedaBaseModel):
    # `Testbench.cocotb` validates its table as a `CocotbTestbench` and holds it as a table.
    model_config = ConfigDict(frozen=True)

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


#: The name of a Python distribution, without a version specifier or extras.
PYTHON_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class Testbench(DesignPart):
    """The testbench: sources, top modules, unit under test, cocotb, parameters and defines."""

    sources: Tuple[Source, ...] = Field((), description=SOURCES_DESCRIPTION)
    top: Tuple012 = Field(
        tuple(),
        description="The testbench top modules: the primary top module and, optionally, a "
        "secondary one. Give one string, or a list of at most two.",
    )
    uut: Optional[str] = Field(
        None, description="The instance name of the unit under test in the testbench."
    )
    removed_inputs: ClassVar[Dict[str, str]] = {"generics": SecondSpelling("`tb.parameters`")}
    # The table of cocotb settings. `CocotbTestbench` validates it, and `cocotb_of(tb)` gives the
    # model.
    cocotb: Optional[Annotated[Mapping[str, Any], FrozenModel(CocotbTestbench)]] = Field(
        None,
        description="Set this when the testbench is based on the cocotb framework: `true` for "
        "the defaults, or a table of cocotb settings. `false` means no cocotb testbench. A source "
        "of type `Cocotb` sets it automatically.",
    )
    python_packages: Tuple[str, ...] = Field(
        (),
        description="Python packages that the testbench imports, for example cocotb. Xeda checks "
        "that they are installed before a simulation, and installs the same versions on a remote "
        "host.",
    )

    @field_validator("python_packages")
    @classmethod
    def _python_package_names(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        for name in value:
            if not PYTHON_PACKAGE_NAME.fullmatch(name):
                raise ValueError(
                    f"`{name}` is not a distribution name. Give only the name of the package, "
                    "such as `cocotb`, with no version and no extras. Xeda installs the local "
                    "version of the package on a remote host."
                )
        return value

    @field_validator("top", mode="before")
    @classmethod
    def _tb_top_validate(cls, value) -> Tuple012:
        if value is None or value == "":
            return tuple()
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, (tuple, list)):
            raise ValueError(
                "`top` is a module name or a list of at most 2 module names, "
                f"got {type(value).__name__}: {value!r}"
            )
        if len(value) > 2:
            raise ValueError("At most 2 simulation top modules are supported.")
        return tuple(value)

    @field_validator("cocotb", mode="before")
    @classmethod
    def _auto_set_cocotb(cls, value, info):
        if (info.context or {}).get("merged"):
            return value  # the design again, with its dependencies: nothing is derived again
        values = info.data if isinstance(info.data, dict) else {}
        form = CocotbTestbench.as_mapping(value)
        if form is WHOLE:
            return None

        def has_cocotb(tb):
            sources = tb.get("sources", [])
            for src in sources:
                if isinstance(src, Source) and src.type == SourceType.Cocotb:
                    return True
                if isinstance(src, dict) and src.get("type") in ["cocotb", SourceType.Cocotb]:
                    return True
                if isinstance(src, str) and src.startswith("cocotb:") and src.endswith(".py"):
                    return True
            return False

        if form is not None or (value is None and has_cocotb(values)):
            return form or {}
        return value


def generator_of(rtl: Rtl, *, root: Path) -> Generator | None:
    """Return the generator that the recipe `rtl.generator` stands for, validated, or `None` when
    there is no recipe.

    The result is a new `Generator`. Its relative `sources` lie below `root`, the root of the
    design that holds `rtl`."""
    return generator_model(rtl.generator, root)


def language_of(design: Design) -> Language:
    """Return the language settings of `design` as a validated model."""
    return Language(**thaw(design.language))


def cocotb_of(tb: Testbench) -> CocotbTestbench | None:
    """Return the cocotb settings of `tb` as a validated model, or `None` for no cocotb testbench."""
    return None if tb.cocotb is None else CocotbTestbench(**thaw(tb.cocotb))


class LanguageSettings(XedaBaseModel):
    model_config = ConfigDict(frozen=True)

    standard: Optional[str] = Field(None, description="The version of the language standard.")
    removed_inputs: ClassVar[Dict[str, str]] = {
        "version": SecondSpelling("`language.verilog.standard`")
    }

    @field_validator("standard", mode="before")
    @classmethod
    def two_digit_standard(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)  # a standard is naturally written as a number: `2008`
        if not isinstance(value, str):
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
    removed_inputs: ClassVar[Dict[str, str]] = {
        "version": SecondSpelling("`language.vhdl.standard`")
    }


class Language(XedaBaseModel):
    model_config = ConfigDict(frozen=True)

    vhdl: VhdlSettings = VhdlSettings()  # type: ignore
    verilog: LanguageSettings = LanguageSettings()  # type: ignore

    @field_validator("verilog", "vhdl", mode="before")
    @classmethod
    def _language_settings(cls, value, info):
        model = {"vhdl": VhdlSettings, "verilog": LanguageSettings}.get(info.field_name)
        expanded = model.as_mapping(value) if model is not None else None
        return value if expanded is None else expanded


class RtlDep(XedaBaseModel):
    model_config = ConfigDict(frozen=True)

    pos: int = 0


class TbDep(XedaBaseModel):
    model_config = ConfigDict(frozen=True)

    pos: int = 0


class DesignReference(XedaBaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str
    rtl: RtlDep = RtlDep()
    tb: TbDep = TbDep()

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


def shown_dependencies(data: Mapping[str, Any]) -> Mapping[str, Any]:
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


#: A full Git commit hash: 40 hexadecimal digits, or 64 in a SHA-256 repository.
_FULL_COMMIT = re.compile(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})")


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

    #: The keys that name where a clone goes. Xeda keeps every Git dependency in the run root.
    removed_inputs: ClassVar[Dict[str, str]] = {
        "clone_dir": "a local dependency that names the design file in a checkout you manage",
        "local_cache": "a local dependency that names the design file in a checkout you manage",
    }

    @field_validator("commit")
    @classmethod
    def _a_commit_is_complete(cls, value: Optional[str]) -> Optional[str]:
        """Accept a full commit hash only: an abbreviated one names different commits over time."""
        if value is not None and not _FULL_COMMIT.fullmatch(value):
            raise ValueError(
                f"the commit {value!r} is not a full commit hash: write the full commit hash "
                "(40 hexadecimal digits, or 64 for a SHA-256 repository)"
            )
        return value.lower() if value is not None else None

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
            # A `file` URL has no host: `file:///srv/lib.git`.
            if not uri.scheme or not (uri.netloc or (uri.scheme == "file" and uri.path)):
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


@dataclass(frozen=True)
class DependencyRecord:
    """A dependency of a design as planning resolved it, at the index of its entry in
    `Design.dependencies`.

    A Git dependency has its repository, the reference as written, the commit that the reference
    named, and where the checkout of that commit is. `repository` has no user name and no
    password (`redacted_url`). `reference` is the branch or tag as written, and `None` for a
    pinned commit or the `HEAD` of the repository. `design_file` is the path of the design file in
    the repository, and `checkout` is the resolved directory of the checkout in the dependency
    storage. A local dependency has only its `design_file`, as written, and no repository, no
    commit and no checkout."""

    repository: str | None
    reference: str | None
    commit: str | None
    design_file: str
    checkout: Path | None

    @property
    def storage(self) -> Path | None:
        """The storage that holds the checkout of a Git dependency, or None. A launch leases it."""
        return None if self.checkout is None else self.checkout.parent

    def as_mapping(self) -> dict[str, str | None]:
        """The record as a run record holds it (`RunRecord.dependencies`): no checkout."""
        return {
            "repository": self.repository,
            "reference": self.reference,
            "commit": self.commit,
            "design_file": self.design_file,
        }


def dependency_pin(repository: str, branch: str | None, design_file: str) -> str:
    """Return the name of a Git reference in `Design.dependency_commits`: its repository (as
    `redacted_url` shows it), its branch or tag as written, and its design file. The name is a
    digest, since a dot in a key of a design document would expand to a table."""
    text = json.dumps([repository, branch, design_file])
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class MergedDependency:
    """A dependency of a design, as `Design.merged_with` takes it: the reference, the design that
    it names (with the dependencies of that design merged), the index of its entry, and its root
    (the resolved checkout of a Git dependency, the design root of a local one)."""

    reference: DesignReference
    design: Design
    index: int | None = None
    checkout: Path | None = None

    def placed(self, source: Source) -> Source:
        """Return `source`, a source of the design of this dependency, as the merged design lists
        it: a written file in the root of the dependency is counted by its place there."""
        if (
            self.index is None
            or self.checkout is None
            or source.generated
            or source.dependency is not None
            or not source.path.is_relative_to(self.checkout)
        ):
            return source
        return source.in_dependency(self.index, source.path.relative_to(self.checkout).as_posix())


DesignType = TypeVar("DesignType", bound="Design")

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
    (`run_dir.run_directory_name`). A flow is a registered flow's name, or a removed
    flow's name (`settings_layers.REMOVED_FLOWS`). Another letter case or dashes name the flow too,
    for this judgment: on a file system that ignores letter case, they are one directory."""
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


class _GitReferenceInput(TypedDict):
    """The Git mapping form accepted by `DesignReference.from_data`."""

    repo_url: str
    design_file: str
    uri: NotRequired[str]
    branch: NotRequired[Optional[str]]
    commit: NotRequired[Optional[str]]
    rtl: NotRequired[RtlDep]
    tb: NotRequired[TbDep]


class Design(XedaBaseModel):
    # A design is immutable, and so is everything in it. `model_copy(update=...)` makes a changed
    # design; call `invalidate_cached_properties()` on the copy.
    model_config = ConfigDict(frozen=True)

    name: str = Field(
        description="The unique name of the design. It starts with a letter and has only "
        "letters, digits, `_` and `-`. Name regex: [a-zA-Z][a-zA-Z0-9_\\-]*."
    )

    design_root: Optional[Path] = Field(None, json_schema_extra={"hidden_from_schema": True})
    description: Optional[str] = Field(None, description="A brief description of the design.")
    authors: Tuple[str, ...] = Field(
        (),
        description='The authors of the design, each in "Name <email>" format (the RFC 5322 '
        'mailbox format), for example ["Jane Doe <jane@example.com>", '
        '"John Doe <john@example.com>"].',
    )
    # `SerializeAsAny`: a dependency is usually a `GitReference`, and pydantic 2 serializes a
    # nested model by its annotated type unless told otherwise. That would silently drop
    # `repo_url`, `design_file`, `branch`, `commit` and the clone settings that the package of a
    # remote run relies on.
    dependencies: Tuple[SerializeAsAny[DesignReference], ...] = ()
    rtl: Rtl
    tb: Testbench = Testbench()  # type: ignore
    # A table of the settings of `Language`, which `language_of(design)` gives as a model.
    language: Annotated[Mapping[str, Any], FrozenModel(Language)] = Field(
        default_factory=dict,
        description="HDL language settings",
    )
    flows: Annotated[Mapping[str, Mapping[str, Any]], Frozen()] = Field(
        dict(),
        description="Design-specific flow settings. Each key is the name of a flow, and each "
        "value is the design's settings for that flow.",
    )
    license: Union[str, Tuple[str, ...], None] = None
    version: Optional[str] = None
    url: Optional[str] = None
    target: str | None = Field(
        None,
        description="Name of the target this design was selected as (`targets.<name>` in its "
        "design file), recorded by the loader for documents to report. It is not part of the "
        "design's identity, and a design file does not write it.",
        json_schema_extra={"hidden_from_schema": True},
    )

    # The designs, dependencies of this one, whose generators the launch runs, dependencies
    # first. A dependency design is a unit with its own root, recipe and configuration. It is no
    # part of a dump of the design: a document of a remote run holds its units apart.
    # (Typed as `Any`, because a model that contains itself has no flat schema.)
    generation_units: Tuple[Any, ...] = Field(
        (), exclude=True, json_schema_extra={"hidden_from_schema": True}
    )

    # The Git dependencies that the sources of this design come from, in the order of their index
    # (`Source.dependency`): set by planning, when it merges them. It is no part of a dump of the
    # design.
    resolved_dependencies: Tuple[Any, ...] = Field(
        (), exclude=True, json_schema_extra={"hidden_from_schema": True}
    )

    # The commits that a remote host fetches, by `dependency_pin` of the Git reference. This side
    # resolved them when it planned, and writes them in the package, so that the host fetches the
    # same commits, also for the Git dependencies of a Git dependency, which the package does not
    # list. Xeda writes it; a design file does not (`Design.from_file`).
    dependency_commits: Annotated[Mapping[str, str], Frozen()] = Field(
        default_factory=dict, exclude=True, json_schema_extra={"hidden_from_schema": True}
    )

    @field_validator("resolved_dependencies")
    @classmethod
    def _resolved_are_records(cls, value):
        for record in value:
            if not isinstance(record, DependencyRecord):
                raise ValueError(
                    f"a resolved dependency is a DependencyRecord, not {type(record).__name__}"
                )
        return value

    @field_validator("generation_units")
    @classmethod
    def _units_are_designs(cls, value):
        for unit in value:
            if not isinstance(unit, Design):
                raise ValueError(f"a unit of generation is a Design, not {type(unit).__name__}")
        return value

    #: A design whose `flows` is `null` has none.
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"flows": unspecified}

    #: The keys that Xeda refuses, with what to use instead for each. The flat keys of the RTL
    #: (`sources`, `top`, ...) and of the testbench (`tests`, `test`) belong in `rtl` and `tb`.
    removed_inputs: ClassVar[Dict[str, str]] = {
        "author": SecondSpelling("`authors`"),
        "hdl": SecondSpelling("`language`"),
        "flow": SecondSpelling("`flows`"),
        "tests": SecondSpelling("`tb`"),
        "test": SecondSpelling("`tb`"),
        "sources": SecondSpelling("`rtl.sources`"),
        "top": SecondSpelling("`rtl.top`"),
        "clock": SecondSpelling("`rtl.clocks`"),
        "clocks": SecondSpelling("`rtl.clocks`"),
        "parameters": SecondSpelling("`rtl.parameters`"),
        "generics": SecondSpelling("`rtl.parameters`"),
        "defines": SecondSpelling("`rtl.defines`"),
        "generator": SecondSpelling("`rtl.generator`"),
        "clock_port": "`rtl.clocks`, as `rtl.clocks: [{port: <port>}]`",
    }

    #: The design file that `from_file` read (absolute), or `None` for any other design. It is no
    #: field: a saved design, a hash and a comparison of settings never see it.
    _source_file: Optional[Path] = PrivateAttr(default=None)

    def __eq__(self, other: object) -> bool:
        """Two designs are equal when their fields are: the file a design was read from says
        nothing about it."""
        if not isinstance(other, Design):
            return NotImplemented
        return (
            type(self) is type(other)
            and self.__pydantic_extra__ == other.__pydantic_extra__
            and all(getattr(self, name) == getattr(other, name) for name in type(self).model_fields)
        )

    __hash__ = None  # type: ignore[assignment]

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

    @field_validator("flows", mode="before")
    @classmethod
    def _flow_settings(cls, value):
        return _flows_table(value)

    @field_validator(
        "dependencies",
        mode="before",
        json_schema_input_type=List[Union[str, DesignReference, _GitReferenceInput]],
    )
    @classmethod
    def _dependencies_from_str(cls, value):
        if value and isinstance(value, (list, tuple)):
            value = [DesignReference.from_data(v) for v in value]
        return value

    @field_validator("authors", mode="before")
    @classmethod
    def _authors_from_str(cls, value):
        if isinstance(value, str):
            return [value]
        return value

    @classmethod
    def with_default_rtl(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Return `data` with an empty `rtl` section when it has none. A design with no RTL
        sources is a design."""
        if "rtl" not in data:
            data["rtl"] = {"sources": [], "generator": None, "defines": {}, "top": None}
        return data

    @classmethod
    def select_target(cls, data: Mapping[str, Any], target: str | None = None) -> dict[str, Any]:
        """Return the design that `data` describes once one of its `targets` is selected.

        The result is an ordinary design with the target's name recorded as `target`. Without a
        `target` the result is the design itself, with no overlay, and `target` stays unset.

        A target is an overlay: it takes the keys of the design file itself (all but
        `TARGET_FORBIDDEN_KEYS`), checked as the design's own are and
        merged over them. Mappings merge key by key at every depth; `rtl.sources` and
        `tb.sources` are appended after the design's; any other list replaces the design's.

        Targets are optional: with no `target` given, no target is selected, whatever the number
        of targets (the overlays are still checked). Without `targets`, naming a `target` is an
        error. A design cannot write `target`, and `targets: null` is invalid. Every problem is a
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
        if target is None:
            return data
        if target not in overlays:
            names = ", ".join(overlays)
            close = difflib.get_close_matches(str(target), list(overlays), n=1)
            raise invalid(
                "targets",
                f"{target!r} is not a target of the design"
                + (f" (did you mean {close[0]!r}?)" if close else "")
                + f". Its targets are: {names}",
            )
        overlay = overlays[target]
        try:
            base = cls.with_default_rtl(deepcopy(data))
        except ValueError as e:
            raise invalid("", str(e)) from e
        # The flows table is judged first, as the design validator judges it: the design is as
        # valid as it is, whichever target is selected.
        for key, value in base.items():
            if input_names(cls).get(key) == "flows":
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
        merged over it, both given in the form of a design file.

        One rule serves every key: the rule that merges the settings layers of a flow
        (`settings_layers.merge_layers`). Each side is read as the tables that its shorthands
        stand for (a clock as its port, `parameters` as a list of objects, `vhdl` as its
        standard). Tables merge key by key
        at every depth, and everything else that the overlay writes replaces. A target passes
        `append`: its sources come after the design's, because it adds the files of a board. The
        sources of any other overlay replace the design's. Each flow's section is merged by that
        flow's own rules. A value of the design that is
        not a table where one is expected is kept as it is, with nothing of the overlay merged
        over it, so validation reports it as it would with no target."""
        layers = importlib.import_module("xeda.flow_runner.settings_layers")
        base, overlay = dict(base), dict(overlay)
        flows: list[dict[str, Any] | None] = []
        for side in (base, overlay):
            names = [key for key in side if input_names(cls).get(key) == "flows"]
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
        merged["flows"] = merged_flows
        return merged

    @classmethod
    def _target_overlay(cls, name: Any, overlay: Any) -> dict[str, Any]:
        """Check and fold one target's overlay, as a design's own top level is."""
        loc = f"targets.{name}"

        def invalid(key: str | None, msg: str, kind: str = "value_error") -> DesignValidationError:
            return DesignValidationError([(f"{loc}.{key}" if key else loc, msg, "", kind)])

        problem = target_name_problem(name)
        if problem is not None:
            raise invalid(None, problem)
        if not isinstance(overlay, Mapping):
            raise invalid(
                None,
                "a target is a table of the design's own keys (`rtl`, `tb`, `flows`, ...), "
                f"got {type(overlay).__name__}",
            )
        try:
            overlay = expand_hierarchy(dict(overlay))
        except ConflictingKeys as e:
            raise invalid(None, str(e)) from e
        known = {*input_names(cls)} - TARGET_FORBIDDEN_KEYS
        for path, message, kind in removed_inputs_in(cls, overlay):
            raise invalid(".".join(path), message, kind)
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
            if input_names(cls).get(key) == "flows":
                try:
                    _flows_table(value)
                except ValueError as e:
                    raise invalid(key, str(e)) from e
        folded = overlay
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
            if input_names(cls).get(path[0]) != "flows":  # a `flows` table is judged on its own
                # This is the design's clocks, not any key of that name (an `rtl.attributes`
                # entry).
                takes = (
                    Rtl.clock_expected()
                    if path in (("rtl", "clock"), ("rtl", "clocks"))
                    else "a table or a short form of one"
                )
                raise invalid(".".join(path), f"takes {takes}, not {value!r:.60}")
        for key, value in folded.items():
            if input_names(cls).get(key) == "flows":
                for path, value in _flow_section_problems(_flows_table(value)):
                    raise invalid(
                        ".".join((key, *path)),
                        f"takes a table or a short form of one, not {value!r:.60}",
                    )
        return folded

    @classmethod
    def process_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        data = cls.with_default_rtl(data)
        log.debug("Design data: %s", shown_dependencies(data))
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
        if design_root is None:
            design_root = (
                data.pop("design_root") if "design_root" in data else design_relative_root()
            )
        if design_root is None:
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
            root = Path(design_root)
            if not root.is_absolute():
                root = design_relative_root() / root
            design_root = root.resolve()
        except TypeError:
            raise invalid("design_root", f"not a path: {design_root!r}") from None
        if not design_root.is_dir():
            raise invalid("design_root", f"directory does not exist: {design_root}")
        if not data.get("design_root"):
            data["design_root"] = design_root
        # A relative path in the design resolves against the design root, in the validation of
        # the nested models, in the generators and in the dependencies alike. The working
        # directory of the process stays as it is.
        with loading_in_design_root(design_root):
            if "targets" in data:
                data = self.select_target(data)
            try:
                data = Design.process_dict(data)
            except (ValueError, TypeError, AssertionError) as e:
                raise invalid(None, str(e)) from e
            try:
                super().__init__(**data)
            except ValidationError as e:
                raise DesignValidationError(
                    _design_errors(e.errors()), data=data, design_root=design_root
                ) from e

    @property
    def generation_label(self) -> str:
        """Return the text that tells this design apart as a unit of generation and as the owner
        of generated sources: its design file, or, for a design built in code, its root and name.

        Two dependency designs in one directory have two labels, and one design file listed twice
        has one."""
        if self._source_file is not None:
            return Path(os.path.normpath(self._source_file)).as_posix()
        return f"{self.root_path.as_posix()}::{self.name}"

    def merged_with(
        self,
        merged: Sequence[MergedDependency],
        resolved: Sequence[DependencyRecord] = (),
    ) -> Design:
        """Return this design with the designs of `merged`, its dependencies, merged in.

        The result lists the sources, top module, parameters and clocks of the dependencies, and
        keeps the dependencies that have generators as units of generation. `resolved` lists the
        dependencies, at their indexes (`Source.dependency`). The result has no `dependencies`: a
        record that kept them as well would merge them again when it is read back (a
        `settings.json`) and duplicate sources. The sources of the result are checked as a design's own are: a file
        that the design and a dependency both list is listed once.

        This design is not changed. `DesignValidationError` says what is wrong."""
        sources = list(self.rtl.sources)
        top = self.rtl.top
        parameters = self.rtl.parameters
        clocks = self.rtl.clocks
        tb_sources = self.tb.sources
        tb_top = self.tb.top
        units: dict[str, Design] = {unit.generation_label: unit for unit in self.generation_units}
        for entry in merged:
            dep_design, dep = entry.design, entry.reference
            log.info("adding dependency sources from %s", dep_design.name)
            # The generator of a dependency writes the sources it declares, in a storage of its
            # own. The merged design lists them with the dependency as their owner, and keeps the
            # dependency (and the dependencies it merged) as a unit of generation, dependencies
            # first.
            owner = dep_design.generation_label
            dep_sources = tuple(
                (
                    src.with_owner(owner)
                    if src.generated and not src.located and src.owner is None
                    else entry.placed(src)
                )
                for src in dep_design.rtl.sources
            )
            for unit in (
                *dep_design.generation_units,
                *([dep_design] if dep_design.rtl.generator is not None else []),
            ):
                units.setdefault(unit.generation_label, unit)
            pos = dep.rtl.pos
            if pos == -1:  # -1 means append 'after' the last element
                sources.extend(dep_sources)
                if not top and dep_design.rtl.top:
                    top = dep_design.rtl.top
                if not parameters and dep_design.rtl.parameters:
                    parameters = dep_design.rtl.parameters
                if not clocks and dep_design.rtl.clocks:
                    clocks = dep_design.rtl.clocks
            else:
                if pos < 0:
                    pos += 1  # afterwards: pos=-2 means the position 'before' the last element
                sources[pos:pos] = dep_sources
            sources = _distinct_sources(sources)
            if not tb_sources and dep_design.tb.sources:
                tb_sources = tuple(entry.placed(src) for src in dep_design.tb.sources)
            if not tb_top and dep_design.tb.top:
                tb_top = dep_design.tb.top
        invalid = DesignValidationError
        owners: dict[str, str | None] = {}
        for src in sources:
            if src.generated and not src.located:
                name = str(src.output_name)
                if owners.setdefault(name, src.owner) != src.owner:
                    raise invalid(
                        [
                            (
                                "dependencies",
                                f"two generators declare the generated source `{name}`: the "
                                "design and its dependencies must name the outputs of their "
                                "generators apart",
                                "",
                                "value_error",
                            )
                        ],
                        design_root=self.root_path,
                        design_name=self.name,
                    )
        rtl = {name: getattr(self.rtl, name) for name in Rtl.model_fields}
        rtl.update(sources=sources, top=top, parameters=parameters, clocks=clocks)
        tb = {name: getattr(self.tb, name) for name in Testbench.model_fields}
        tb.update(sources=tb_sources, top=tb_top)
        try:
            with loading_in_design_root(self.root_path):
                checked_rtl = Rtl.model_validate(rtl)
                checked_tb = Testbench.model_validate(tb, context={"merged": True})
        except ValidationError as e:
            raise DesignValidationError(
                _design_errors(e.errors()), design_root=self.root_path, design_name=self.name
            ) from e
        result = self.model_copy(
            update={
                "rtl": checked_rtl,
                "tb": checked_tb,
                "dependencies": (),
                "generation_units": tuple(units.values()),
                "resolved_dependencies": tuple(resolved),
            }
        )
        result.invalidate_cached_properties()
        return result

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
    ) -> List[Source]:
        source_types_str = [str(st).lower() for st in source_types]
        sources: List[Source] = []
        if rtl:
            sources.extend(self.rtl.sources)
        if tb and self.tb:
            sources.extend([src for src in self.tb.sources if src not in self.rtl.sources])
        if len(source_types) == 1 and isinstance(source_types[0], str) and source_types[0] == "*":
            return sources
        return [src for src in sources if str(src.type).lower() in source_types_str]

    def sim_sources_of_type(self, *source_types: Union[str, SourceType]) -> List[Source]:
        if not self.tb:
            return []
        return self.sources_of_type(*source_types, rtl=True, tb=True)

    @property
    def sim_sources(self) -> List[Source]:
        return self.sim_sources_of_type(
            SourceType.Verilog, SourceType.SystemVerilog, SourceType.Vhdl
        )

    @property
    def sim_tops(self) -> Tuple012:
        if self.tb:
            if self.tb.cocotb is not None and self.rtl.top:
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
        """Remove the keys `names` from the design data `data`,"""
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
        packaged: bool = False,
    ) -> DesignType:
        """Load and validate a design from a TOML, JSON or YAML file.

        `target` selects one of the design's `targets` (`select_target`). Without it, the design
        is loaded with no target. `overrides` are applied last, over the selected target.

        `packaged` is for the document that the local xeda wrote for a remote host
        (`xeda.remote.packaging`) and only for the remote launch entry. Such a document holds
        the design as its target made it, with the name of that target in `target`, so that the
        remote plans the run directories and identities of the local plan. A design file written
        by a user never has the key, whatever its name. The document also writes each name of a
        table of tool or netlist names with its dots escaped (`dataclass.ESCAPED_DOT`), and the
        loader reads that text as the merged name (`dataclass.AtomicName`). So the remote gets the
        tables as the local layers merged them, even when they hold `A` and `A.B`. A user's design
        file may not have such a key.

        A file that cannot be read as a design raises `DesignFileParseError`. A file whose design
        does not validate raises `DesignValidationError`. Both errors name the file.
        """
        if overrides is None:
            overrides = {}
        if remove_extra is None:
            remove_extra = []
        # Read the working directory here, in the call that receives the file, and nowhere later.
        design_file = Path(design_file).absolute()
        absolute_file = design_file
        design_dict = _read_design_file(design_file)
        if allow_extra:
            cls = model_with_allow_extra(cls)
        try:
            recorded_target = None
            if not packaged and "dependency_commits" in design_dict:
                raise DesignValidationError(
                    [
                        (
                            "dependency_commits",
                            "Xeda writes `dependency_commits` for a remote host; a design file "
                            "does not have it: delete it, and pin a Git dependency with `commit`",
                            "",
                            "value_error",
                        )
                    ],
                    data=dict(design_dict),
                )
            if packaged:
                recorded_target = design_dict.pop("target", None)
                problem = None if recorded_target is None else target_name_problem(recorded_target)
                if problem:
                    raise DesignValidationError(
                        [("target", problem, "", "value_error")], data=dict(design_dict)
                    )
            design_dict = cls.target_selected(design_dict, target, overrides)
            if packaged:
                # the names of a table that layers merged, `A` and `A.B` (`dataclass.AtomicName`)
                if "flows" in design_dict:
                    design_dict["flows"] = escaped_names_atomic(design_dict["flows"])
            if recorded_target is not None:
                design_dict["target"] = recorded_target
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
            if design_root is None and isinstance(dr, (str, os.PathLike)) and dr:
                # A relative `design_root` in the file is relative to the file.
                dr = absolute_file.parent / dr
            if design_root is None:
                design_root = dr
            if design_root is None:
                design_root = design_file.parent
            design = cls(design_root=design_root, **design_dict)
            design._source_file = design_file.absolute()
            return design
        except DesignValidationError as e:
            raise DesignValidationError(  # add design_file to the emitted exception
                e.errors,
                data=e.data,
                design_root=e.design_root,
                design_name=e.design_name,
                file=str(absolute_file),
            ) from e
        except Exception as e:
            log.error("Error processing design file: %s", absolute_file)
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
        for path, message, kind in removed_inputs_in(cls, overrides):
            raise DesignValidationError([(".".join(path), message, "", kind)], data=dict(data))
        try:
            return cls._merge_target(
                cls.with_default_rtl(deepcopy(selected)), overrides, append=False
            )
        except (ValueError, XedaException) as e:
            raise DesignValidationError(
                [("flows", str(e), "", "value_error")], data=dict(data)
            ) from e

    def source_path_as_named(self, src: FileResource) -> Path:
        """Return `src` as this design names it.

        The path is relative to the design root when `src` is under it, and otherwise the path
        that the design file wrote. A generated source is named by its name in the output of its
        producer, because its file lies in a storage or a run directory whose place depends on
        the run root."""
        if isinstance(src, Source) and src.generated:
            return Path(str(src.output_name))
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

        def place(source: FileResource) -> str:
            # A generated source is told apart by its name in the output of its producer: its
            # file lies where the run root puts it.
            if isinstance(source, Source) and source.generated:
                return f"generated:{source.output_name}"
            return str(source.file.resolve())

        this = place(src)
        if any(
            place(other) != this and self._folded_source_name(other) == name
            for other in (*self.rtl.sources, *self.tb.sources)
        ):
            name += "_" + hashlib.sha256(this.encode()).hexdigest()[:8]
        return name + suffix

    def _folded_source_name(self, src: FileResource) -> str:
        """Return the path of `src`, as this design names it, as one filename-safe token."""
        parts = (
            re.sub(r"\W+", "_", part).strip("_")
            for part in self.source_path_as_named(src).with_suffix("").parts
        )
        return "_".join(part for part in parts if part)

    def part_metadata(self, part: str) -> Dict[str, Any]:
        """Return the metadata of a design part (`rtl` or `tb`): everything but its sources.

        This is what the identity of a run counts of a part, beside the places and types of its
        sources (`flow_runner.configuration`), and what the freshness fingerprint counts beside
        their content. It reads no file. Every value is the validated one: the clocks are
        complete, and so is the `cocotb` table."""
        if part == "rtl":
            return {
                "top": self.rtl.top,
                "parameters": self._parameters_fingerprint(self.rtl),
                "defines": thaw(self.rtl.defines),
                "clocks": thaw(self.rtl.clocks),
                "attributes": thaw(self.rtl.attributes),
            }
        if part == "tb":
            return {
                "top": self.tb.top,
                "parameters": self._parameters_fingerprint(self.tb),
                "defines": thaw(self.tb.defines),
                "uut": self.tb.uut,
                "cocotb": thaw(self.tb.cocotb),
            }
        raise ValueError(f"{part!r} is not a part of a design (its parts are rtl and tb)")

    def _part_fingerprint(
        self, part: str, source_types: Collection[SourceType] | None = None
    ) -> Dict[str, Any]:
        """Return the fingerprint of a part (`rtl` or `tb`), with the sources of `source_types`
        only if they are given (all of them otherwise).

        A source outside `source_types` is not read, so a generated source that has no file yet
        is no obstacle to the fingerprint of a flow that does not take its type."""
        sources = [
            self._source_fingerprint(src)
            for src in getattr(self, part).sources
            if source_types is None or src.type in source_types
        ]
        fingerprint = {"sources": sources, **self.part_metadata(part)}
        if part == "rtl":
            fingerprint["language"] = thaw(self.language)
        return fingerprint

    @property
    def rtl_fingerprint(self) -> Dict[str, Any]:
        """Location-independent inputs that can change RTL compilation or synthesis.

        Source order is significant (notably for VHDL), and the source type tells tools how to
        compile identical bytes. Every source's path counts relative to the design root
        (`_source_fingerprint`), as does a parameter whose value is a path
        (`_parameters_fingerprint`). Moving the whole design does not change its identity.
        """
        return self._part_fingerprint("rtl")

    @property
    def tb_fingerprint(self) -> Dict[str, Any]:
        """The location-independent testbench inputs that identify a run."""
        return self._part_fingerprint("tb")

    def _parameters_fingerprint(self, dv: DesignPart) -> Dict[str, Any]:
        """Return the parameter values as the tool sees them.

        A path under the design root counts relative to it (`$DESIGN_ROOT/rom.mem`), the rule
        that `flowrun_hash` applies to settings. So a design that is given a file under its root
        keeps its identity wherever it is moved. A path outside the root is the location that
        the design names, and counts as such.

        A remote launch moves some of the paths. Then the host counts the value as the client
        does (`path_identity`, role `parameter`).
        """
        # A path in the checkout of a Git dependency counts relative to it, first: the checkout
        # may lie below the design root (a run root there), and holds the commit in its name.
        roots = [
            (f"DEPENDENCY_{index}", record.checkout)
            for index, record in enumerate(self.resolved_dependencies)
            if record.checkout is not None
        ]
        roots += [("DESIGN_ROOT", self.root_path)] if self.design_root else []

        def counted(value: Any) -> Any:
            value = thaw(value)
            known = path_identity(value, "parameter") if isinstance(value, str) else None
            return location_free(value, roots) if known is None else known

        return {k: counted(v) for k, v in dv.parameters.items()}

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

    def _source_fingerprint(self, source: Source) -> Dict[str, Any]:
        """Return what identifies a source: its content, its type and its place: its path relative
        to the design root, or for a generated file, its name in the output of its producer
        (`Source.output_name`).

        A source's location may affect include resolution, commands in a sourced constraint or
        script, and tool lookup. Counting every declared source by path avoids reusing a cached
        result for a different layout.

        The path is always relative to the root, outside it too (`../lib/defs.vh`). A lookup by
        place depends on where files sit relative to each other, so moving a design together with
        what it names keeps its identity, and so does any spelling of the path.
        """
        fingerprint: Dict[str, Any] = {
            "content": source.content_hash,
            "type": str(source.type) if source.type is not None else None,
        }
        if source.generated:
            # A generated file: where it sits in the output of its producer.
            fingerprint["generated"] = source.output_name
        else:
            index = source.dependency
            if index is not None and self.resolved_dependencies[index].checkout is not None:
                # A file in a Git checkout: its place in the run root holds the commit, so the
                # fingerprint counts its path in the checkout.
                fingerprint["path"] = self.source_place(source)
                fingerprint["dependency"] = index
            else:
                known = path_identity(source.file, "source")
                fingerprint["path"] = (
                    known if known is not None else root_relative_text(self.root_path, source.file)
                )
        return fingerprint

    def source_place(self, source: Source) -> str:
        """Return the place of `source` as the identity of a run counts it: its path from the
        design root, as POSIX text (`root_relative_text`).

        A remote launch moves the sources outside the design root. Then the host counts the
        place as the client does (`path_identity`, role `source`). A generated source has no path
        from the design root: its place is its name in the output of its producer (the name or
        pattern that a design file declares). A file of a dependency has its path in the root of the
        dependency (`Source.dependency_place`): the place of a Git checkout in the run root holds
        the commit, which the identity of a run does not count."""
        if source.generated:
            assert source.output_name is not None
            return source.output_name
        known = path_identity(source.file, "source")
        if known is not None:
            return known
        if source.dependency_place is not None:
            return source.dependency_place
        return root_relative_text(self.root_path, source.file)

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
            return semantic_hash(self._part_fingerprint(part, source_types))[:32]

        return semantic_hash(
            {f"{part}_hash": part_hash(part) for part in _DESIGN_PART_ORDER if part in wanted}
        )
