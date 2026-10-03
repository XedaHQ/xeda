"""Read-only openXC7 layout, mapped fabric, content identity and binary-header contracts."""

import builtins
import shutil
import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from xeda.flow import FlowFatalError


@pytest.fixture
def api():
    from xeda.flows import xilinx

    return xilinx


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


PARTS = (
    ("xc7a100tcsg324-1", "artix7", "xc7a100t", "xc7a100t"),
    ("xc7a35tcsg324-1", "artix7", "xc7a35t", "xc7a50t"),
    ("xc7a50tcsg324-1", "artix7", "xc7a50t", "xc7a50t"),
    ("xc7k420tffg1156-2", "kintex7", "xc7k420t", "xc7k480t"),
    ("xc7s75fgga676-1", "spartan7", "xc7s75", "xc7s100"),
    ("xc7z035ffg676-2L", "zynq7", "xc7z035", "xc7z045"),
    ("xc7vx485tffg1761-3", "virtex7", "xc7vx485t", "xc7vx485t"),
)


@pytest.fixture
def prefix(tmp_path):
    root = tmp_path / "openXC7 install"
    for tool in ("nextpnr-himbaechel", "bbasm"):
        _write(root / "bin" / tool, "same version 1.0.0\n")
    share = root / "share/nextpnr"
    for name in (
        "uarch/xilinx/gen/xilinx_gen.py",
        "himbaechel_dbgen/chip.py",
        "uarch/xilinx/constids.inc",
        "uarch/xilinx/meta/wire_intents.json",
    ):
        _write(share / "himbaechel" / name, "fixture content\n")
    for family in sorted({entry[1] for entry in PARTS}):
        parts = {p: {"device": d} for p, f, d, _ in PARTS if f == family}
        devices = {d: {"fabric": b} for _, f, d, b in PARTS if f == family}
        database = share / "prjxray-db" / family
        _write(database / "mapping/parts.yaml", yaml.safe_dump(parts))
        _write(database / "mapping/devices.yaml", yaml.safe_dump(devices))
        for name in ("timings/slicem.sdf", "timings/BRAM_L.sdf", "tile_type_CLBLL_L.json"):
            _write(database / name, "fixture content\n")
        for p, f, _, fabric in PARTS:
            if f == family:
                _write(database / fabric / "tilegrid.json", "{}\n")
                _write(database / fabric / "tileconn.json", "{}\n")
                _write(database / p / "package_pins.csv", "pin,site\nA1,IOB_X0Y0\n")
    return root


def _layout(api, prefix, **kwargs):
    return api.find_xilinx_layout(prefix / "bin/nextpnr-himbaechel", **kwargs)


def _selection(api, prefix, part=PARTS[0][0], **kwargs):
    layout = _layout(api, prefix, **kwargs)
    return layout, api.select_xilinx(part, layout)


def _binary(fabric="xc7a100t", *, offset=32):
    # Empty table slices are sufficient for structural validation, never routing.
    header = bytearray(struct.pack("<4I", 0x00CA7CA7, 6, 148, 209) + bytes(68))
    strings = (b"xilinx", fabric.encode(), b"python_dbgen")
    start = offset + len(header)
    for i, string in enumerate(strings):
        field = 16 + 4 * i
        struct.pack_into("<i", header, field, start - (offset + field))
        start += len(string) + 1
    return bytearray(
        struct.pack("<i", offset) + bytes(offset - 4) + header + b"\0".join(strings) + b"\0"
    )


def test_layout_resolves_executable_links_before_finding_prefix(api, prefix, tmp_path):
    link = tmp_path / "wrappers/nextpnr-himbaechel"
    link.parent.mkdir()
    link.symlink_to(prefix / "bin/nextpnr-himbaechel")
    layout = api.find_xilinx_layout(link)
    assert layout.nextpnr == (prefix / "bin/nextpnr-himbaechel").resolve()
    assert layout.himbaechel == prefix / "share/nextpnr/himbaechel"
    assert layout.database == prefix / "share/nextpnr/prjxray-db"
    assert layout.generator == layout.himbaechel / "uarch/xilinx/gen/xilinx_gen.py"
    assert layout.bbasm == prefix / "bin/bbasm"
    with pytest.raises(FrozenInstanceError):
        layout.database = tmp_path


