"""Read-only openXC7 layout, mapped fabric, content identity and binary-header contracts."""

import builtins
import json
import shutil
import struct
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from xeda.flow import FlowFatalError
from xeda.run_dir import RunDirectory, RunDirectoryError
from xeda.run_root import ensure_run_root


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


@pytest.fixture
def generation(prefix, tmp_path):
    """Real subprocess fakes: the installation is read-only during generation."""
    mode = _write(tmp_path / "mode", "ok")
    calls = tmp_path / "generation-calls.jsonl"
    generator = prefix / "share/nextpnr/himbaechel/uarch/xilinx/gen/xilinx_gen.py"
    _write(generator.parent / "support.py", "VALUE = 'complete bba'\n")
    generator.write_text(
        "import argparse, json, os, signal, time\n"
        "from pathlib import Path\n"
        "from support import VALUE\n"
        "p = argparse.ArgumentParser()\n"
        "for name in ('xray', 'device', 'bba'): p.add_argument('--' + name, required=True)\n"
        "a = p.parse_args()\n"
        f"mode = Path({str(mode)!r}).read_text()\n"
        f"with Path({str(calls)!r}).open('a') as f:\n"
        " f.write(json.dumps({'cwd': os.getcwd(), 'args': vars(a), "
        "'bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}) + '\\n')\n"
        "Path(a.bba).write_text(VALUE)\n"
        "time.sleep(0.2)\n"
        "if mode == 'partial': raise SystemExit(9)\n"
        "if mode == 'signal': os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    assembler = prefix / "bin/bbasm"
    assembler.write_text(
        f"#!{sys.executable}\n"
        "import sys\nfrom pathlib import Path\n"
        f"mode = Path({str(mode)!r}).read_text()\n"
        "assert sys.argv[1] == '-l'\n"
        "assert Path(sys.argv[2]).read_text() == 'complete bba'\n"
        "if mode == 'assembler': raise SystemExit(8)\n"
        f"data = bytes.fromhex({bytes(_binary()).hex()!r})\n"
        "if mode == 'header': data = b'bad header'\n"
        "if mode != 'missing': Path(sys.argv[3]).write_bytes(data)\n"
    )
    assembler.chmod(0o755)
    root = ensure_run_root(tmp_path / "run")
    owned = RunDirectory.claimed(root / "d/consumer", root)
    return prefix, owned, mode, calls


def _prepare(api, generation):
    prefix, owned, _mode, _calls = generation
    layout, selection = _selection(api, prefix)
    return api.prepare_chipdb(layout, selection, owned)


def test_generated_cache_is_atomic_immutable_and_shared(api, generation):
    prefix, owned, _mode, calls = generation
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in prefix.rglob("*") if p.is_file()}
    chipdb = _prepare(api, generation)
    layout, selection = _selection(api, prefix)
    identity = api.chipdb_identity(layout, selection)
    assert chipdb == owned.run_root / ".cache/xilinx-chipdb" / identity.key / "xc7a100t.bin"
    manifest = yaml.safe_load((chipdb.parent / "manifest.yaml").read_text())
    assert manifest["identity"] == identity.key
    assert manifest["fabric"] == selection.fabric
    assert manifest["sha"] == api.record_file(chipdb).sha
    assert manifest["inputs"]["bbasm_digest"] == identity.bbasm_digest
    states = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in chipdb.parent.iterdir()}
    other = RunDirectory.claimed(owned.run_root / "other/consumer", owned.run_root)
    assert api.prepare_chipdb(layout, selection, other) == chipdb
    assert states == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in states}
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    assert not list(prefix.rglob("__pycache__"))
    assert not owned.path.exists()
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(records) == 1 and records[0]["bytecode"] == "1"
    assert Path(records[0]["cwd"]).parent == chipdb.parent.parent
    assert records[0]["args"]["xray"] == str(layout.database / selection.family)
    assert not list(chipdb.parent.parent.glob("*.tmp-*"))


