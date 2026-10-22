"""Utility functions and classes of Xeda."""

import errno
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import tomllib
import unittest
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from enum import Enum
from functools import cached_property, reduce
from pathlib import Path, PurePath
from types import MappingProxyType, TracebackType
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from xml.etree import ElementTree

from varname import argname  # type: ignore

from .dataclass import AtomicName, BaseModel, XedaBaseModel
from .units import Quantity

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
    "model_state",
    "toml_loads",
    "parse_xml",
    "try_convert_to_primitives",
    "try_convert",
    # list/container utils
    "unique",
    "rebuild_like",
    "location_free",
    "location_roots",
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
    "flows_table_problems",
    "ConflictingKeys",
    "restore_exception",
    "EscapedName",
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
    """Standard Delay Format (SDF) files for a simulation, one for each delay type.

    `root` is the region where the SDF applies. It is `/TESTBENCH/UUT` when `tb.top` is `TESTBENCH`
    and the simulation netlist is instantiated in the testbench with the instance name `UUT`.
    """

    root: Optional[str] = None
    min: Optional[Path] = None
    max: Optional[Path] = None
    typ: Optional[Path] = None

    def delay_items(self) -> Iterable[Tuple[str, Union[Path, None]]]:
        """Return the `(delay_type, sdf_file)` pairs for the delay types that have a file."""
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


#: The permissions `open(path, "w")` gives a file it creates.
_UMASK = os.umask(0o022)
os.umask(_UMASK)
_CREATE_MODE = 0o666 & ~_UMASK


def _discard(temporary: str) -> None:
    """Remove a temporary file that Xeda made beside a name and did not put there."""
    Path(temporary).unlink(missing_ok=True)  # the temporary file, never committed


@contextmanager
def replacing_file(
    path: Union[str, os.PathLike],
    mode: str = "w",
    encoding: Optional[str] = None,
    *,
    copy_mode_from: Optional[Union[str, os.PathLike]] = None,
) -> Iterator[IO[Any]]:
    """Open `path` for writing like `open(path, mode)`, but replace what is at `path` and never
    write through it, a symbolic link included. The content goes to a temporary file beside
    `path`. Once the body has completed, `os.replace` renames the temporary file over `path`, so
    `path` is always a complete file. If the body raises, the temporary file is removed and `path`
    stays as it was. `copy_mode_from`, when given, sets the permission bits of the temporary file
    to those of that path (`shutil.copymode`) before the commit. If reading those bits fails,
    `path` stays untouched and is not replaced with the wrong mode."""
    if mode not in ("w", "wt", "wb"):
        raise ValueError(f"replacing_file writes a file anew; mode {mode!r} is not one of w, wb")
    target = Path(path)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    committed = False
    try:
        os.chmod(temporary, _CREATE_MODE)
        with os.fdopen(fd, mode, encoding=None if "b" in mode else encoding) as f:
            yield f
        if copy_mode_from is not None:
            shutil.copymode(copy_mode_from, temporary)
        os.replace(temporary, target)
        committed = True
    finally:
        if not committed:
            _discard(temporary)


@contextmanager
def live_log(path: Union[str, os.PathLike], encoding: str = "utf-8") -> Iterator[IO[str]]:
    """Open the log of a tool at its own name from the start and flush it line by line, so that
    `tail -F` shows a long run as it goes.

    Each run replaces the file at the name. `tail -F` follows the new file, and `tail -f` keeps
    the one it opened. This is the one write that Xeda does not make complete and then rename. A
    log is meant to be watched while it grows, and the partial log of a failed tool is its
    diagnostic, so it is kept as it stands.

    The log is a new file. Xeda makes it beside `path` and renames it onto `path` at once
    (`os.replace`), before anything is written. Every line then goes to the descriptor of that
    file, never to the name. So whatever was at `path` is replaced as a name and never written
    through. That includes a symbolic link, or a hard link whose inode a file outside the run
    directory may share, which a tool may have left there. A log that cannot be made raises here,
    so a caller that makes it before it starts the tool starts nothing. The caller has located
    `path` in the run directory (`RunDirectory.writable`)."""
    target = Path(path)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.chmod(temporary, _CREATE_MODE)
        os.replace(temporary, target)
    except BaseException:
        os.close(fd)
        _discard(temporary)
        raise
    with os.fdopen(fd, "w", encoding=encoding, buffering=1) as f:
        yield f


#: The largest byte count one `copy_file_range` or `sendfile` call is asked for (the kernel
#: clamps it anyway: `sendfile` to 0x7ffff000).
_FAST_COPY_CHUNK = 1 << 30
#: The read/write loop's buffer.
_LOOP_CHUNK = 1 << 20
#: `COPYFILE_DATA` of macOS's <copyfile.h> (`posix._COPYFILE_DATA` in CPython): the data fork only.
_COPYFILE_DATA = 1 << 3


