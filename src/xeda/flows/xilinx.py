"""Selection, validation and cache preparation of openXC7 chip databases.

The functions find an installation through the resolved `nextpnr` executable. They read no
environment variable and search no directory by glob. A generation identity describes the
content and a fixed recipe, and does not depend on installation paths. The header check
validates structure only. It is no integrity record of the content, so callers must trace
explicit binaries and digest published cache entries.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
import struct
import logging
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
from ..run_dir import RunDirectory
from ..run_root import is_run_root
from ..utils import NonZeroExitCode, replacing_file, semantic_hash

log = logging.getLogger(__name__)

# Larger dies were not measured and may take much longer than the xc7a100t.
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
    """The resolved paths of the installed data. An explicit chipdb needs no generation tools."""

    nextpnr: Path
    himbaechel: Path
    database: Path
    generator: Path | None
    bbasm: Path | None
    chipdb: Path | None


@dataclass(frozen=True)
class XilinxSelection:
    """A full part, its mapped device and the fabric that is routed.

    `name` is the part as the database spells it. Give this name to the `--device` option of
    `nextpnr`, which matches case-sensitively (`xc7a100tcsg324-2L`, not `-2l`).
    """

    part: str
    family: str
    device: str
    fabric: str
    database: Path
    name: str = ""


@dataclass(frozen=True)
class XilinxChipdbIdentity:
    """The content inputs and the fixed generation recipe of an immutable cache entry."""

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
    """The version-6 header of a chip database, read from a bounded part of the file."""

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
    """Locate the supported openXC7 layout of `bin/` and `share/nextpnr/`.

    `nextpnr` is the executable that the caller already selected. `prjxray_db` replaces the
    database root. `chipdb` names one file, not a directory to search. The function runs no
    tool, extracts no resource and creates no file.
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
    """Load a mapping YAML file, and name the part and the path in an error.

    Keys match in lower case. Each entry keeps the name as the file spells it.
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


def find_prjxray_database(executable: Path, prjxray_db: Path | None = None) -> Path:
    """Return the Project X-Ray database root that a tool of an openXC7 installation reads.

    The root is `prjxray_db` when given. Otherwise it is `share/nextpnr/prjxray-db` under the
    prefix of the resolved `executable` (`<prefix>/bin/fpga-as`). The function only reads, and
    consults no environment variable.
    """
    try:
        if prjxray_db is not None:
            database = prjxray_db.resolve()
        else:
            binary = executable.resolve()
            if not binary.is_file() or binary.parent.name != "bin":
                raise FlowFatalError(
                    f"Unsupported openXC7 layout: {executable}; searched {binary}. Install it "
                    "under <prefix>/bin with share/nextpnr data, or set prjxray_db."
                )
            database = binary.parent.parent / "share/nextpnr/prjxray-db"
        if not database.is_dir():
            raise FlowFatalError(
                f"Project X-Ray database root is missing; searched {database}. "
                "Install openXC7 data or set prjxray_db to its database root."
            )
        return database
    except (OSError, RuntimeError) as error:
        raise FlowFatalError(f"Cannot read openXC7 layout for {executable}: {error}") from error


def select_xilinx(part: str, layout: XilinxLayout) -> XilinxSelection:
    """Select the device and the fabric of a part with the installed Project X-Ray mappings."""
    return select_xilinx_part(part, layout.database)


def select_xilinx_part(part: str, database: Path) -> XilinxSelection:
    """Select the device and the fabric of a part with the mappings of the root `database`."""
    normalized = part.lower()
    family = _FAMILIES.get(FPGA(normalized).family or "")
    if family is None:
        raise FlowFatalError(f"Unsupported Xilinx 7-series part {part} in {database}")
    root = database / family
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
    return XilinxSelection(normalized, family, device, fabric, database, name)


#: The files of a part's directory that describe its die and its pinout. The directories of the
#: speed grades of one device and package are expected to agree in them.
PINOUT_FILES = ("part.json", "package_pins.csv")


@dataclass(frozen=True)
class PartData:
    """The Project X-Ray part data that `fpga-as` packs a part with.

    `name` is the part whose directory is `directory`. It is the part asked for, or another
    speed grade of the same device and package.
    """

    requested: str
    name: str
    directory: Path

    @property
    def exact(self) -> bool:
        return self.name.lower() == self.requested.lower()


def _speed_grade_order(name: str) -> tuple[int, int, str]:
    """Return the sort key of a speed grade: its number, then a plain grade before `L`."""
    match = re.fullmatch(r"(\d+)(L?)", (FPGA(name).speed or "").lstrip("-").upper())
    return (int(match[1]), len(match[2]), name) if match else (sys.maxsize, 0, name)


def _package_of(name: str) -> str | None:
    """Return the package of a part with its pin count (`ffg676`), or `None` for a non-part."""
    try:
        fpga = FPGA(name)
    except ValueError:
        return None
    return f"{fpga.package}{fpga.pins}" if fpga.package and fpga.pins else None


def _no_part_data(selection: XilinxSelection, root: Path, with_data: list[str]) -> FlowFatalError:
    """Build the error for a part whose package has no part data at any speed grade.

    The error names the part data that the database has for the same device in other packages."""
    package = _package_of(selection.part) or selection.part
    grades: dict[str, list[str]] = {}
    for name in sorted(with_data, key=_speed_grade_order):
        grades.setdefault(_package_of(name) or "", []).append(FPGA(name).speed or "")
    if grades:
        elsewhere = "; ".join(
            f"{p}: speed grades {', '.join(g)}" for p, g in sorted(grades.items())
        )
        available = f"It has part data for other packages of {selection.device} ({elsewhere})."
    else:
        available = f"It has no part data for any package of {selection.device}."
    return FlowFatalError(
        f"Project X-Ray has no part data for {selection.part}: {root} has no directory with a "
        f"part.json for device {selection.device}, package {package}, at any speed grade. "
        f"{available} Pack a part that has data, or set prjxray_db to a database that has this one."
    )


def locate_part_data(selection: XilinxSelection) -> PartData:
    """Return the part data directory of `selection`: the exact part's, if the database has it.

    A device and package are one die with one pinout. The speed grade directories of one device
    and package are therefore expected to agree in `part.json` and `package_pins.csv` (`nextpnr`
    keeps the exact grade for timing), and a database often has the directory of one grade only.
    When the exact part has none, the directory of the lowest speed grade of the same device and
    package stands in: the smallest grade number, with a plain grade before its `L` variant. The
    caller logs the choice. If the directories of the other grades differ from it in those two
    files, this function logs a warning that names them. It never uses another device or package.
    If no directory of that device and package holds a `part.json`, it raises a `FlowFatalError`
    that names the part, the directory searched and the data that the database has for other
    packages of the device.
    """
    root = selection.database / selection.family
    exact = root / selection.name
    if (exact / "part.json").is_file():
        return PartData(selection.part, selection.name, exact)
    parts_path = root / "mapping/parts.yaml"
    with_data = []
    for name, entry in _mapping(parts_path, selection.part).values():
        try:
            same_device = _mapped_name(entry, "device", parts_path, name) == selection.device
        except FlowFatalError:
            continue
        if same_device and _package_of(name) and (root / name / "part.json").is_file():
            with_data.append(name)
    package = _package_of(selection.part)
    candidates = [name for name in with_data if _package_of(name) == package]
    if not candidates:
        raise _no_part_data(selection, root, with_data)
    name = min(candidates, key=_speed_grade_order)
    differing = {}
    for other in sorted(candidates, key=_speed_grade_order):
        files = [
            file
            for file in PINOUT_FILES
            if other != name and _content(root / other / file) != _content(root / name / file)
        ]
        if files:
            differing[other] = files
    if differing:
        log.warning(
            "The Project X-Ray data of the speed grades of %s%s do not agree. %s has none, so it "
            "is packed with the data of %s, which differs from that of %s. Check the database.",
            selection.device,
            package,
            selection.part,
            name,
            "; ".join(f"{other} in {', '.join(files)}" for other, files in differing.items()),
        )
    return PartData(selection.part, name, root / name)


def _content(path: Path) -> bytes | None:
    """Return the bytes of a file, or `None` if there is none."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _tree_contents(
    root: Path, ancestors: frozenset[Path] = frozenset()
) -> tuple[tuple[str, str], ...]:
    """Return the sorted relative names and content digests of a directory tree.

    The function uses the shared walker and digest helpers. Directory links count by content
    too. It expands them separately, because the walker visits each directory once and would
    omit the content of a second alias of a directory. A cyclic or unreadable input cannot
    identify a generated database.
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
    """Hash the generation inputs from scratch, including installed binaries and whole data trees.

    The generator receives `--xray <family> --device <fabric> --bba <temporary>`. `bbasm`
    always receives `-l`. The hash includes no interpreter version, install path or version
    probe, and no persistent hash cache that checks metadata alone is used. Kintex and Virtex
    also read the Artix timing directory in openXC7 1.0, so the hash includes those fallback
    inputs.
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
    """Validate the version-6 header and the exact strings at the end of a chip database file.

    The function reads only small, bounded parts of the file. The first offset (signed,
    little-endian) points to the header. Each string offset is relative to its own pointer
    field. The selected fabric determines the expected die, not the part's device. The
    function does not read the large routing tables and does not authenticate the whole file.
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
    """Check that no link is on the path of a cache entry or lock, and return the path.

    The check runs before any lock, read, creation or publication. Cache entries and durable
    locks never use links, not even links inside the run root: replacing the name of a lock
    would split the coordination of processes between different inodes. `RunDirectory.unlinked`
    is this rule, and every cache under a run root shares it.
    """
    return owner.unlinked(path)


def _cached_chipdb(
    owner: RunDirectory,
    entry: Path,
    selection: XilinxSelection,
    identity: XilinxChipdbIdentity,
) -> Path:
    """Read an immutable cache entry. A corrupt entry is an error to repair, never a cache miss."""
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
            f"Corrupt Xilinx chipdb cache entry {entry}: {error}; remove this entry and run again."
        ) from error


def _generate_chipdb(
    layout: XilinxLayout,
    selection: XilinxSelection,
    temporary: RunDirectory,
    entry: Path,
    environment: Mapping[str, str],
) -> Path:
    """Run the generator and the assembler with a time limit. Write in no run directory.

    The programs get `environment`, with Python bytecode switched off."""
    assert layout.generator is not None and layout.bbasm is not None
    if selection.fabric == "xc7a100t":
        log.info(
            "generating the xc7a100t chip database: about a minute and about 3.5 GB "
            "of memory, once per run root"
        )
    else:
        log.info(
            "generating the %s chip database in %s; time and memory for this fabric are "
            "unmeasured (xc7a100t takes about a minute and about 3.5 GB)",
            selection.fabric,
            entry,
        )
    bba = temporary.writable(f"{selection.fabric}.bba")
    binary = temporary.writable(f"{selection.fabric}.bin")
    env = {**environment, "PYTHONDONTWRITEBYTECODE": "1"}
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


def locate_chipdb(
    layout: XilinxLayout,
    selection: XilinxSelection,
    run_directory: RunDirectory,
    identity: XilinxChipdbIdentity | None = None,
) -> Path | None:
    """Return where `prepare_chipdb` leaves the chip database of `selection`, if it is there.

    The result is an explicit chipdb, validated, or else the cache entry for the identity of the
    installation and the device data, validated as `prepare_chipdb` validates a hit. The result
    is `None` if nothing is there or the run root is not a marked one, because a launch would
    generate the database. The function reads files to form the identity and to check the entry.
    It writes nothing and starts no program. `identity` is the identity of this installation and
    device data, if the caller has just formed it (hashing them is most of the cost). Otherwise
    this function forms it."""
    if layout.chipdb is not None:
        validate_chipdb(layout.chipdb, selection)
        return layout.chipdb
    root = run_directory.run_root
    if root is None or not is_run_root(root):
        return None
    owner = RunDirectory(root, root)
    identity = identity or chipdb_identity(layout, selection)
    entry = _cache_path(owner, _cache_path(owner, root / ".cache/xilinx-chipdb") / identity.key)
    # check an entry as for a hit: a corrupt one raises `FlowFatalError`, and the launch refuses it
    return _cached_chipdb(owner, entry, selection, identity) if entry.exists() else None


def prepare_chipdb(
    layout: XilinxLayout,
    selection: XilinxSelection,
    run_directory: RunDirectory,
    identity: XilinxChipdbIdentity | None = None,
    *,
    environment: Mapping[str, str],
) -> Path:
    """Validate an explicit chipdb, or prepare a shared immutable cache entry named by content.

    Generation needs a claimed `RunDirectory` under a marked run root. The function never
    rebuilds the run root from a flow's run directory. A durable lock beside the entry
    coordinates POSIX processes with the existing lock code. Windows has no process locking.
    `xeda scrub` leaves this cache and its locks alone. A cache hit starts no program. The
    manifest records the provenance of the generator. `identity` is the identity of this
    installation and device data, if the caller has formed it already (`chipdb_identity`).
    `environment` is the environment of the generator and the assembler.
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
    identity = identity or chipdb_identity(layout, selection)
    cache = _cache_path(owner, root / ".cache/xilinx-chipdb")
    entry = _cache_path(owner, cache / identity.key)
    _cache_path(owner, lock_file(entry))
    cache.mkdir(parents=True, exist_ok=True)
    with run_dir_lock(entry):
        # Check the paths again after the wait, before reading or writing the entry.
        _cache_path(owner, entry)
        _cache_path(owner, lock_file(entry))
        if entry.exists():
            return _cached_chipdb(owner, entry, selection, identity)
        # Hash the installation again only before generating. A hit hashed it once.
        if chipdb_identity(layout, selection) != identity:
            raise FlowFatalError(
                f"Xilinx chipdb inputs changed while waiting for {entry}; run again."
            )
        # The entry's exclusive lock is held, so no process is generating this key. Any
        # scratch directory of the key is the remains of a killed generation (several GB).
        for interrupted in sorted(cache.glob(f"{identity.key}.tmp-*")):
            owner.remove(_cache_path(owner, interrupted))
        temporary = Path(tempfile.mkdtemp(prefix=f"{identity.key}.tmp-", dir=cache))
        scratch = RunDirectory.claimed(_cache_path(owner, temporary), root)
        try:
            binary = _generate_chipdb(layout, selection, scratch, entry, environment)
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
                    f"Xilinx chipdb inputs changed during generation for {entry}; run again."
                )
            _cache_path(owner, entry)
            _cache_path(owner, temporary)
            temporary.rename(entry)
            return entry / binary.name
        finally:
            owner.remove(temporary)
