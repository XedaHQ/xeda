from __future__ import annotations

import errno
import hashlib
import inspect
import json
import logging
import os
import pprint
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from copy import deepcopy
from enum import Enum
from functools import cached_property
from glob import escape as glob_escape
from glob import glob, has_magic
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
    model_validator,
    model_with_allow_extra,
    validation_errors,
)
from .proc_utils import tool_output_redirect
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

log = logging.getLogger(__name__)

__all__ = [
    "AnyDesignValidationException",
    "Clock",
    "Design",
    "DesignFileParseError",
    "DesignSource",
    "DesignValidationError",
    "FileResource",
    "LanguageSettings",
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
        self.file = str(Path(file).absolute())
        self.reason = reason
        self.line = line
        self.column = column
        super().__init__(self.file, reason, line, column)  # rebuilt from `args` when unpickled

    def __str__(self) -> str:
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


def _yaml_error_position(e: yaml.MarkedYAMLError) -> tuple[str, int | None, int | None]:
    """`(message, line, column)` of a YAML error, 1-based: where the parser found the problem,
    with where the enclosing construct began (if it did) in the message."""
    parts = []
    if e.context:
        context = e.context
        if e.context_mark is not None and e.context_mark is not e.problem_mark:
            context += f" (line {e.context_mark.line + 1}, column {e.context_mark.column + 1})"
        parts.append(context)
    if e.problem:
        parts.append(e.problem)
    if e.note:
        parts.append(f"note: {e.note}")
    mark = e.problem_mark or e.context_mark  # PyYAML marks are 0-based
    reason = ": ".join(parts) or "invalid YAML"
    if mark is None:
        return reason, None, None
    return reason, mark.line + 1, mark.column + 1


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
            data = yaml.safe_load(path.read_bytes())
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
        raise DesignFileParseError(path, *_yaml_error_position(e)) from None
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


def _expand_source_glob(pattern: str, root: Path, what: str = "source") -> List[str]:
    """The files a source pattern names, in a stable order.

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
    """Files matching a pattern, with variable values treated as literal path components."""
    expanded = _expand_design_path(pattern, root, escape=glob_escape)
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
        if isinstance(src, str) and has_magic(src):
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
        args = generator.args
        return (
            generator.command
            or (args if isinstance(args, str) else " ".join(str(part) for part in args))
            or generator.name
        )
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
            with open(self.file, "rb") as f:
                # the first 128 bits is more than enough
                return hashlib.sha3_256(f.read()).hexdigest()[:32]
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
        if not isinstance(other, FileResource):
            return False
        return self.file == other.file

    def __hash__(self) -> int:
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
    self-describing. NOTE: the declaration order is significant, see `from_str`.
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


#: Sources other files find by their name or place, so where one sits relative to the design
#: root is part of the design (`Design._source_fingerprint`): a Verilog `include` searches the
#: including file's directory, then the header directories; a Bluespec package, a C++ header, a
#: cocotb module and a memory file are found by name on a search path. VHDL, constraints and
#: scripts are named explicitly wherever they sit, and count by content alone.
_LOCATED_SOURCE_TYPES = frozenset(
    {
        SourceType.Verilog,
        SourceType.VerilogHeader,
        SourceType.SystemVerilog,
        SourceType.SVHeader,
        SourceType.Bluespec,
        SourceType.Cpp,
        SourceType.Cocotb,
        SourceType.MemoryFile,
    }
)


class DesignSource(FileResource):
    def __init__(
        self,
        path: Union[str, os.PathLike, Dict[str, str]],
        typ: Union[str, SourceType, None] = None,
        standard: Optional[str] = None,
        variant: Optional[str] = None,
        _root_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(path, dict):
            typ = typ or path.pop("type", None)
            standard = standard or path.pop("standard", None)
            variant = variant or path.pop("variant", None)
            rp = path.pop("root_path", None)
            if not _root_path and rp:
                _root_path = Path(rp)
        super().__init__(path, _root_path=_root_path, **kwargs)

        def type_from_suffix(path: Path) -> Tuple[Optional[SourceType], Optional[str]]:
            type_variants_map = {
                (SourceType.Chisel, None): ["sc"],
                (SourceType.Cpp, None): ["cc", "cpp", "cxx"],
                (SourceType.Vhdl, None): ["vhd", "vhdl"],
                (SourceType.Verilog, None): ["v"],
                (SourceType.VerilogHeader, None): ["vh"],
                (SourceType.SVHeader, None): ["svh"],
                (SourceType.SystemVerilog, None): ["sv"],
                (SourceType.Bluespec, "bsv"): ["bsv"],
                (SourceType.Bluespec, "bh"): ["bs", "bh"],
                (SourceType.Xdc, None): ["xdc"],
                (SourceType.Sdc, None): ["sdc"],
                (SourceType.Tcl, None): ["tcl"],
                (SourceType.Cocotb, None): ["py"],
                (SourceType.MemoryFile, None): ["mem"],
            }
            for (typ, vari), suffixes in type_variants_map.items():
                if path.suffix[1:] in suffixes:
                    return (typ, vari)
            return None, None

        self.variant = variant
        self.type = None
        if isinstance(typ, SourceType):
            self.type = typ
        elif isinstance(typ, str):
            self.type = SourceType.from_str(typ)
        if not self.type:
            self.type, self.variant = type_from_suffix(self.file)
        self.standard = standard
        # What the design *stated*, as opposed to what the filename implied. Only the former is
        # written back out: reloading re-infers the latter from the same suffix, while a stated
        # `type` that contradicts it (`{ file = "legacy.v", type = "SystemVerilog" }`) is lost
        # for good if the dump leaves it out -- and it is part of the design's identity.
        self._stated: Dict[str, Any] = {
            key: value
            for key, value in (
                ("type", str(self.type) if typ is not None and self.type is not None else None),
                ("standard", standard),
                ("variant", variant),
            )
            if value is not None
        }

    def as_json_value(self) -> Union[str, Dict[str, Any]]:
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


class DVSettings(XedaBaseModel):
    """Design/Verification settings"""

    sources: List[DesignSource]
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
        def src_with_type(src, src_type):
            if src_type:
                return {"file": src, "type": src_type}
            return src

        def ds(src: Union[str, Path], typ=None):
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
                if has_magic(src):
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
    args: Union[str, List[str]] = []
    command: Optional[str] = None
    check: bool = True
    env: Optional[Dict[str, str]] = None
    # sweepable parameters used in command
    parameters: dict = {}
    sources: List[Union[str, Path]] = Field(
        default_factory=list,
        description="List of design sources used by this generator",
    )
    run_only_if_sources_modified: bool = Field(
        default=True,
        description="If True, the generator will run only if either not all rtl.sources exist or one of the sources has a newer modification time than all the rtl.sources",
    )
    # for xeda to know dependencies, clean previous artifacts, check after generation:
    generated_sources: List[str] = []

    @field_validator("sources", mode="before")
    @classmethod
    def _sources_to_files(cls, value):
        # sources can contain globs which are expanded
        if isinstance(value, str):
            value = [value]
        sources: List[Path] = []
        for src in unique(value):
            if isinstance(src, str):
                if has_magic(src):
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
        # The generator's inputs: whether to rerun it is decided by their modification times.
        missing = [str(src) for src in sources if not src.exists()]
        if missing:
            raise ValueError(f"generator source file does not exist: {', '.join(missing)}")
        return sources

    def run(self):
        if self.command:
            cmd = self.command.split()
        elif not self.executable:
            raise ValueError("executable is not set")
        else:
            cmd = [self.executable, *self.args]
        self.run_cmd(cmd)

    def run_cmd(self, cmd, check=None, stdout=None, stderr=None):
        log.info("Running command: '%s'", " ".join(cmd))
        if stdout is None:
            # A generator's subprocess inherits our stdout, which would corrupt a `--json`
            # document. `tool_output_redirect()` is None unless output has been redirected, so
            # normal runs keep inheriting as before.
            stdout = tool_output_redirect()
        p = subprocess.run(
            cmd,
            cwd=self.cwd,
            check=check if check is not None else self.check,
            stdout=stdout,
            stderr=stderr,
            env=self.env,
        )
        if self.check and p.returncode:
            raise NonZeroExitCode(cmd, p.returncode)
        return p

    @property
    def name(self) -> str:
        return str(self.__class__.__qualname__ or "generator")


class ChiselGenerator(Generator):
    main: Optional[str] = None
    project: Optional[str] = None
    build_system: str = "mill"
    check: bool = True

    def run(self):
        if self.build_system == "mill":
            return self.run_mill()
        elif self.build_system == "bloop":
            return self.run_bloop()
        else:
            raise Exception(f"Unsupported build system: {self.build_system}")

    def run_mill(self):
        # if file ./mill or ./millw exists, use it, otherwise use mill from PATH
        mill_exec = "./mill"
        if not Path(mill_exec).exists():
            mill_exec = "mill"
        if not self.project:
            ValueError("`project` must be specified for Chisel generator")
        cmd = [mill_exec]
        if self.main:
            cmd += [f"{self.project}.runMain", self.main]
        else:
            cmd.append(f"{self.project}.run")
        if self.args:
            if isinstance(self.args, str):
                self.args = self.args.split()
            cmd += self.args
        return self.run_cmd(cmd)

    def run_bloop(self):
        if self.project is None:
            p = self.run_cmd(["bloop", "projects"], stdout=subprocess.PIPE)
            projects_str = p.stdout.decode()
            projects = re.split(r"\s+", projects_str)
            if projects:
                log.info(f"Found projects: {', '.join(projects)}")
                self.project = projects[0]
            else:
                log.error("No projects found!")
                raise ValueError("No projects found!")
        if not self.project:
            ValueError("`project` must be specified for Chisel generator")
        cmd = ["bloop", "run", self.project]
        if self.main:
            cmd += ["--main", self.main]
        if self.args:
            if isinstance(self.args, str):
                self.args = self.args.split()
            cmd.append("--")
            cmd += self.args
        return self.run_cmd(cmd)


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

    sources: List[DesignSource] = []
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


class DesignReference(XedaBaseModel):
    uri: str
    rtl: RtlDep = RtlDep()
    tb: TbDep = TbDep()
    local_cache: Path = Path.cwd() / ".xeda_dependencies"

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
        toml_path = Path(self.uri)
        if not toml_path.exists():
            raise ValueError(f"file {toml_path} does not exist!")
        return Design.from_toml(toml_path)


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
            uri = urlparse(repo_url)
            uri_path = uri.path.lstrip("/.")
            if not uri.netloc:
                raise ValueError(f"invalid URL: {uri}")
            commit = values.get("commit")
            branch = values.get("branch")
            if commit:
                uri_path += "_commit=" + commit
            elif branch:
                uri_path += "_" + branch
            local_cache = values.get("local_cache")
            if local_cache:
                return Path(local_cache) / uri.netloc / uri_path
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

        if not self.clone_dir:
            raise ValueError(f"'clone_dir' not set for GitReference: {self}")
        repo = None
        if self.clone_dir.exists():
            try:
                repo = Repo(self.clone_dir)
                if not repo.git_dir:
                    raise ValueError(f"repo={repo} is missing 'git_dir'")
                log.info("Updating existing git repository at %s", self.clone_dir)
                repo.remotes.origin.fetch()
                if not self.commit:
                    repo.git.pull()
            except git.InvalidGitRepositoryError:
                log.error("Path %s is not a valid git repository.", self.clone_dir)
        if repo is None:
            log.info(
                "Cloning git repository url:%s branch:%s commit:%s",
                self.repo_url,
                self.branch,
                self.commit,
            )
            repo = Repo.clone_from(
                self.repo_url,
                self.clone_dir,
                depth=1,
                branch=self.branch,
            )
        if repo is None:
            ValueError("repo is None!")
        if self.commit:
            log.info("Checking out commit: %s", self.commit)
            repo.git.checkout(self.commit)
        elif self.branch:
            log.info("Checking out branch: %s", self.branch)
            repo.git.checkout(self.branch)

        toml_path = self.clone_dir / self.design_file
        return Design.from_toml(toml_path)


DesignType = TypeVar("DesignType", bound="Design")


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
    def process_compatibility(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if "rtl" not in data:
            clock_inputs = {
                name: data.pop(name) for name in ("clock", "clock_port", "clocks") if name in data
            }
            # Keep multiple spellings if they were written together: the corresponding model
            # validator then reports the ambiguity instead of silently choosing one.
            parameters = {
                name: data.pop(name) for name in ("parameters", "generics") if name in data
            }
            data["rtl"] = {
                "sources": data.pop("sources", []),
                "generator": data.pop("generator", None),
                **parameters,
                "defines": data.pop("defines", {}),
                "top": data.pop("top", None),
                **clock_inputs,
            }
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
    def process_generation(cls, data: Dict[str, Any]):
        design_root = data.get("design_root")
        if not design_root:
            design_root = Path.cwd()
        else:
            design_root = Path(design_root)
        rtl = data.get("rtl", {})
        assert isinstance(rtl, dict), f"rtl must be a dictionary, but found {type(rtl)}"
        generator = rtl.pop("generator", None)
        if generator:
            with WorkingDirectory(design_root):
                # A generator is told the design root as `$DESIGN_ROOT` names it in the design's
                # own paths: replacing whatever the shell exports, which is another directory's.
                env = {**os.environ, "DESIGN_ROOT": str(design_root)}
                if isinstance(generator, str):
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
                    if generator.cwd is None:
                        generator.cwd = str(design_root)
                    if generator.env is None:
                        generator.env = dict(env)
                    else:
                        # An `env` the design states is the generator's whole environment, and
                        # a `DESIGN_ROOT` in it is the design's own word.
                        generator.env.setdefault("DESIGN_ROOT", str(design_root))
                    skip_run = False
                    rtl_sources = _source_paths_as_given(rtl.get("sources", []), design_root)
                    if generator.run_only_if_sources_modified and generator.sources and rtl_sources:
                        log.debug("Generator sources: %s", generator.sources)
                        # check if rtl.sources exist and if they are newer than the generator sources
                        if all(src.exists() for src in rtl_sources):
                            generator_sources_last_modified = max(
                                Path(gen_src).stat().st_mtime for gen_src in generator.sources
                            )
                            if all(
                                src.stat().st_mtime >= generator_sources_last_modified
                                for src in rtl_sources
                            ):
                                skip_run = True
                                log.info(
                                    "Skipping generator '%s' run, as all rtl.sources are newer than the generator sources",
                                    generator.name,
                                )
                            else:
                                log.info(
                                    "Running generator '%s' as some rtl.sources are older than the generator sources",
                                    generator.name,
                                )
                        else:
                            log.info(
                                "Running generator '%s' as not all rtl.sources exist",
                                generator.name,
                            )
                    if not skip_run:
                        log.info("Running generator: %s", generator.name)
                        generator.run()
                else:
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
                if pos == -1:  # -1 means append 'after' the last element
                    self.rtl.sources.extend(dep_design.rtl.sources)
                    if not self.rtl.top and dep_design.rtl.top:
                        self.rtl.top = dep_design.rtl.top
                    if not self.rtl.parameters and dep_design.rtl.parameters:
                        self.rtl.parameters = dep_design.rtl.parameters
                    if not self.rtl.clocks and dep_design.rtl.clocks:
                        self.rtl.clocks = dep_design.rtl.clocks
                else:
                    if pos < 0:
                        pos += 1  # afterwards: pos=-2 means the position 'before' the last element
                    self.rtl.sources[pos:pos] = dep_design.rtl.sources
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
    def from_toml(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[str, os.PathLike, None] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
    ) -> DesignType:
        return cls.from_file(
            design_file,
            design_root=design_root,
            overrides=overrides,
            allow_extra=allow_extra,
            remove_extra=remove_extra,
        )

    @classmethod
    def from_file(
        cls: Type[DesignType],
        design_file: Union[str, os.PathLike],
        design_root: Union[str, os.PathLike, None] = None,
        overrides: Optional[Dict[str, Any]] = None,
        allow_extra: bool = False,
        remove_extra: Optional[List[str]] = None,
    ) -> DesignType:
        """Load and validate a design description from a TOML, JSON or YAML file.

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
        design_dict = hierarchical_merge(design_dict, overrides)
        if "name" not in design_dict:
            design_name = design_file.stem
            design_name = removesuffix(design_name, ".xeda")
            log.debug(
                "'design.name' not specified! Inferring design name: `%s` from design file name.",
                design_name,
            )
            design_dict["name"] = design_name
        if allow_extra:
            cls = model_with_allow_extra(cls)
        else:
            for k in remove_extra:
                design_dict.pop(k, None)
        # Default value for design_root is the folder containing the design description file.
        dr = design_dict.pop("design_root", None)
        if design_root is None:
            design_root = dr
        if design_root is None:
            design_root = design_file.parent
        try:
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
        compile identical bytes. The fingerprint names no path at all, so moving the design, or
        laying the same files out differently, does not change its identity.
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
        """What a source *is*: its content, type and compile metadata -- and, for a source
        that other files find by its name or place (`_LOCATED_SOURCE_TYPES`), where it is
        relative to the design root.

        For most sources the location does not change what they mean: the same bytes compiled
        in the same order under the same metadata are the same design wherever they sit. But a
        Verilog `include` is resolved by place -- the including file's directory first, then
        the header directories a flow builds from the sources -- and a Bluespec package, a C++
        header, a cocotb module or a memory file is found by name on a search path. Two layouts
        of the same files can then build different results, and counted by content alone they
        were one design, so a cached run of one was reused for the other.

        Relative to the root, never absolute: moving a whole design keeps its identity. A source
        outside the root counts by the path the design wrote (`source_path_as_named`).
        """
        fingerprint = {
            "content": source.content_hash,
            "type": str(source.type) if source.type is not None else None,
            "standard": source.standard,
            "variant": source.variant,
        }
        if source.type in _LOCATED_SOURCE_TYPES:
            fingerprint["path"] = self.source_path_as_named(source).as_posix()
        return fingerprint

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

    # pylint: disable=arguments-differ
    def model_dump(self, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        """The design as it was *given*, not as it was filled in.

        Every dump of a design is something a design gets rebuilt from -- `settings.json`, the
        archive `send_design` ships -- so it records what the design file said and leaves out
        what merely defaulted.

        xeda writes a design as JSON through this method (`utils.json_encodable` calls
        `model_dump(mode="json")`), and `model_dump_json` below prunes the same way, so a design
        is one document however it is serialized. Neither passes `serialize_as_any`: it skips
        the serializer `DesignSource` declares on its own schema.
        """
        kwargs.setdefault("exclude_unset", True)
        kwargs.setdefault("exclude_defaults", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:  # type: ignore[override]
        """`model_dump(mode="json")` as text: pruned the same way, so it is the same document."""
        kwargs.setdefault("exclude_unset", True)
        kwargs.setdefault("exclude_defaults", True)
        return super().model_dump_json(**kwargs)