def _copy_with_copy_file_range(src_fd: int, dst_fd: int, size: int) -> None:
    """Copy `size` bytes with `os.copy_file_range` (Linux), at explicit offsets. The call may copy
    fewer bytes than asked, so the function loops. A return of 0 before `size` bytes means that
    the file is shorter than it was said to be, or that the file system does not support the
    call. The function raises in that case."""
    copied = 0
    while copied < size:
        n = os.copy_file_range(  # type: ignore[attr-defined,unused-ignore]
            src_fd, dst_fd, min(size - copied, _FAST_COPY_CHUNK), copied, copied
        )
        if n == 0:
            raise OSError(errno.EIO, "copy_file_range copied nothing before the end of the file")
        copied += n


def _copy_with_sendfile(src_fd: int, dst_fd: int, size: int) -> None:
    """Copy `size` bytes with `os.sendfile` (Linux), at an explicit offset, in a loop like
    `_copy_with_copy_file_range`."""
    copied = 0
    while copied < size:
        n = os.sendfile(dst_fd, src_fd, copied, min(size - copied, _FAST_COPY_CHUNK))
        if n == 0:
            raise OSError(errno.EIO, "sendfile copied nothing before the end of the file")
        copied += n


def _copy_with_fcopyfile(src_fd: int, dst_fd: int, size: int) -> None:
    """Copy the data of `src_fd` into `dst_fd` with `fcopyfile(3)` and `COPYFILE_DATA` (macOS),
    from the current offsets of both descriptors (the caller has rewound them). This is a plain
    data copy: on descriptors it never clones. The call returns 0 or -1 and has no partial
    result, but the function still checks the size of the destination. It calls `fcopyfile`
    through `ctypes`, so it needs no private CPython API."""
    try:
        import ctypes

        fcopyfile = ctypes.CDLL(None, use_errno=True).fcopyfile
    except (ImportError, AttributeError) as e:  # no ctypes, or no such function: not available
        raise OSError(errno.ENOSYS, f"fcopyfile is not available: {e}") from e
    fcopyfile.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    fcopyfile.restype = ctypes.c_int
    if fcopyfile(src_fd, dst_fd, None, _COPYFILE_DATA) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if os.fstat(dst_fd).st_size != size:
        raise OSError(errno.EIO, "fcopyfile copied fewer bytes than the file holds")


def _fast_copies(platform: str = sys.platform) -> List[Callable[[int, int, int], None]]:
    """Return the kernel copy primitives of `platform`, best first. The choice follows what the
    interpreter reports and imports nothing that could fail: `os.copy_file_range`, then
    `os.sendfile` on Linux, `fcopyfile` on macOS, none elsewhere (a plain loop does the work)."""
    if platform.startswith("linux"):
        fast: List[Callable[[int, int, int], None]] = []
        if hasattr(os, "copy_file_range"):
            fast.append(_copy_with_copy_file_range)
        if hasattr(os, "sendfile"):
            fast.append(_copy_with_sendfile)
        return fast
    if platform == "darwin":
        return [_copy_with_fcopyfile]
    return []


def _rewind(src_fd: int, dst_fd: int, *, source_is_file: bool) -> None:
    """Put both descriptors at offset 0 and leave `dst_fd` empty. Every attempt starts from this
    state, so what an earlier attempt wrote before it failed is never part of the result."""
    if source_is_file:
        os.lseek(src_fd, 0, os.SEEK_SET)
    os.lseek(dst_fd, 0, os.SEEK_SET)
    if os.fstat(dst_fd).st_size:
        os.ftruncate(dst_fd, 0)