@pytest.mark.parametrize("mode", ["partial", "signal", "assembler", "missing", "header"])
def test_failed_generation_publishes_nothing_and_allows_retry(api, generation, mode):
    _prefix, owned, mode_file, _calls = generation
    mode_file.write_text(mode)
    with pytest.raises(FlowFatalError) as error:
        _prepare(api, generation)
    assert "xc7a100t" in str(error.value)
    if mode == "signal":
        assert "memory pressure" in str(error.value) and "generator" in str(error.value)
    cache = owned.run_root / ".cache/xilinx-chipdb"
    assert not list(cache.glob("*/manifest.yaml"))
    assert not list(cache.glob("*.tmp-*"))
    mode_file.write_text("ok")
    assert _prepare(api, generation).is_file()


@pytest.mark.parametrize("damage", ["header", "digest", "manifest"])
def test_corrupt_published_entry_is_an_error_without_replacement(api, generation, damage):
    chipdb = _prepare(api, generation)
    if damage == "header":
        chipdb.write_bytes(b"broken")
    elif damage == "digest":
        content = bytearray(chipdb.read_bytes())
        content[8] ^= 1  # outside the bounded structural header
        chipdb.write_bytes(content)
    else:
        (chipdb.parent / "manifest.yaml").unlink()
    before = chipdb.read_bytes()
    with pytest.raises(FlowFatalError, match="remove.*rerun") as error:
        _prepare(api, generation)
    assert str(chipdb.parent) in str(error.value)
    assert chipdb.read_bytes() == before
    assert len(generation[3].read_text().splitlines()) == 1


@pytest.mark.parametrize("component", [".cache", ".cache/xilinx-chipdb", "entry", "lock"])
def test_cache_links_never_authorize_external_writes(api, generation, tmp_path, component):
    prefix, owned, _mode, _calls = generation
    layout, selection = _selection(api, prefix)
    identity = api.chipdb_identity(layout, selection)
    relative = {
        "entry": f".cache/xilinx-chipdb/{identity.key}",
        "lock": f".cache/xilinx-chipdb/{identity.key}.lock",
    }.get(component, component)
    external = tmp_path / "outside"
    external.mkdir()
    canary = _write(external / "canary", "untouched")
    link = owned.run_root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(canary if component == "lock" else external)
    with pytest.raises((FlowFatalError, RunDirectoryError, ValueError)):
        _prepare(api, generation)
    assert {p.name: p.read_text() for p in external.iterdir()} == {"canary": "untouched"}
    assert link.is_symlink()


def test_explicit_read_only_chipdb_needs_no_owned_cache(api, prefix, tmp_path):
    chipdb = tmp_path / "supplied.bin"
    chipdb.write_bytes(_binary())
    chipdb.chmod(0o444)
    before = chipdb.stat()
    layout, selection = _selection(api, prefix, chipdb=chipdb)
    owned = RunDirectory.unlaunched(tmp_path / "unlaunched")
    assert api.prepare_chipdb(layout, selection, owned) == chipdb
    assert chipdb.stat() == before
    assert not owned.path.exists()


def test_automatic_generation_requires_a_marked_claimed_root(api, generation, tmp_path):
    layout, selection = _selection(api, generation[0])
    for owned in (
        RunDirectory.unlaunched(tmp_path),
        RunDirectory.claimed(tmp_path / "d", tmp_path),
    ):
        with pytest.raises(FlowFatalError, match="claimed|marked"):
            api.prepare_chipdb(layout, selection, owned)


