"""Selection, validation and owned cache preparation of openXC7 chip databases.

An installation is located through the resolved nextpnr executable, never environment
variables or directory globs. Generation identities describe content and a fixed recipe,
independent of installation paths. Header validation is a structural guard, not a content
integrity record: callers must trace explicit binaries and digest published cache entries.
"""

from __future__ import annotations

import re
import struct
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from ..digest import DIRECTORY_DIGEST, record_file
from ..flow import FPGA, FlowFatalError
from ..flow_runner.run_lock import lock_file, run_dir_lock
from ..listing import directory_files
from ..proc_utils import ProcessTimeout, run_process
from ..run_dir import RunDirectory, RunDirectoryError
from ..run_root import is_run_root
from ..utils import NonZeroExitCode, replacing_file, semantic_hash

log = logging.getLogger(__name__)

# Generating unmeasured larger dies may take much longer than the measured a100t.
CHIPDB_GENERATION_TIMEOUT = 1800.0

CHIPDB_MAGIC = 0x00CA7CA7
CHIPDB_VERSION = 6
CHIPDB_ENDIANNESS = "little"
BBASM_ARGS = ("-l",)
_HEADER_SIZE = 84
_FAMILIES = {
    "artix-7": "artix7",
    "kintex-7": "kintex7",
    "spartan-7": "spartan7",
    "zynq-7": "zynq7",
    "virtex-7": "virtex7",
}
_DEVICE_NAME = re.compile(r"xc7[a-z0-9]+")


@dataclass(frozen=True)
class XilinxLayout:
    """Resolved installed data paths; explicit chipdbs need no generation tools."""

    nextpnr: Path
    himbaechel: Path
    database: Path
    generator: Path | None
    bbasm: Path | None
    chipdb: Path | None


@dataclass(frozen=True)
class XilinxSelection:
    """A full ordering part, its mapped device and the fabric actually routed.

    ``name`` is the part as the database spells it, which nextpnr's ``--device`` must be
    given: it matches case-sensitively (``xc7a100tcsg324-2L``, never ``-2l``).
    """

    part: str
    family: str
    device: str
    fabric: str
    database: Path
    name: str = ""


@dataclass(frozen=True)
class XilinxChipdbIdentity:
    """Content inputs and fixed generation recipe for an immutable cache entry."""

    key: str
    fabric: str
    family: str
    version: int
    endianness: str
    assembler_args: tuple[str, ...]
    nextpnr_digest: str
    bbasm_digest: str
    himbaechel_files: tuple[tuple[str, str], ...]
    database_files: tuple[tuple[str, str], ...]
    timing_files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class XilinxChipdbHeader:
    """The bounded version-6 structural header, including its relative location."""

    offset: int
    version: int
    width: int
    height: int
    uarch: str
    fabric: str
    generator: str


def find_xilinx_layout(
    nextpnr: Path, *, prjxray_db: Path | None = None, chipdb: Path | None = None
) -> XilinxLayout:
    """Locate the supported ``bin/`` and ``share/nextpnr/`` openXC7 layout.

    ``nextpnr`` is the executable already selected by the caller. ``prjxray_db``
    replaces the database root; ``chipdb`` names one file, never a search directory.
    This does not probe tools, extract resources or create files.
    """
    try:
        binary = nextpnr.resolve()
        if not binary.is_file() or binary.parent.name != "bin":
            raise FlowFatalError(
                f"Unsupported openXC7 nextpnr layout: {nextpnr}; searched {binary}. "
                "Install nextpnr-himbaechel under <prefix>/bin with share/nextpnr data."
            )
        prefix = binary.parent.parent
        share = prefix / "share/nextpnr/himbaechel"
        database = (
            prjxray_db.resolve() if prjxray_db is not None else prefix / "share/nextpnr/prjxray-db"
        )
        explicit = chipdb.resolve() if chipdb is not None else None
        if explicit is not None and not explicit.is_file():
            raise FlowFatalError(f"chipdb must name one existing file; searched {chipdb}")
        if not database.is_dir():
            raise FlowFatalError(
                f"Project X-Ray database root is missing; searched {database}. "
                "Install openXC7 data or set prjxray_db to its database root."
            )
        generator = None
        bbasm = None
        if explicit is None:
            if not share.is_dir():
                raise FlowFatalError(
                    f"Himbaechel share tree is missing; searched {share}. Install openXC7 1.0 data."
                )
            generator = share / "uarch/xilinx/gen/xilinx_gen.py"
            bbasm = prefix / "bin/bbasm"
            for name, path in (("Xilinx generator", generator), ("bbasm", bbasm)):
                if not path.is_file():
                    raise FlowFatalError(
                        f"{name} is missing; searched {path}. "
                        "Install openXC7 1.0 or supply an existing chipdb file."
                    )
        return XilinxLayout(binary, share, database, generator, bbasm, explicit)
    except (OSError, RuntimeError) as error:
        raise FlowFatalError(f"Cannot read openXC7 layout for {nextpnr}: {error}") from error