def copy_fd(src_fd: int, dst_fd: int) -> None:
    """Copy the whole content of the open file `src_fd` into the open, writable file `dst_fd`,
    replacing what `dst_fd` holds.

    This is `shutil.copyfileobj` between two descriptors, through the copy primitive of the
    platform (`os.copy_file_range`, then `os.sendfile`, on Linux; `fcopyfile` on macOS). The
    destination is a separate file with the same logical content. On Linux, `copy_file_range`
    may use file-system reflinks (shared copy-on-write extents) or a server-side copy.
    Permission bits are not copied.

    A source that is a regular file is read from its start, whatever its offset is. Every
    attempt starts from a rewound source and an emptied destination (`_rewind`). Any `OSError`
    from a kernel primitive makes the next primitive, and last the plain loop, start again from
    scratch. Such an error means that the primitive is not there (`ENOSYS`, `EINVAL`, `EBADF`,
    `EOPNOTSUPP`, or `EPERM` in a sandbox), that it refuses the pair (`EXDEV` across file
    systems), or that it fails after it copied some bytes. A primitive that stops short, or
    copies nothing before the end of a file that is not empty, counts as a failure in the same
    way. A real error (`ENOSPC`, `EIO`) happens again in the loop, which raises it. An empty file
    needs no primitive. A pipe or another source that cannot be rewound goes straight to the
    loop. The result is complete or an exception. The offset of the destination afterwards is
    not defined."""
    st = os.fstat(src_fd)
    source_is_file = stat.S_ISREG(st.st_mode)
    if source_is_file and st.st_size > 0:
        for fast in _fast_copies():
            _rewind(src_fd, dst_fd, source_is_file=True)
            try:
                fast(src_fd, dst_fd, st.st_size)
                return
            except OSError as e:
                log.debug("%s failed (%s): trying the next way to copy", fast.__name__, e)
    _rewind(src_fd, dst_fd, source_is_file=source_is_file)
    while True:
        chunk = os.read(src_fd, _LOOP_CHUNK)
        if not chunk:
            return
        view = memoryview(chunk)
        while view:
            view = view[os.write(dst_fd, view) :]


def replacing_copy(src: Union[str, os.PathLike], dst: Union[str, os.PathLike]) -> Path:
    """Copy `src` to `dst` like `shutil.copy(src, dst)`: the content and the permission bits, into
    `dst` if it is a directory. A file or link at the destination is replaced and never written
    through (`replacing_file`). Return the destination."""
    target = Path(dst)
    if target.is_dir() and not target.is_symlink():
        target = target / Path(src).name
    with open(src, "rb") as source, replacing_file(target, "wb", copy_mode_from=src) as f:
        copy_fd(source.fileno(), f.fileno())
    return target


def backup_existing(path: Path) -> Optional[Path]:
    if not path.exists():
        log.warning("%s does not exist for backup!", path)
        return None
    modifiedTime = os.path.getmtime(path)
    suffix = f".backup_{datetime.fromtimestamp(modifiedTime):%Y-%m-%d-%H%M%S}"
    if path.suffix:
        suffix += path.suffix
    backup_path = path.with_suffix(suffix)
    typ = "file" if path.is_file() else "directory" if path.is_dir() else "???"
    log.debug("Renaming existing %s from '%s' to '%s'", typ, path.name, backup_path.name)
    while backup_path.exists():
        backup_path = backup_path.with_suffix(backup_path.suffix + "_")
    return path.rename(backup_path)


def model_state(obj: Any) -> Dict[str, Any]:
    """Return the field values of a pydantic model, including the keys accepted with
    `extra="allow"`.

    pydantic v2 keeps permitted extras in `__pydantic_extra__` and not in `__dict__`. Reading
    `__dict__` alone would drop every key that a design supplied under `--design-allow-extra`,
    from `settings.json` and from the run hashes. Two different designs would then hash the same.

    `__dict__` also holds the cache of each `cached_property`, so it grows keys such as
    `Tool.version` the first time one is read. Restricting the result to the declared fields
    keeps `semantic_hash()`, and therefore the run directory of a flow, independent of whether a
    cached property was evaluated first.
    """
    extra = getattr(obj, "__pydantic_extra__", None)
    fields = getattr(type(obj), "model_fields", None)
    state = obj.__dict__
    if isinstance(fields, dict):
        state = {k: v for k, v in state.items() if k in fields}
    return {**state, **(extra or {})}


def rebuild_like(container: Any, items: List[Any]) -> Any:
    """Return `items` in the built-in kind of `container`. `type(container)(items)` would not do:
    the constructor of a namedtuple takes its fields positionally, and the constructor of a
    subclass may take anything."""
    if isinstance(container, tuple):
        return tuple(items)
    if isinstance(container, frozenset):
        return frozenset(items)
    if isinstance(container, set):
        return set(items)
    return list(items)


#: The package directory of this installation. A path under it is a file of Xeda, the same on
#: every installation, so the identity of a run counts it relative to this directory
#: (`$XEDA/...`), as it counts a path under the design root relative to that root. This is a
#: module attribute, read at each call.
XEDA_PACKAGE_ROOT = Path(__file__).absolute().parent