def test_inputs_changed_during_generation_are_not_published(api, generation, monkeypatch):
    original = api.run_process
    layout, selection = _selection(api, generation[0])

    def mutate(executable, *args, **kwargs):
        result = original(executable, *args, **kwargs)
        if executable == str(layout.bbasm):
            (layout.himbaechel / "uarch/xilinx/constids.inc").write_text("changed")
        return result

    monkeypatch.setattr(api, "run_process", mutate)
    with pytest.raises(FlowFatalError, match="changed.*generation"):
        _prepare(api, generation)
    assert not list((generation[1].run_root / ".cache/xilinx-chipdb").glob("*/manifest.yaml"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cache process locks")
def test_two_processes_share_one_generation(api, generation):
    prefix, owned, _mode, calls = generation
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from xeda.flows.xilinx import find_xilinx_layout, select_xilinx, prepare_chipdb\n"
        "from xeda.run_dir import RunDirectory\n"
        "layout = find_xilinx_layout(Path(sys.argv[1]))\n"
        "selection = select_xilinx('xc7a100tcsg324-1', layout)\n"
        "root = Path(sys.argv[2])\n"
        "print(prepare_chipdb(layout, selection, RunDirectory.claimed(root / sys.argv[3], root)))\n"
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(prefix / "bin/nextpnr-himbaechel"),
                str(owned.run_root),
                name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("design_a/consumer", "design_b/consumer")
    ]
    try:
        outputs = [process.communicate(timeout=30) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
        assert outputs[0][0] == outputs[1][0]
        assert len(calls.read_text().splitlines()) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_generation_timeout_leaves_no_entry_and_releases_lock(api, generation, monkeypatch):
    monkeypatch.setattr(api, "CHIPDB_GENERATION_TIMEOUT", 0.05)
    with pytest.raises(FlowFatalError, match="time limit") as error:
        _prepare(api, generation)
    assert "OOM" not in str(error.value)
    cache = generation[1].run_root / ".cache/xilinx-chipdb"
    assert not list(cache.glob("*/manifest.yaml"))
    monkeypatch.setattr(api, "CHIPDB_GENERATION_TIMEOUT", 30)
    assert _prepare(api, generation).is_file()


def test_generation_logs_resource_cost_before_start(api, generation, monkeypatch, caplog):
    original = api.run_process

    def check_log(*args, **kwargs):
        assert "about a minute" in caplog.text and "3.5 GB" in caplog.text
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "run_process", check_log)
    with caplog.at_level("INFO", logger="xeda.flows.xilinx"):
        _prepare(api, generation)


def test_stale_temporary_is_not_a_published_cache_entry(api, generation):
    layout, selection = _selection(api, generation[0])
    identity = api.chipdb_identity(layout, selection)
    cache = generation[1].run_root / ".cache/xilinx-chipdb"
    stale = cache / f"{identity.key}.tmp-interrupted"
    stale.mkdir(parents=True)
    (stale / "xc7a100t.bin").write_bytes(b"partial")
    other = cache / f"{'0' * len(identity.key)}.tmp-other-identity"
    other.mkdir()
    (other / "xc7a100t.bin").write_bytes(b"another build")
    chipdb = _prepare(api, generation)
    assert chipdb.is_file() and chipdb.parent == cache / identity.key
    assert chipdb.read_bytes() != b"partial"
    assert not stale.exists()
    assert (other / "xc7a100t.bin").read_bytes() == b"another build"


def test_interrupted_scratch_is_reclaimed_without_following_links(api, generation, tmp_path):
    layout, selection = _selection(api, generation[0])
    identity = api.chipdb_identity(layout, selection)
    cache = generation[1].run_root / ".cache/xilinx-chipdb"
    cache.mkdir(parents=True)
    external = tmp_path / "outside"
    external.mkdir()
    canary = _write(external / "canary", "untouched")
    (cache / f"{identity.key}.tmp-linked").symlink_to(external)
    with pytest.raises((FlowFatalError, RunDirectoryError)):
        _prepare(api, generation)
    assert canary.read_text() == "untouched"


def test_cache_hit_hashes_the_installation_once(api, generation, monkeypatch):
    _prepare(api, generation)
    hashed = []
    original = api._tree_contents
    monkeypatch.setattr(
        api, "_tree_contents", lambda root, *a, **k: hashed.append(root) or original(root, *a, **k)
    )
    layout, selection = _selection(api, generation[0])
    _prepare(api, generation)
    assert hashed.count(layout.himbaechel) == 1
    assert hashed.count(selection.database / selection.family) == 1


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