@pytest.mark.parametrize("part,family,device,fabric", PARTS)
def test_selects_full_part_through_device_to_fabric(api, prefix, part, family, device, fabric):
    layout, selection = _selection(api, prefix, part.upper())
    assert (selection.part, selection.family, selection.device, selection.fabric) == (
        part.lower(),
        family,
        device,
        fabric,
    )
    assert selection.database == layout.database
    with pytest.raises(FrozenInstanceError):
        selection.fabric = device


@pytest.mark.parametrize("missing", ["himbaechel", "prjxray-db", "generator", "bbasm"])
def test_missing_installation_component_lists_searched_path(api, prefix, missing):
    paths = {
        "himbaechel": prefix / "share/nextpnr/himbaechel",
        "prjxray-db": prefix / "share/nextpnr/prjxray-db",
        "generator": prefix / "share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py",
        "bbasm": prefix / "bin/bbasm",
    }
    path = paths[missing]
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    with pytest.raises(FlowFatalError, match="[Ss]earched") as error:
        _layout(api, prefix)
    assert str(path) in str(error.value)


def test_explicit_database_root_is_used_and_missing_override_never_falls_back(
    api, prefix, tmp_path
):
    external = tmp_path / "external database"
    shutil.copytree(prefix / "share/nextpnr/prjxray-db", external)
    layout, selection = _selection(api, prefix, prjxray_db=external)
    assert layout.database == selection.database == external
    with pytest.raises(FlowFatalError, match="prjxray_db"):
        _layout(api, prefix, prjxray_db=tmp_path / "typo")


