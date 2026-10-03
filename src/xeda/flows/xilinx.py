"""Read-only selection and validation of openXC7 1.0 Himbaechel chip databases.

An installation is located through the resolved nextpnr executable, never environment
variables or directory globs. Generation identities describe content and a fixed recipe,
independent of installation paths. Header validation is a structural guard, not a content
integrity record: callers must trace explicit binaries and digest published cache entries.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..digest import DIRECTORY_DIGEST, record_file
from ..flow import FPGA, FlowFatalError
from ..listing import directory_files
from ..utils import semantic_hash

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
    """A full ordering part, its mapped device and the fabric actually routed."""

    part: str
    family: str
    device: str
    fabric: str
    database: Path


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


def _mapping(path: Path, part: str) -> dict[str, object]:
    """Load mapping YAML while preserving a useful part/path error boundary."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected a YAML mapping")
        normalized: dict[str, object] = {}
        for name, entry in data.items():
            if not isinstance(name, str) or name.lower() in normalized:
                raise ValueError("expected unique text mapping keys")
            normalized[name.lower()] = entry
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
    device = _mapped_name(parts[normalized], "device", parts_path, normalized)
    devices_path = root / "mapping/devices.yaml"
    devices = _mapping(devices_path, normalized)
    fabric = _mapped_name(devices.get(device), "fabric", devices_path, normalized)
    fabric_path = root / fabric
    if not fabric_path.is_dir():
        raise FlowFatalError(
            f"Fabric {fabric} for Xilinx part {part} is absent; searched {fabric_path}. "
            "Install a Project X-Ray database containing this fabric."
        )
    return XilinxSelection(normalized, family, device, fabric, layout.database)


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