def location_roots(
    design_root: Optional[os.PathLike | str] = None, runner_cwd: Optional[os.PathLike | str] = None
) -> list[tuple[str, Path]]:
    """Return the roots that the identity of a run counts paths relative to (`location_free`),
    most specific first: the design root, the directory Xeda was started from, and the
    installation of Xeda."""
    roots = [
        (var, Path(root).absolute())
        for var, root in (
            ("DESIGN_ROOT", design_root),
            ("PWD", runner_cwd),
            ("XEDA", XEDA_PACKAGE_ROOT),
        )
        if root is not None
    ]
    # Specificity is the place's own depth, so a root written as a short link to a deep directory
    # still outranks a shallower one it lies in; the written depth only breaks a tie.
    roots.sort(
        key=lambda var_root: (
            len(Path(os.path.realpath(var_root[1])).parts),
            len(var_root[1].parts),
        ),
        reverse=True,
    )
    return roots


def _spellings(path: PurePath) -> list[PurePath]:
    """Return `path` as written, then as the file system resolves it when that differs. A place
    reached through a symbolic link (a linked prefix, the `/tmp` of macOS) has two spellings."""
    if not path.is_absolute():
        return [path]
    resolved = PurePath(os.path.realpath(path))
    return [path] if resolved == path else [path, resolved]