def test_layout_ignores_legacy_environment_and_path_assembler(api, prefix, monkeypatch, tmp_path):
    for name in ("CHIPDB_DIR", "NEXTPNR_XILINX_PYTHON_DIR", "PRJXRAY_DB_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "unrelated"))
    monkeypatch.setenv("PATH", str(tmp_path / "unrelated"))
    assert _layout(api, prefix).database == prefix / "share/nextpnr/prjxray-db"
    (prefix / "bin/bbasm").unlink()
    with pytest.raises(FlowFatalError, match="bbasm"):
        _layout(api, prefix)


@pytest.mark.parametrize(
    "part", ["xc7a100tcsg324", "xc7a100tcsg324-3", "xc7a100tcsg324-1junk", "ice40"]
)
def test_unknown_full_part_is_an_actionable_error(api, prefix, part):
    layout = _layout(api, prefix)
    with pytest.raises(FlowFatalError) as error:
        api.select_xilinx(part, layout)
    assert part in str(error.value)


@pytest.mark.parametrize("missing", ["mapping/parts.yaml", "mapping/devices.yaml", "xc7a100t"])
def test_selection_requires_mapping_metadata_and_fabric(api, prefix, missing):
    root = prefix / "share/nextpnr/prjxray-db/artix7"
    path = root / missing
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    with pytest.raises(FlowFatalError) as error:
        _selection(api, prefix)
    assert str(path) in str(error.value) and PARTS[0][0] in str(error.value)


@pytest.mark.parametrize(
    "content", ["[", "[]", "xc7a100tcsg324-1: {}", "xc7a100tcsg324-1: {device: ../escape}"]
)
def test_bad_mapping_metadata_is_a_contextual_error(api, prefix, content):
    path = prefix / "share/nextpnr/prjxray-db/artix7/mapping/parts.yaml"
    path.write_text(content)
    with pytest.raises(FlowFatalError) as error:
        _selection(api, prefix)
    assert str(path) in str(error.value) and PARTS[0][0] in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_explicit_chipdb_is_one_existing_file_without_fallback(api, prefix, tmp_path, kind):
    chipdb = tmp_path / "supplied.bin"
    if kind == "directory":
        chipdb.mkdir()
        (chipdb / "xc7a100t.bin").write_bytes(_binary())
    with pytest.raises(FlowFatalError, match="chipdb") as error:
        _layout(api, prefix, chipdb=chipdb)
    assert str(chipdb) in str(error.value)


def test_explicit_chipdb_needs_only_mapping_metadata_not_generation_tools(api, prefix, tmp_path):
    chipdb = tmp_path / "supplied.bin"
    chipdb.write_bytes(_binary())
    shutil.rmtree(prefix / "share/nextpnr/himbaechel")
    (prefix / "bin/bbasm").unlink()
    layout, selection = _selection(api, prefix, chipdb=chipdb)
    assert layout.chipdb == chipdb
    assert layout.generator is None and layout.bbasm is None
    assert api.validate_chipdb(chipdb, selection).fabric == "xc7a100t"
    (layout.database / "artix7/mapping/devices.yaml").unlink()
    with pytest.raises(FlowFatalError, match="devices.yaml"):
        api.select_xilinx(PARTS[0][0], layout)


def test_identity_is_content_only_with_sorted_relative_names_and_fixed_recipe(
    api, prefix, tmp_path
):
    layout, selection = _selection(api, prefix)
    identity = api.chipdb_identity(layout, selection)
    moved = tmp_path / "another prefix"
    shutil.copytree(prefix, moved)
    other_layout, other_selection = _selection(api, moved)
    assert api.chipdb_identity(other_layout, other_selection) == identity
    assert identity.assembler_args == ("-l",)
    assert identity.version == 6 and identity.endianness == "little"
    assert identity.fabric == selection.fabric
    assert identity.himbaechel_files == tuple(sorted(identity.himbaechel_files))
    assert identity.database_files == tuple(sorted(identity.database_files))
    assert len(identity.key) == 64
    assert all(not Path(name).is_absolute() for name, _ in identity.himbaechel_files)
    with pytest.raises(FrozenInstanceError):
        identity.key = "changed"


MUTATIONS = (
    "bin/nextpnr-himbaechel",
    "bin/bbasm",
    "share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py",
    "share/nextpnr/himbaechel/himbaechel_dbgen/chip.py",
    "share/nextpnr/himbaechel/uarch/xilinx/constids.inc",
    "share/nextpnr/himbaechel/uarch/xilinx/meta/wire_intents.json",
    "share/nextpnr/prjxray-db/artix7/mapping/parts.yaml",
    "share/nextpnr/prjxray-db/artix7/mapping/devices.yaml",
    "share/nextpnr/prjxray-db/artix7/timings/slicem.sdf",
    "share/nextpnr/prjxray-db/artix7/xc7a100t/tilegrid.json",
    "share/nextpnr/prjxray-db/artix7/xc7a100t/tileconn.json",
    "share/nextpnr/prjxray-db/artix7/tile_type_CLBLL_L.json",
    "share/nextpnr/prjxray-db/artix7/xc7a100tcsg324-1/package_pins.csv",
)


@pytest.mark.parametrize("name", MUTATIONS)
def test_successive_identity_calls_notice_edits_without_version_changes(api, prefix, name):
    layout, selection = _selection(api, prefix)
    before = api.chipdb_identity(layout, selection)
    path = prefix / name
    old = path.read_bytes()
    path.write_bytes(old + b"\n# same version, changed content\n")
    assert api.chipdb_identity(layout, selection).key != before.key
    path.write_bytes(old)
    assert api.chipdb_identity(layout, selection) == before


def test_identity_notices_relative_rename_and_added_files(api, prefix):
    layout, selection = _selection(api, prefix)
    before = api.chipdb_identity(layout, selection)
    path = _write(layout.himbaechel / "additional.py", "new content")
    added = api.chipdb_identity(layout, selection)
    assert added.key != before.key
    path.rename(path.with_name("renamed.py"))
    assert api.chipdb_identity(layout, selection).key != added.key


def test_identity_tracks_directory_alias_content_without_install_path_identity(
    api, prefix, tmp_path
):
    layout, selection = _selection(api, prefix)
    first = _write(layout.himbaechel / "first/data", "first content").parent
    second = _write(layout.himbaechel / "second/data", "second content").parent
    link = layout.himbaechel / "directory_alias"
    link.symlink_to(first, target_is_directory=True)
    before = api.chipdb_identity(layout, selection)
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert api.chipdb_identity(layout, selection).key != before.key
    moved = tmp_path / "copy"
    shutil.copytree(prefix, moved)
    moved_layout, moved_selection = _selection(api, moved)
    assert api.chipdb_identity(moved_layout, moved_selection) == api.chipdb_identity(
        layout, selection
    )


def test_unreadable_identity_input_is_a_contextual_error(api, prefix, monkeypatch):
    layout, selection = _selection(api, prefix)
    original = builtins.open
    target = layout.himbaechel / "himbaechel_dbgen/chip.py"

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("unreadable test input")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    with pytest.raises(FlowFatalError) as error:
        api.chipdb_identity(layout, selection)
    assert str(target) in str(error.value) and selection.part in str(error.value)


def test_unreadable_mapping_is_a_contextual_error(api, prefix, monkeypatch):
    layout = _layout(api, prefix)
    path = layout.database / "artix7/mapping/parts.yaml"
    original = Path.open

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("unreadable test mapping")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(FlowFatalError) as error:
        api.select_xilinx(PARTS[0][0], layout)
    assert str(path) in str(error.value) and PARTS[0][0] in str(error.value)


@pytest.mark.parametrize("missing", ["device", "fabric"])
def test_missing_device_or_fabric_mapping_is_a_contextual_error(api, prefix, missing):
    root = prefix / "share/nextpnr/prjxray-db/artix7/mapping"
    path = root / ("parts.yaml" if missing == "device" else "devices.yaml")
    path.write_text("{}\n")
    with pytest.raises(FlowFatalError) as error:
        _selection(api, prefix)
    assert str(path) in str(error.value) and PARTS[0][0] in str(error.value)


def test_marketing_aliases_share_fabric_identity(api, prefix):
    layout, a35 = _selection(api, prefix, PARTS[1][0])
    a50 = api.select_xilinx(PARTS[2][0], layout)
    assert api.chipdb_identity(layout, a35) == api.chipdb_identity(layout, a50)


@pytest.mark.parametrize("part", [PARTS[3][0], PARTS[6][0]])
def test_identity_includes_artix_timing_data_used_by_kintex_and_virtex_generator(api, prefix, part):
    layout, selection = _selection(api, prefix, part)
    before = api.chipdb_identity(layout, selection)
    path = layout.database / "artix7/timings/slicem.sdf"
    path.write_text("changed fallback timing\n")
    assert api.chipdb_identity(layout, selection).key != before.key


@pytest.mark.parametrize("part,family,device,fabric", PARTS)
def test_valid_header_checks_selected_fabric_including_aliases(
    api, prefix, tmp_path, part, family, device, fabric
):
    _, selection = _selection(api, prefix, part)
    path = tmp_path / "chipdb.bin"
    path.write_bytes(_binary(fabric))
    header = api.validate_chipdb(path, selection)
    assert (header.version, header.width, header.height, header.fabric) == (6, 148, 209, fabric)
    assert (header.uarch, header.generator, header.offset) == ("xilinx", "python_dbgen", 32)
    with pytest.raises(FrozenInstanceError):
        header.version = 5
    if device != fabric:
        path.write_bytes(_binary(device))
        with pytest.raises(FlowFatalError, match="fabric"):
            api.validate_chipdb(path, selection)


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "random",
        "negative",
        "zero-offset",
        "past-end",
        "short-header",
        "magic",
        "version",
        "uarch",
        "fabric",
        "generator",
        "unterminated",
        "negative-pointer",
        "past-end-pointer",
        "zero-width",
        "zero-height",
        "truncated-tail",
        "junk",
        "big-endian",
    ],
)
def test_corrupt_headers_are_rejected_before_use(api, prefix, tmp_path, case):
    _, selection = _selection(api, prefix)
    data = _binary()
    offset = 32
    if case == "empty":
        data = b""
    elif case == "random":
        data = b"random bytes"
    elif case in ("negative", "zero-offset", "past-end"):
        struct.pack_into(
            "<i", data, 0, {"negative": -4, "zero-offset": 0, "past-end": len(data)}[case]
        )
    elif case == "short-header":
        data = data[: offset + 28]
    elif case in ("magic", "version", "zero-width", "zero-height"):
        field, value = {
            "magic": (0, 0),
            "version": (4, 5),
            "zero-width": (8, 0),
            "zero-height": (12, 0),
        }[case]
        struct.pack_into("<I", data, offset + field, value)
    elif case in ("uarch", "fabric", "generator"):
        old, new = {
            "uarch": (b"xilinx", b"nexus!"),
            "fabric": (b"xc7a100t", b"xc7a200t"),
            "generator": (b"python_dbgen", b"other_dbgen!"),
        }[case]
        data = data.replace(old, new)
    elif case == "unterminated":
        data[-1] = 65
    elif case in ("negative-pointer", "past-end-pointer"):
        struct.pack_into(
            "<i", data, offset + 16, -1000 if case == "negative-pointer" else len(data)
        )
    elif case == "truncated-tail":
        data = data[:-5]
    elif case == "junk":
        data += b"junk"
    elif case == "big-endian":
        struct.pack_into(">i", data, 0, offset)
    path = tmp_path / "chipdb.bin"
    path.write_bytes(data)
    with pytest.raises(FlowFatalError) as error:
        api.validate_chipdb(path, selection)
    assert str(path) in str(error.value) and selection.part in str(error.value)