def _mapping(path: Path, part: str) -> dict[str, tuple[str, object]]:
    """Load mapping YAML while preserving a useful part/path error boundary.

    Keys are matched in lower case; each entry keeps the name as the file spells it.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a YAML mapping")
        normalized: dict[str, tuple[str, object]] = {}
        for name, entry in data.items():
            if not isinstance(name, str) or name.lower() in normalized:
                raise ValueError("expected unique text mapping keys")
            normalized[name.lower()] = (name, entry)
        return normalized
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        raise FlowFatalError(
            f"Cannot read Xilinx mapping for part {part} at {path}: {error}"
        ) from error


def _mapped_name(entry: object, field: str, path: Path, part: str) -> str:
    name = entry.get(field) if isinstance(entry, dict) else None
    if not isinstance(name, str) or not _DEVICE_NAME.fullmatch(name.lower()):
        raise FlowFatalError(f"Missing or invalid {field} mapping for part {part} at {path}")
    return name.lower()


def select_xilinx(part: str, layout: XilinxLayout) -> XilinxSelection:
    """Select part -> device -> fabric using the installed Project X-Ray mappings."""
    normalized = part.lower()
    family = _FAMILIES.get(FPGA(normalized).family or "")
    if family is None:
        raise FlowFatalError(f"Unsupported Xilinx 7-series part {part} in {layout.database}")
    root = layout.database / family
    parts_path = root / "mapping/parts.yaml"
    parts = _mapping(parts_path, normalized)
    if normalized not in parts:
        raise FlowFatalError(
            f"Unknown full Xilinx part {part}; searched {parts_path}. "
            "Use a full part including package and speed supported by this Project X-Ray database."
        )
    name, entry = parts[normalized]
    device = _mapped_name(entry, "device", parts_path, normalized)
    devices_path = root / "mapping/devices.yaml"
    devices = _mapping(devices_path, normalized)
    fabric = _mapped_name(devices.get(device, ("", None))[1], "fabric", devices_path, normalized)
    fabric_path = root / fabric
    if not fabric_path.is_dir():
        raise FlowFatalError(
            f"Fabric {fabric} for Xilinx part {part} is absent; searched {fabric_path}. "
            "Install a Project X-Ray database containing this fabric."
        )
    return XilinxSelection(normalized, family, device, fabric, layout.database, name)


def _tree_contents(
    root: Path, ancestors: frozenset[Path] = frozenset()
) -> tuple[tuple[str, str], ...]:
    """Sorted relative names/content using the shared walker and digest helpers.

    Directory links are read by content too. Expand them separately because the
    walker's visit-once policy otherwise omits the content of a second directory alias.
    A cyclic or unreadable input cannot identify a generated database.
    """
    resolved = root.resolve()
    if resolved in ancestors:
        raise ValueError(f"cyclic input directory at {root}")
    if not resolved.is_dir():
        raise ValueError(f"missing input directory {root}")
    record_file(resolved)  # also rejects an unreadable root
    entries: list[tuple[str, str]] = []
    for path in directory_files(resolved):
        name = path.relative_to(resolved).as_posix()
        try:
            record = record_file(path)
        except OSError as error:
            raise ValueError(f"cannot read input {path}: {error}") from error
        if record.sha != DIRECTORY_DIGEST and not path.is_file():
            raise ValueError(f"input is not a readable regular file: {path}")
        entries.append((name, record.sha))
        if path.is_symlink() and path.is_dir():
            nested = _tree_contents(path, ancestors | {resolved})
            entries.extend((f"{name}/{child}", digest) for child, digest in nested)
    return tuple(sorted(entries))


def chipdb_identity(layout: XilinxLayout, selection: XilinxSelection) -> XilinxChipdbIdentity:
    """Hash generation inputs afresh, including installed binaries and whole data trees.

    The generator passes ``--xray <family> --device <fabric> --bba <temporary>``;
    bbasm always uses ``-l``. No interpreter version, install path, version probe or
    persistent metadata-only hash cache participates. Kintex/Virtex also read the
    Artix timing directory in openXC7 1.0, so include those fallback inputs.
    """
    if layout.chipdb is not None or layout.generator is None or layout.bbasm is None:
        raise FlowFatalError(
            "An explicit chipdb has no generation identity; trace its content instead"
        )
    if selection.database != layout.database:
        raise FlowFatalError(
            f"Xilinx part {selection.part} selection has a different database root"
        )
    try:
        nextpnr_digest = record_file(layout.nextpnr).sha
        bbasm_digest = record_file(layout.bbasm).sha
        himbaechel_files = _tree_contents(layout.himbaechel)
        database_files = _tree_contents(selection.database / selection.family)
        timing_files = (
            _tree_contents(selection.database / "artix7/timings")
            if selection.family in ("kintex7", "virtex7")
            else ()
        )
        recipe = {
            "format": "himbaechel",
            "magic": CHIPDB_MAGIC,
            "version": CHIPDB_VERSION,
            "endianness": CHIPDB_ENDIANNESS,
            "generator": "uarch/xilinx/gen/xilinx_gen.py",
            "generator_args": (
                "--xray",
                "<family>",
                "--device",
                "<fabric>",
                "--bba",
                "<temporary>",
            ),
            "assembler_args": BBASM_ARGS,
            "family": selection.family,
            "fabric": selection.fabric,
            "nextpnr": nextpnr_digest,
            "bbasm": bbasm_digest,
            "himbaechel": himbaechel_files,
            "database": database_files,
            "artix_timing_fallback": timing_files,
        }
        return XilinxChipdbIdentity(
            semantic_hash(recipe),
            selection.fabric,
            selection.family,
            CHIPDB_VERSION,
            CHIPDB_ENDIANNESS,
            BBASM_ARGS,
            nextpnr_digest,
            bbasm_digest,
            himbaechel_files,
            database_files,
            timing_files,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise FlowFatalError(
            f"Cannot identify Xilinx chipdb for part {selection.part} "
            f"from {layout.himbaechel} and {selection.database}: {error}"
        ) from error


def validate_chipdb(path: Path, selection: XilinxSelection) -> XilinxChipdbHeader:
    """Validate version-6 header and exact EOF strings with small bounded reads.

    The first signed little-endian offset points to the header. Each string offset
    is relative to its own pointer field, not the file or header. The selected
    fabric, rather than the part's marketing device, determines the expected die.
    Large routing tables are not read here; this is not whole-file authentication.
    """
    try:
        if not path.is_file():
            raise ValueError("expected an existing regular chipdb file")
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()

            def read_at(offset: int, count: int) -> bytes:
                if offset < 0 or offset + count > size:
                    raise ValueError(f"offset {offset} with length {count} is outside {size} bytes")
                stream.seek(offset)
                data = stream.read(count)
                if len(data) != count:
                    raise ValueError("truncated chipdb")
                return data

            offset = struct.unpack("<i", read_at(0, 4))[0]
            if offset < 4:
                raise ValueError(f"invalid header offset {offset}")
            header = read_at(offset, _HEADER_SIZE)
            magic, version, width, height = struct.unpack_from("<4I", header)
            if magic != CHIPDB_MAGIC:
                raise ValueError(f"invalid magic {magic:#x}; expected {CHIPDB_MAGIC:#x}")
            if version != CHIPDB_VERSION:
                raise ValueError(f"unsupported version {version}; expected {CHIPDB_VERSION}")
            if width == 0 or height == 0:
                raise ValueError(f"invalid dimensions {width} x {height}")
            expected = ("xilinx", selection.fabric, "python_dbgen")
            for i, (name, text) in enumerate(zip(("uarch", "fabric", "generator"), expected)):
                field = 16 + 4 * i
                relative = struct.unpack_from("<i", header, field)[0]
                target = offset + field + relative
                value = text.encode("ascii") + b"\0"
                if read_at(target, len(value)) != value:
                    raise ValueError(f"invalid or unterminated {name} string; expected {text!r}")
            tail = b"\0".join(text.encode("ascii") for text in expected) + b"\0"
            if read_at(size - len(tail), len(tail)) != tail:
                raise ValueError("invalid EOF marker: truncated tail or appended data")
            return XilinxChipdbHeader(offset, version, width, height, *expected)
    except (OSError, ValueError, struct.error) as error:
        raise FlowFatalError(
            f"Invalid Xilinx chipdb {path} for part {selection.part} "
            f"(fabric {selection.fabric}): {error}"
        ) from error


def _cache_path(owner: RunDirectory, path: Path) -> Path:
    """Guard the lexical namespace before locks, reads, creates or publication.

    Cache entries and durable locks never use links, including links within the root:
    replacing a lock's name would split process coordination between different inodes.
    """
    located = owner.inside(path)
    current = owner.path
    for part in path.relative_to(owner.path).parts:
        current = current / part
        if current.is_symlink():
            raise RunDirectoryError(
                f"Linked Xilinx cache path {current}; remove the link and rerun."
            )
    return located


def _cached_chipdb(
    owner: RunDirectory,
    entry: Path,
    selection: XilinxSelection,
    identity: XilinxChipdbIdentity,
) -> Path:
    """Read an immutable entry; corruption is a repairable error, never a cache miss."""
    try:
        chipdb = _cache_path(owner, entry / f"{selection.fabric}.bin")
        manifest = _cache_path(owner, entry / "manifest.yaml")
        validate_chipdb(chipdb, selection)
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("identity") != identity.key
            or data.get("fabric") != selection.fabric
            or data.get("sha") != record_file(chipdb).sha
            or semantic_hash(data.get("inputs")) != semantic_hash(asdict(identity))
        ):
            raise ValueError("manifest identity/fabric or binary digest does not match")
        return chipdb
    except (OSError, ValueError, FlowFatalError, yaml.YAMLError) as error:
        raise FlowFatalError(
            f"Corrupt Xilinx chipdb cache entry {entry}: {error}; remove this entry and rerun."
        ) from error


def _generate_chipdb(
    layout: XilinxLayout, selection: XilinxSelection, temporary: RunDirectory, entry: Path
) -> Path:
    """Run bounded native generator/assembler processes without flow-directory writes."""
    assert layout.generator is not None and layout.bbasm is not None
    if selection.fabric == "xc7a100t":
        log.info(
            "generating the xc7a100t chip database: about a minute and up to about 4.8 GB "
            "of memory, once per run root"
        )
    else:
        log.info(
            "generating the %s chip database in %s; time and memory for this fabric are "
            "unmeasured (xc7a100t takes about a minute and up to about 4.8 GB)",
            selection.fabric,
            entry,
        )
    bba = temporary.writable(f"{selection.fabric}.bba")
    binary = temporary.writable(f"{selection.fabric}.bin")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    commands = (
        (
            "generator",
            sys.executable,
            [
                layout.generator,
                "--xray",
                selection.database / selection.family,
                "--device",
                selection.fabric,
                "--bba",
                bba,
            ],
        ),
        ("assembler", str(layout.bbasm), [*BBASM_ARGS, bba, binary]),
    )
    for stage, executable, args in commands:
        try:
            run_process(
                executable,
                args,
                cwd=temporary.path,
                env=env,
                timeout=CHIPDB_GENERATION_TIMEOUT,
            )
        except (OSError, NonZeroExitCode) as error:
            killed = (
                isinstance(error, NonZeroExitCode)
                and not isinstance(error, ProcessTimeout)
                and error.exit_code < 0
            )
            hint = (
                " A signal kill can indicate memory pressure; this does not establish an OOM kill."
                if killed
                else ""
            )
            raise FlowFatalError(
                f"Xilinx chipdb {selection.fabric} {stage} failed for cache {entry}: {error}.{hint}"
            ) from error
    _cache_path(temporary, binary)
    validate_chipdb(binary, selection)
    temporary.remove(bba)
    return binary


def prepare_chipdb(
    layout: XilinxLayout, selection: XilinxSelection, run_directory: RunDirectory
) -> Path:
    """Validate an explicit chipdb or prepare a shared content-keyed immutable entry.

    Generation requires a claimed, marked root, never one reconstructed from a flow
    directory. A sibling durable lock coordinates POSIX processes using the existing
    lock machinery; Windows retains its existing absence of process locking. Scrubbing
    ordinary flow directories leaves this cache and its locks alone. A cache hit starts
    no program: the manifest is its generator provenance.
    """
    if layout.chipdb is not None:
        validate_chipdb(layout.chipdb, selection)
        return layout.chipdb
    root = run_directory.run_root
    if root is None or not is_run_root(root):
        raise FlowFatalError(
            "Automatic Xilinx chipdb generation needs a claimed RunDirectory under a marked "
            "run root; supply a validated explicit chipdb when constructing a flow directly."
        )
    owner = RunDirectory(root, root)
    identity = chipdb_identity(layout, selection)
    cache = _cache_path(owner, root / ".cache/xilinx-chipdb")
    entry = _cache_path(owner, cache / identity.key)
    _cache_path(owner, lock_file(entry))
    cache.mkdir(parents=True, exist_ok=True)
    with run_dir_lock(entry):
        # Recheck authority after waiting, before reading or writing the entry.
        _cache_path(owner, entry)
        _cache_path(owner, lock_file(entry))
        if entry.exists():
            return _cached_chipdb(owner, entry, selection, identity)
        # Hash the installation again only when about to generate: a hit hashed it once.
        if chipdb_identity(layout, selection) != identity:
            raise FlowFatalError(f"Xilinx chipdb inputs changed while waiting for {entry}; rerun.")
        # Holding the entry's exclusive lock, no process is generating this key, so any
        # scratch directory of it is the remains of a killed generation (multi-GB).
        for interrupted in sorted(cache.glob(f"{identity.key}.tmp-*")):
            owner.remove(_cache_path(owner, interrupted))
        temporary = Path(tempfile.mkdtemp(prefix=f"{identity.key}.tmp-", dir=cache))
        scratch = RunDirectory.claimed(_cache_path(owner, temporary), root)
        try:
            binary = _generate_chipdb(layout, selection, scratch, entry)
            manifest = {
                "identity": identity.key,
                "fabric": selection.fabric,
                "sha": record_file(binary).sha,
                "inputs": asdict(identity),
                "generator": str(layout.generator),
                "assembler": str(layout.bbasm),
            }
            with replacing_file(scratch.writable("manifest.yaml")) as stream:
                yaml.safe_dump(manifest, stream, sort_keys=True)
            if chipdb_identity(layout, selection) != identity:
                raise FlowFatalError(
                    f"Xilinx chipdb inputs changed during generation for {entry}; rerun."
                )
            _cache_path(owner, entry)
            _cache_path(owner, temporary)
            temporary.rename(entry)
            return entry / binary.name
        finally:
            owner.remove(temporary)