def location_free(value: Any, roots: list[tuple[str, Path]]) -> Any:
    """Return `value` with every absolute path under one of `roots` rewritten as `$VAR/relative`.

    Each root and each path is recognized as written and as resolved. The function tries `roots`
    in order, most specific first, and the first one that holds the path under any spelling of
    either names it. A place reached through a symbolic link therefore counts as the same place
    however it is spelled."""
    if isinstance(value, dict):
        return {key: location_free(item, roots) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return rebuild_like(value, [location_free(item, roots) for item in value])
    if isinstance(value, PurePath) or (isinstance(value, str) and os.path.isabs(value)):
        spellings = _spellings(PurePath(value))
        # Try the roots in the given order (most specific first), each against every spelling
        # of the path. The first root that holds the place at all names it, however the place
        # is written. With the roots in the outer loop, a less specific root cannot claim one
        # spelling of a path that a more specific root holds under the other.
        for var, root in roots:
            for root_spelling in _spellings(root):
                for path in spellings:
                    if path.is_relative_to(root_spelling):
                        relative = path.relative_to(root_spelling).as_posix()
                        return f"${var}" if relative == "." else f"${var}/{relative}"
    return value


def json_encodable(obj: Any) -> Any:
    """Return a form of `obj` that `json` can encode, for an object it cannot encode by itself.

    A pydantic model goes through pydantic (`mode="json"`), so every serializer that its fields
    declare runs, and what lands in `settings.json` is what the model would validate back.
    Reading `__dict__` would record a `DesignSource` as its raw instance state, private
    attributes included, which nothing can load. It would also skip the `Path`, enum and
    `FileResource` conversions that the models define.

    Any object that is not a model says how it is written by defining `as_json_value()`
    (`FileResource`, `DesignSource`, `FlowOutcome`). An `Enum` is written as its value, as
    pydantic writes one in JSON mode. A set or frozenset is written as a list.

    Nothing is written by reading its `__dict__`. The attributes of an object are not a
    serialization format, and descending into a plain `Enum`, whose `__dict__` carries
    `__objclass__`, does not terminate (`semantic_hash` has a guard against exactly that).
    Anything unrecognized is written as its `str()`, which is legible and cannot recurse.

    This is the one encoder. `introspect.json_safe`, which builds the documents that the CLI
    prints, hands it every value that is not plain data. A value printed under `--json` and the
    same value in a file that Xeda writes therefore cannot differ.
    """
    if isinstance(obj, BaseModel):
        return with_json_keys(obj.model_dump(mode="json"))
    if isinstance(obj, Enum):
        return with_json_keys(obj.value)
    if isinstance(obj, (set, frozenset)):
        return list(obj)  # as `json` itself writes a tuple; `str()` would write `{...}` text
    as_json_value = getattr(obj, "as_json_value", None)
    if callable(as_json_value):
        return with_json_keys(as_json_value())
    return str(obj)


def json_key(key: Any) -> Any:
    """Return a mapping key in a form that `json` can write, by the rule that `json_encodable`
    uses for a value: an enum as its value, anything else as its text (`Path("/a")` -> `"/a"`, a
    tuple as `"('clk', 'rise')"`). `str`, numbers, `bool` and `None` are left to `json`, which
    writes them itself (`true`, `null`, a `str` enum member as its text)."""
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    if isinstance(key, Enum):
        return json_key(key.value)
    return str(key)


def with_json_keys(value: Any) -> Any:
    """Return `value` with every mapping key in its plain containers made a key that `json` can
    write.

    `json` accepts only `str`, numbers, `bool` and `None` as keys. It raises on any other before
    an encoder hook sees it. A `Path` or tuple key in the results of a flow would therefore fail
    `results.json`, and the `--json` document with it, which is the one document that an agent
    reads. Every document that Xeda writes or prints goes through this function (and
    `json_encodable` applies it to what it expands), so no document can fail on a key, and a
    printed document still agrees with the file. A key whose JSON text another key of the same
    mapping already has is qualified with its type, so neither value is dropped.
    """
    if isinstance(value, dict):
        converted: Dict[str, Any] = {}
        for key, item in value.items():
            written = json_key(key)
            # JSON turns even valid non-string keys into strings. Compare the keys as JSON
            # writes them. Otherwise `1` and "1" produce duplicate JSON names, and Path("a")
            # followed by "a" silently overwrites one of the values here.
            name = written if isinstance(written, str) else json.dumps(written)
            if name in converted:
                stem = f"{name} ({type(key).__name__})"
                name = stem
                suffix = 2
                while name in converted:
                    name = f"{stem} {suffix}"
                    suffix += 1
            converted[name] = with_json_keys(item)
        return converted
    if isinstance(value, (list, tuple)):
        return [with_json_keys(item) for item in value]
    return value


def dump_json(data: object, path: Path, backup: bool = True, indent: int = 4) -> None:
    """Write JSON with normalized keys and optional backup."""
    if path.exists() and backup:
        backup_existing(path)
        assert not path.exists(), "Old file still exists!"

    with replacing_file(path) as outfile:
        json.dump(with_json_keys(data), outfile, default=json_encodable, indent=indent)


_TCL_SUBSTITUTED = re.compile(r'([\\\[\]"${}])')


def tcl_quote(value: Any) -> str:
    """Return `value` escaped to stand for itself inside a double-quoted TCL word.

    The function puts a backslash before every character that TCL would substitute (`$`, `[`,
    `\\`), that would end the word (`"`), or that would count as a brace of the script the word
    sits in (`{`, `}`, as in `catch {read_verilog ...}`). Use it for a path in a message
    (`puts "Reading {{src|tcl_quote}}"`). For a whole word, use `tcl_word`."""
    return _TCL_SUBSTITUTED.sub(r"\\\1", str(value))


def tcl_word(value: Any) -> str:
    """Return `value` as one literal TCL word, however it is spelled: a path with a space,
    brackets (`fifo[1].v`) or a `$` reaches the command as it is. Unquoted or double-quoted
    only, such a path is split by TCL, `$v` is read as a variable, and `[x]` runs as a command
    (the TCL of Vivado even starts an external program whose name begins with `x`). The
    templates of every flow have this function."""
    return f'"{tcl_quote(value)}"'


def tcl_list(value: Any) -> str:
    """Return `value`, one path or a list of them, as a TCL expression for the list of them:
    `[list "a b.v"]`. Use it for a command that takes its files as a list and splits a single
    word at its spaces: the `read_verilog`, `read_vhdl`, `read_xdc`, `add_files` and `get_files`
    commands of Vivado."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return "[list " + " ".join(tcl_word(item) for item in items) + "]"


def unique(lst: List[Any]) -> List[Any]:
    """Return the unique elements of the list in their original order (first occurrence).

    It falls back to an equality comparison when the items are not hashable. A design source may be
    given as a table (`{file: "a.vhdl"}`), and the hashing fast path raises
    `TypeError: unhashable type: 'dict'` on it.
    """
    try:
        return list(OrderedDict.fromkeys(lst))
    except TypeError:
        out: List[Any] = []
        for item in lst:
            if item not in out:
                out.append(item)
        return out


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
    """Merge `overrides` into a copy of `base_dict`, recursively where both values are dicts, and
    return the result. With `add_new_keys=False`, the keys of `overrides` that `base_dict` lacks
    are ignored.
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


def as_list(value: Any) -> List[Any]:
    """Return `value` as a list: a list or tuple gives its items, anything else gives one item."""
    return list(value) if isinstance(value, (list, tuple)) else [value]


_T1 = TypeVar("_T1")
_D1 = TypeVar("_D1")


def try_convert(value: Any, typ: Type[_T1], default: Optional[_D1] = None) -> Union[None, _T1, _D1]:
    # Any failure of the conversion stands for the default of the caller.
    try:
        return typ(value)  # type: ignore
    except Exception:  # noqa: BLE001
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
        if s.lower() == "true":
            return True
        if s.lower() == "false":
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


#: The separator of a hierarchical key is a dot. A dot after a backslash does not split the key,
#: and the backslash stays in the key.
SEP = r"(?<!\\)\."


def get_hierarchy(dct: Dict[str, Any], path: Union[str, List[str]]) -> Optional[Any]:
    if isinstance(path, str):
        path = re.split(SEP, path)
    try:
        return reduce(dict.__getitem__, path, dct)
    except KeyError as e:
        log.error("Error getting hierarchy for path %r: %s", path, e)
        return None


def set_hierarchy(dct: Dict[str, Any], path, value) -> None:
    """Set `value` in `dct` at `path`, a dotted key (`"clock.period"`) or its parts, making a
    table of each key before the last. A dict `value` is set key by key, so a dotted key in it is
    a path too.

    A key is a value or a table, never both. A key that holds a value cannot have keys inside it
    (`timing=true` then `timing.x=1`), and a key that has keys inside it is not given a value
    (`timing.x=1` then `timing=true`). Either order raises a `ConflictingKeys` that names both
    keys. A key left empty (`None`) holds no value, so a table can follow it. Setting a key again
    replaces its value, and a table replaces a table.

    One mapping does not write a table both ways. `{"fpga.part": "P", "fpga": {"speed": -6}}`
    sets keys of `fpga` in two spellings, and the result would depend on the order of the keys. It
    raises a `ConflictingKeys` that names both spellings, in either order. Two dotted keys under
    one table (`fpga.part` and `fpga.speed`) are the normal form. A mapping set in `dct` and each
    mapping inside it are judged one by one (`refuse_both_spellings`). Separate calls are not one
    mapping: a later one replaces what an earlier one set, as the layers of settings do.
    """
    parts = _key_parts(path) if isinstance(path, str) else list(path)
    _set_hierarchy(dct, parts, value, ())


def refuse_both_spellings(mapping: Mapping[Any, Any], above: Tuple[Any, ...] = ()) -> None:
    """Raise a `ConflictingKeys` if `mapping` writes a table both dotted and as a table.

    `fpga.part` and `fpga: {...}` both set keys of `fpga`. So do `a.b.c` and `a.b: {...}`, and
    `a.b.c` and `a: {b: {...}}`. An empty table counts as a table. Only the keys of `mapping` are
    judged; a mapping inside it is judged when it is set (`set_hierarchy`). `above` is the path of
    `mapping` in a larger one, so that the error names the keys in full.
    """
    tables: Dict[Tuple[Any, ...], None] = {}  # the paths written as a table
    dotted: Dict[Tuple[Any, ...], str] = {}  # a path that a dotted key goes through -> that key
    for key, value in mapping.items():
        parts = tuple(_key_parts(key) if isinstance(key, str) else [key])
        written = _dotted((*above, *parts))
        if isinstance(value, dict):
            if parts in dotted:
                raise ConflictingKeys(_dotted((*above, *parts)), dotted[parts], both_ways=True)
            tables[parts] = None
        for end in range(1, len(parts)):
            through = parts[:end]
            if through in tables:
                raise ConflictingKeys(_dotted((*above, *through)), written, both_ways=True)
            dotted.setdefault(through, written)


def _key_parts(key: str) -> List[Any]:
    """The parts of the dotted key `key`. An `AtomicName` is one part."""
    return [key] if isinstance(key, AtomicName) else re.split(SEP, key)


def _set_hierarchy(dct: Dict[str, Any], parts: List[Any], value: Any, above: Tuple[Any, ...]):
    """Run `set_hierarchy` for the table `dct`, which lies under the keys `above` (an error names
    them in full)."""
    key = parts[0]
    here = (*above, key)
    if len(parts) > 1:
        if dct.get(key) is None:
            dct[key] = {}
        elif not isinstance(dct[key], MutableMapping):
            raise ConflictingKeys(here, (*above, *parts))
        _set_hierarchy(dct[key], parts[1:], value, here)
        return
    if isinstance(value, dict):
        held = dct.get(key)
        if value and held is not None and not isinstance(held, MutableMapping):
            # A whole table after a value is the same mistake as a dotted key inside it.
            raise ConflictingKeys(here, (*here, next(iter(value))))
        refuse_both_spellings(value, here)
        table: Dict[str, Any] = {}
        for k, v in value.items():
            _set_hierarchy(table, _key_parts(k) if isinstance(k, str) else [k], v, here)
        value = table
    else:
        held = dct.get(key)
        if isinstance(held, MutableMapping) and held:
            if value is None:  # nothing to set: the keys inside it stay
                return
            raise ConflictingKeys(here, (*here, next(iter(held))))
    dct[key] = value


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
    """Return the command-line options for `variable`: one for each truthy item.

    The options are named after `name` or, by default, after the argument that the caller passed.
    A boolean item gives a bare flag and another item gives `--name=value`, or `--name` and
    `value` when `assign` is false. None and empty text give an empty list.
    """
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
    """Return `s` without `suffix`, like `str.removesuffix` of Python 3.9 and later."""
    if sys.version_info >= (3, 9):
        return s.removesuffix(suffix)
    else:
        return s[: -len(suffix)] if suffix and s.endswith(suffix) else s


def removeprefix(s: str, prefix: str) -> str:
    """Return `s` without `prefix`, like `str.removeprefix` of Python 3.9 and later."""
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


DictStrHier = Dict[str, Any]
StrOrDictStrHier = Union[str, DictStrHier]


def conv(v):
    return v


def expand_hierarchy(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if d is None:
        return {}
    refuse_both_spellings(d)
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
                refuse_both_spellings(override)
                for key, val in override.items():  # type: ignore
                    set_hierarchy(res, key, conv(val))
        return res
    if isinstance(settings, Mapping):
        settings = dict(settings)
        if not hierarchical_keys:
            return settings
        return expand_hierarchy(settings)
    raise TypeError(f"Unsupported type: {type(settings)}")


def flows_table_problems(table: Any, *, layers: bool = False) -> List[Tuple[str, str]]:
    """Return what is wrong with the shape of a `flows` table, as `(key path, problem)` pairs.

    A `flows` table maps flow names, as text, to mappings of the settings of that flow. The table
    and each of its sections are mappings, and an absent one (`None`) is empty. Nothing else is
    read as one: text, numbers and lists are refused, empty ones included. A mistake such as
    `flows: []` or `-s flows.verilator=3` is therefore reported where it was made, by every
    origin that writes a table (a design, a target, a project, the command line, the API). It is
    neither ignored nor does it end in a traceback deeper in a merge. All origins use this one
    rule.

    With `layers`, a section may also be a list or tuple of `KEY=VALUE` text (empty ones too).
    This is how code gives a layer of settings (`settings_to_dict`, and `-s` on the command line).
    The merge of the tables of all origins (`settings_layers.merge_flow_sections`) accepts it,
    because code reaches the merge that way. A file never does.
    """
    if table is None:
        return []
    if not isinstance(table, Mapping):
        return [("flows", f"must be a mapping of flow names to their settings, not {table!r:.60}")]

    def acceptable(section: Any) -> bool:
        if section is None or isinstance(section, Mapping):
            return True
        return (
            layers
            and isinstance(section, (list, tuple))
            and all(isinstance(item, str) and "=" in item for item in section)
        )

    problems = []
    for name, section in table.items():
        if not isinstance(name, str):  # a file or `-s` always gives text; code may not
            problems.append(
                (
                    "flows",
                    f"has the key {name!r:.60}, which is no flow name: a key is a flow's name",
                )
            )
        elif not acceptable(section):
            problems.append(
                (f"flows.{name}", f"must be a mapping of settings, not {section!r:.60}")
            )
    return problems


_xeda_varprog = None


def expand_vars(path: str, environ: Dict[str, str]) -> str:
    """Expand the shell variables `$var` and `${var}` in `path` with the values in `environ`.

    Unknown variables are left unchanged. This is adapted from `os.path.expandvars`, which reads
    `os.environ` and takes no explicit mapping.
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


#: Environment variables that never name a design path, so expanding them into one is always a
#: mistake. The set is immutable because every path expansion reads it.
_ENV_BLACKLIST = frozenset(
    {
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
    }
)

#: The variables that Xeda itself gives to a path in a setting, each by the entry of the
#: validation context of the settings that holds its value: the directory Xeda was started from,
#: and the design root under its two names. A path in a setting reads any other variable from
#: the environment (`expand_env_vars`). Every message that says how to give a setting a location
#: derives the forms it names from this table (`LOCATION_FORMS`).
PATH_VARIABLES: Mapping[str, str] = MappingProxyType(
    {"PWD": "runner_cwd", "DESIGN_ROOT": "design_root", "DESIGN_DIR": "design_root"}
)

#: How a setting is given a location, as a message says it: a path under each of
#: `PATH_VARIABLES`, or an absolute path.
LOCATION_FORMS = ", ".join(f"${name}/..." for name in PATH_VARIABLES) + " or an absolute path"


def path_variables(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return the value of each of `PATH_VARIABLES` in `context` (its `runner_cwd` and
    `design_root`), in the form that `expand_env_vars` takes as overrides. A variable that
    `context` lacks gets None, which leaves it as written."""
    return {name: context.get(key) for name, key in PATH_VARIABLES.items()}


def expand_env_vars(
    path: Union[str, Path],
    overrides: Optional[Dict[str, Any]] = None,
    escape: Optional[Callable[[str], str]] = None,
) -> Path:
    """Return `path` with its environment variables substituted by their values.

    A variable whose value in `overrides` is None is not expanded. `escape`, when given, is
    applied to every substituted value: use `glob.escape` for a pattern, where a value is a
    literal place and not more pattern.
    """
    if not isinstance(path, str):
        path = str(path)
    # nothing to expand in a path too short to hold a variable
    if len(path) < 2:
        return Path(path)
    if overrides is None:
        overrides = {}
    # Never substitute a variable of `_ENV_BLACKLIST`, or one that starts with an underscore,
    # into a design path.
    environ = {
        k: v for k, v in os.environ.items() if not k.startswith("_") and k not in _ENV_BLACKLIST
    }
    for k, v in overrides.items():
        if v is None:
            environ.pop(k, None)
        else:
            environ[k] = str(v)
    if escape is not None:
        environ = {k: escape(v) for k, v in environ.items()}
    path = expand_vars(path, environ)
    return Path(path)


class XedaException(Exception):
    """The base class of all Xeda exceptions. The command line catches and reports them."""


def restore_exception(cls: type[Exception], args: tuple[Any, ...], state: dict[str, Any]) -> Any:
    """Rebuild an exception that was pickled, without calling its constructor.

    Use it in the `__reduce__` of an exception whose constructor takes keyword-only arguments:
    `(restore_exception, (type(self), self.args, dict(self.__dict__)))`."""
    exception = Exception.__new__(cls)
    Exception.__init__(exception, *args)
    exception.__dict__.update(state)
    return exception


class ConflictingKeys(XedaException, ValueError):
    """Keys that cannot be set together, in either order.

    A key given as a value and as a table: `value_key` holds a value, and `child_key` sets a key
    inside it. Or, with `both_ways`, a table written dotted and as a table in one mapping:
    `value_key` is the table, and `child_key` is the dotted key that sets a key of it (`fpga.part`
    beside `fpga: {...}`). The class is a `XedaException`, so the command line reports it, and a
    `ValueError`, so a validator that expands a key reports it as the error of the field."""

    def __init__(self, value_key: Any, child_key: Any, both_ways: bool = False) -> None:
        self.value_key = _dotted(value_key)
        self.child_key = _dotted(child_key)
        self.both_ways = both_ways
        if both_ways:
            message = (
                f"`{self.child_key}` and `{self.value_key}: {{...}}` both set keys of "
                f"`{self.value_key}`: write one of them"
            )
        else:
            message = (
                f"`{self.value_key}` is set to a value, and `{self.child_key}` sets a key inside "
                "it: give one of them"
            )
        super().__init__(message)

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.value_key, self.child_key, self.both_ways))