def test_header_validation_reads_bounded_ranges_not_whole_binary(
    api, prefix, tmp_path, monkeypatch
):
    _, selection = _selection(api, prefix)
    path = tmp_path / "chipdb.bin"
    path.write_bytes(_binary(offset=1 << 20))
    original = Path.open
    reads = []

    class Reader:
        def __enter__(self):
            self.stream = original(path, "rb")
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= 256
            reads.append(size)
            return self.stream.read(size)

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    assert api.validate_chipdb(path, selection).offset == 1 << 20
    assert sum(reads) < 512


def test_valid_header_accepts_signed_backward_string_pointers(api, prefix, tmp_path):
    _, selection = _selection(api, prefix)
    data = _binary(offset=128)
    strings = b"xilinx\0xc7a100t\0python_dbgen\0"
    data[4 : 4 + len(strings)] = strings
    target = 4
    for i, text in enumerate((b"xilinx", b"xc7a100t", b"python_dbgen")):
        pointer = 128 + 16 + 4 * i
        struct.pack_into("<i", data, pointer, target - pointer)
        target += len(text) + 1
    path = tmp_path / "chipdb.bin"
    path.write_bytes(data)
    assert api.validate_chipdb(path, selection).offset == 128


def test_unreadable_header_reports_path_and_part(api, prefix, tmp_path, monkeypatch):
    _, selection = _selection(api, prefix)
    path = tmp_path / "chipdb.bin"
    path.write_bytes(_binary())

    def denied(*args, **kwargs):
        raise PermissionError("unreadable test file")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(FlowFatalError) as error:
        api.validate_chipdb(path, selection)
    assert str(path) in str(error.value) and selection.part in str(error.value)


def test_explicit_chipdb_cannot_be_given_a_generation_identity(api, prefix, tmp_path):
    path = tmp_path / "supplied.bin"
    path.write_bytes(_binary())
    layout, selection = _selection(api, prefix, chipdb=path)
    with pytest.raises(FlowFatalError, match="explicit chipdb"):
        api.chipdb_identity(layout, selection)