class EscapedName(XedaException, ValueError):
    """A name of a setting in `opaque_key_fields` that contains `dataclass.ESCAPED_DOT`. The
    expansion of dotted keys keeps such a key whole, so one origin could give both `A` and `A.B`
    without a `ConflictingKeys`. The names of the tool have no backslash."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"the name `{name}` has a backslash before a dot; the names of this setting are "
            "written with plain dots (`STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM`)"
        )

    def __reduce__(self):  # an exception crosses a process boundary by being pickled
        return (type(self), (self.name,))


def _dotted(key: Any) -> str:
    """Return a key given as its parts in the dotted form it is written in."""
    return ".".join(map(str, key)) if isinstance(key, (tuple, list)) else str(key)


class ToolException(XedaException):
    """The base class of all tool exceptions."""


class NonZeroExitCode(ToolException):
    """A command ended with a nonzero exit code. `args` holds what the tool reported, if the
    caller knows it. The message shows it after the code."""

    def __init__(self, command_args: Any, exit_code: int, *args: object) -> None:
        if isinstance(command_args, (list, tuple)):
            command_args = " ".join(map(str, command_args))
        self.command_args = command_args
        self.exit_code = exit_code
        super().__init__(*args)

    def __str__(self) -> str:
        message = f"Command '{self.command_args}' exited with code {self.exit_code}!"
        return " ".join([message, *map(str, self.args)])


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
        if isinstance(data, Enum):
            # An enum member's `__dict__` carries `__objclass__`, the enum class, whose own
            # `__dict__` lists every member, so descending into it never terminates. Hash the
            # member as it is written out instead. `SourceType` reaches here through
            # `DesignSource.type` whenever a design is hashed directly.
            return str(data)
        if isinstance(data, type):
            return f"{data.__module__}.{data.__qualname__}"
        if isinstance(data, Quantity):
            # What the float that the quantity replaces hashed as, whatever unit it was
            # written in: a run keeps its identity when a clock period gains a unit.
            return str(data.magnitude)
        if hasattr(data, "__dict__"):
            return _sorted_dict_str(model_state(data))
        return str(data)

    r = repr(_sorted_dict_str(data))
    return hashlib.sha3_256(bytes(r, "UTF-8")).hexdigest()
