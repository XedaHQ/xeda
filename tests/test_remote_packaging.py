"""`package_inputs`: what a request sends to a remote host, with no transport."""

import gzip
import io
import json
import shutil
from pathlib import Path, PurePosixPath

import pytest

from xeda import Design
from xeda.design import SourceType
from xeda.flow import FlowSettingsException
from xeda.remote.manifest import apply_sync, build_manifest, plan_sync, write_tar
from xeda.remote.packaging import InputPackage, package_inputs

from .io_flows import _ReadsInstalled, _ReadsInstalledNested  # noqa: F401  (registers the flows)
from .test_git_dependencies import git_log, remote
from .tiny_platform import TINY_OPENROAD_RESIDUE

FIXTURES = (git_log, remote)

pytestmark = pytest.mark.python_compat

REMOTE_INPUTS = PurePosixPath("/remote/xeda/v1/ws/key/inputs/d")
STORE_KEY = "__reads_installed"
NESTED_KEY = "__reads_installed_nested"


def _design(tmp_path: Path, **kwargs) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    kwargs.setdefault("rtl", {"sources": [], "top": "t"})
    return Design(name="d", design_root=root, **kwargs)


def _package(design: Design, sections, tmp_path: Path, **kwargs) -> InputPackage:
    return package_inputs(
        design,
        sections,
        set(sections),
        run_root=tmp_path / "xeda_run",
        remote_inputs=REMOTE_INPUTS,
        **kwargs,
    )


def _shipped(package: InputPackage) -> dict:
    return json.loads(package.generated[package.design_file])


def _store(path: Path) -> Path:
    path.mkdir()
    (path / "lib.txt").write_text("secret of this machine\n")
    return path


@pytest.mark.parametrize("place", ["flat", "nested"])
def test_locator_setting_comes_only_from_the_host_profile(tmp_path, place):
    store = _store(tmp_path / "store")
    if place == "flat":
        key, local, host = STORE_KEY, {"store": str(store)}, {"store": "/opt/pdk"}
        expected = lambda value: {"store": value}  # noqa: E731
    else:
        key, local, host = (
            NESTED_KEY,
            {"where": {"store": str(store)}},
            {"where": {"store": "/opt/pdk"}},
        )
        expected = lambda value: {"where": {"store": value}}  # noqa: E731
    design = _design(tmp_path)

    local_only = _package(design, {key: local}, tmp_path)
    assert _shipped(local_only)["flows"][key] == expected(None)

    # both set: the host profile wins on the remote, the local value never travels
    with_host = _package(design, {key: local}, tmp_path, host_flows={key: host})
    assert _shipped(with_host)["flows"][key] == expected("/opt/pdk")
    for package in (local_only, with_host):
        assert not package.files and not package.path_identities
        assert list(package.generated) == [package.design_file]
        assert str(store) not in package.generated[package.design_file].decode()


def test_host_value_of_another_flow_is_not_used(tmp_path):
    store = _store(tmp_path / "store")
    package = _package(
        _design(tmp_path),
        {STORE_KEY: {"store": str(store)}},
        tmp_path,
        host_flows={NESTED_KEY: {"where": {"store": "/opt/pdk"}}},
    )
    assert _shipped(package)["flows"][STORE_KEY] == {"store": None}


def _liberty_chain(tmp_path: Path) -> Path:
    parts = tmp_path / "libs" / "parts"
    parts.mkdir(parents=True)
    units = parts / "units.lib.gz"
    units.write_bytes(gzip.compress(b'  time_unit : "1ns";\n'))
    nested = parts / "nested.lib.gz"
    nested.write_bytes(gzip.compress(f"include_file({units});\n".encode()))
    library = tmp_path / "libs" / "own.lib"
    library.write_text(f"library (t) {{ include_file({nested}); }}\n")
    return library


def _openroad_sections(library: Path) -> dict:
    return {
        "openroad_place": {"platform_file": str(TINY_OPENROAD_RESIDUE), "liberty": [str(library)]}
    }


def test_liberty_rewrite_names_the_remote_inputs_path(tmp_path):
    library = _liberty_chain(tmp_path)
    package = _package(_design(tmp_path), _openroad_sections(library), tmp_path)

    (shipped,) = _shipped(package)["flows"]["openroad_place"]["liberty"]
    assert shipped.startswith("$DESIGN_ROOT/_liberty_inputs/")
    copy = package.generated[shipped.removeprefix("$DESIGN_ROOT/")].decode()
    include = copy.split("include_file(")[1].split(")")[0]
    assert include.startswith(f"{REMOTE_INPUTS}/_liberty_inputs/")
    assert str(tmp_path) not in copy
    # the included file is renamed in turn, and its own include too
    nested = package.generated[PurePosixPath(include).relative_to(REMOTE_INPUTS).as_posix()]
    assert str(REMOTE_INPUTS).encode() in gzip.decompress(nested)
    assert any(name.endswith("units.lib.gz") for name in package.files)  # nothing to rename
    # the identity of the run is that of the original file
    assert package.path_identities[str(REMOTE_INPUTS / shipped.removeprefix("$DESIGN_ROOT/"))] == {
        "setting": str(library)
    }


def test_liberty_of_a_flow_outside_the_plan_travels_as_a_path(tmp_path):
    library = tmp_path / "unused.lib"
    library.write_text(f"library (t) {{ include_file({tmp_path / 'gone.lib'}); }}\n")
    package = package_inputs(
        _design(tmp_path),
        _openroad_sections(library),
        set(),
        run_root=tmp_path / "xeda_run",
        remote_inputs=REMOTE_INPUTS,
    )
    assert not any(name.startswith("_liberty_inputs") for name in package.generated)
    assert library in package.files.values()


def test_liberty_include_that_cannot_be_followed_stops_the_packaging(tmp_path):
    library = tmp_path / "own.lib"
    library.write_text("library (t) { include_file(units.lib); }\n")
    with pytest.raises(FlowSettingsException, match=r"own\.lib"):
        _package(_design(tmp_path), _openroad_sections(library), tmp_path)


def test_package_is_deterministic(tmp_path):
    library = _liberty_chain(tmp_path)
    design = _design(tmp_path)
    sections = _openroad_sections(library)
    first = _package(design, sections, tmp_path)
    second = _package(design, sections, tmp_path)
    assert first == second
    assert build_manifest(first.files, first.generated) == build_manifest(
        second.files, second.generated
    )


def test_the_package_unpacks_into_the_design_it_describes(tmp_path):
    design_root = tmp_path / "design"
    (design_root / "rtl").mkdir(parents=True)
    (design_root / "inc").mkdir()
    (design_root / "rtl" / "top.v").write_text('`include "defs.vh"\nmodule top; endmodule\n')
    (design_root / "rtl" / "defs.vh").write_text("`define VAL 1\n")
    (design_root / "inc" / "defs.vh").write_text("`define VAL 2\n")
    (design_root / "rom.mem").write_text("00\n")
    design = Design(
        name="d",
        design_root=design_root,
        rtl={
            "sources": ["rtl/top.v", "rtl/defs.vh", "inc/defs.vh"],
            "top": "top",
            "parameters": {"ROM": {"file": "rom.mem"}, "OUT": {"path": "out/t.txt"}},
        },
    )
    package = _package(design, {}, tmp_path)
    manifest = build_manifest(package.files, package.generated)
    assert [entry.path for entry in manifest] == sorted(entry.path for entry in manifest)

    archive = io.BytesIO()
    write_tar(manifest, package.files, package.generated, archive)
    # unpack with the standard library only: what the remote does is the task of manifest.py
    import tarfile

    remote = tmp_path / "remote"
    archive.seek(0)
    with tarfile.open(fileobj=archive) as tar:
        tar.extractall(remote, filter="data")
    loaded = Design.from_file(remote / package.design_file)
    assert [loaded.source_path_as_named(src).as_posix() for src in loaded.rtl.sources] == [
        "rtl/top.v",
        "rtl/defs.vh",
        "inc/defs.vh",
    ]
    assert loaded.rtl_hash == design.rtl_hash
    rom = Path(loaded.rtl.parameters["ROM"])
    assert rom.read_text() == "00\n" and rom.is_relative_to(remote)
    assert Path(loaded.rtl.parameters["OUT"]) == remote / "out" / "t.txt"


def test_a_design_file_name_taken_by_a_source_moves_the_source(tmp_path):
    design = _design(tmp_path)
    (design.root_path / "d.xeda.json").write_text("{}\n")
    design = Design(
        name="d",
        design_root=design.root_path,
        rtl={"sources": [{"file": "d.xeda.json", "type": "JsonNetlist"}], "top": "t"},
    )
    package = _package(design, {}, tmp_path)
    assert package.files.keys().isdisjoint(package.generated)
    (source,) = _shipped(package)["rtl"]["sources"]
    assert source["file"] != "d.xeda.json" and source["file"] in package.files


def test_testbench_python_packages_travel(tmp_path):
    design = _design(tmp_path, tb={"sources": [], "python_packages": ["cocotb"]})
    assert _shipped(_package(design, {}, tmp_path))["tb"]["python_packages"] == ["cocotb"]


def test_the_package_lists_only_regular_files_that_exist(tmp_path):
    shutil.copy(Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.vhdl", tmp_path)
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [{"file": "sqrt.vhdl", "type": SourceType.Vhdl}], "top": "sqrt"},
    )
    package = _package(design, {}, tmp_path)
    assert set(package.files) == {"sqrt.vhdl"}
    assert all(path.is_file() for path in package.files.values())


# ------------------------------------------------------------------------ the remote plan
#
# The remote plans the packaged design again, and the local xeda compares the identity of every
# node with its own plan. So the packaged document must plan the same nodes, with the same hashes,
# in the same places below the run root, as the request it was made from.

TARGETS = Path(__file__).parent / "resources" / "targets"
SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
PROJECT = {
    "flows": {
        "vivado_synth": {"clocks": {"main_clock": {"period": "5 ns"}}},
        "nextpnr": {"seed": 9},
    },
}


def _targets_copy(tmp_path: Path) -> Path:
    shutil.copytree(TARGETS, tmp_path / "targets")
    return tmp_path / "targets"


def _project(directory: Path) -> str:
    import yaml

    design = yaml.safe_load((directory / "knight.yaml").read_text())
    design["design_root"] = str(directory)
    path = directory / "xedaproject.yaml"
    path.write_text(yaml.safe_dump({**PROJECT, "designs": [design]}))
    return str(path)


def _nodes(plan, run_root: Path) -> list[tuple]:
    return [
        (node.name, node.flowrun_hash, node.run_path.relative_to(run_root).as_posix())
        for node in plan.nodes
    ]


def _directory_input(tmp_path: Path) -> Path:
    """A directory with a nested file, a nested empty directory and a link."""
    root = tmp_path / "data"
    (root / "deep" / "deeper").mkdir(parents=True)
    (root / "empty" / "inner").mkdir(parents=True)
    (root / "top.txt").write_text("top\n")
    (root / "deep" / "deeper" / "x.txt").write_text("x\n")
    (root / "linked.txt").symlink_to(root / "top.txt")
    return root


def _sync(package: InputPackage, remote_dir: Path) -> None:
    """Put the package into `remote_dir` as the remote does: by manifest, tar and sync."""
    entries = build_manifest(package.files, package.generated)
    plan = plan_sync(remote_dir, entries)
    stream = io.BytesIO()
    missing = [entry for entry in entries if entry.path in set(plan.missing)]
    write_tar(missing, package.files, package.generated, stream)
    apply_sync(remote_dir, entries, plan, io.BytesIO(stream.getvalue()), package.directories)


def _remote_plan(tmp_path, prepared, local_root, flow, hashed):
    """Package a prepared request, put the package where the remote has it, load the document as
    the launch entry does and plan it. Return the package, the plan of the remote, the run root of
    the remote and the directory of the inputs."""
    from xeda.utils import using_path_identities
    from xeda.flow_runner import DefaultRunner
    from xeda.remote.launch_entry import load_packaged_design

    remote_dir = tmp_path / "remote_inputs"
    package = package_inputs(
        prepared.declared,
        prepared.sections,
        {node.name for node in prepared.plan.nodes},
        run_root=local_root,
        remote_inputs=PurePosixPath(remote_dir),
        bindings=prepared.plan.bindings,
        request=prepared.plan.request,
        resolved=prepared.design.resolved_dependencies,
    )
    _sync(package, remote_dir)
    remote_root = tmp_path / "remote" / "xeda_run"
    remote = DefaultRunner(remote_root, display_results=False, hashed_run_dirs=hashed)
    loaded = load_packaged_design(remote_dir / package.design_file)
    with using_path_identities(package.path_identities):
        remote_plan = remote.plan(flow, design=loaded)
    return package, remote_plan, remote_root, remote_dir


@pytest.mark.parametrize("hashed", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        dict(flow="vivado_synth", target="arty", settings=[]),
        dict(flow="vivado_synth", target="arty", settings=["clocks.main_clock.period=8 ns"]),
        dict(flow="vivado_synth", target=None, settings=["fpga.part=xc7a100tcsg324-1"]),
        dict(flow="nextpnr", target="ulx3s", settings=["seed=4"]),
        dict(flow="yosys_fpga+nextpnr", target="ulx3s", settings=["seed=4"]),
        dict(flow="nextpnr", target="ulx3s", settings=[], project=True),
        dict(
            flow="vivado_synth",
            target="arty",
            settings=["clocks.main_clock.period=8 ns"],
            project=True,
        ),
        dict(flow="__input_maker", target=None, settings=["input_file=@data"], directory=True),
        dict(flow="__input_maker", target=None, settings=["input_file=@data"], directory="inside"),
    ],
    ids=[
        "target",
        "target-and-s",
        "s-only",
        "chain-target-s",
        "explicit-chain",
        "project-target",
        "project-s",
        "directory-setting",
        "directory-setting-in-design-root",
    ],
)
def test_the_remote_plans_the_nodes_of_the_local_plan(tmp_path, case, hashed):
    from xeda.flow_runner import DefaultRunner

    directory = _targets_copy(tmp_path)
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=hashed)
    settings = list(case["settings"])
    if case.get("directory"):
        data = _directory_input(directory if case["directory"] == "inside" else tmp_path)
        settings = [setting.replace("@data", str(data)) for setting in settings]
    if case.get("project"):
        request = dict(design="knight", xedaproject=_project(directory))
    else:
        request = dict(design=directory / "knight.yaml")
    prepared = local.prepare_request(
        case["flow"], flow_settings=settings, target=case["target"], **request
    )
    assert prepared.design.target == case["target"]

    package, remote_plan, remote_root, remote_dir = _remote_plan(
        tmp_path, prepared, local_root, case["flow"], hashed
    )
    if case.get("directory"):
        assert any(name.endswith("deep/deeper/x.txt") for name in package.files)
        assert any(name.endswith("linked.txt") for name in package.files)
        (empty,) = package.directories
        assert empty.endswith("empty/inner") and (remote_dir / empty).is_dir()
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    assert [node.name for node in remote_plan.nodes][-1] == prepared.plan.nodes[-1].name
    if case["target"]:
        assert _shipped(package)["target"] == case["target"]
        assert all(
            case["target"] in node[2].split("/") for node in _nodes(remote_plan, remote_root)
        )
    else:
        assert "target" not in _shipped(package)


def test_a_directory_setting_reaches_the_remote_as_it_is(tmp_path):
    from xeda.remote.manifest import plan_sync

    data = _directory_input(tmp_path)
    design = _design(tmp_path)
    package = _package(design, {"__input_maker": {"input_file": str(data)}}, tmp_path)
    assert all(path.is_file() for path in package.files.values())
    (setting,) = _shipped(package)["flows"]["__input_maker"].values()
    relative = setting.removeprefix("$DESIGN_ROOT/")
    assert set(package.files) >= {f"{relative}/top.txt", f"{relative}/deep/deeper/x.txt"}
    assert package.directories == [f"{relative}/empty/inner"]
    remote_dir = tmp_path / "remote"
    _sync(package, remote_dir)
    assert (remote_dir / relative / "empty" / "inner").is_dir()
    assert (remote_dir / relative / "deep" / "deeper" / "x.txt").read_text() == "x\n"
    assert (remote_dir / relative / "linked.txt").read_text() == "top\n"
    # a second sync changes nothing, and an empty directory that left the package goes
    assert plan_sync(remote_dir, build_manifest(package.files, package.generated)).missing == []
    _sync(package, remote_dir)
    package.directories.clear()
    _sync(package, remote_dir)
    assert not (remote_dir / relative / "empty").exists()


def _table(plan, flow: str, *path: str) -> dict:
    """The table of names (a setting in `opaque_key_fields`) that a node of `plan` holds."""
    value = plan.node(flow).settings
    for name in path:
        value = getattr(value, name)
    return {key: str(item) for key, item in value.items()}


def _assert_tables_reach_the_remote(tmp_path, prepared, local_root, flow, *path, expected):
    """The remote gets the table as the local layers merged it, and plans the local nodes."""
    assert _table(prepared.plan, flow, *path) == expected
    package, remote_plan, remote_root, _ = _remote_plan(
        tmp_path, prepared, local_root, flow, hashed=False
    )
    assert _table(remote_plan, flow, *path) == expected
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    return package


def test_a_vivado_table_that_holds_a_name_inside_another_reaches_the_remote_whole(tmp_path):
    from xeda.flow_runner import DefaultRunner

    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl: {sources: [sqrt.vhdl], top: sqrt, clocks: [{port: clk}]}\n"
        "flows:\n  vivado_synth:\n    fpga.part: xc7a12tcsg325-1\n    clocks.main_clock.period: 5ns\n"
        "    set_synth_properties: {A: 1}\n"
    )
    local_root = tmp_path / "local" / "xeda_run"
    prepared = DefaultRunner(local_root, display_results=False).prepare_request(
        "vivado_synth",
        design=design_root / "sqrt.yaml",
        flow_settings=["set_synth_properties.A.B=2"],
    )
    package = _assert_tables_reach_the_remote(
        tmp_path,
        prepared,
        local_root,
        "vivado_synth",
        "set_synth_properties",
        expected={"A": "1", "A.B": "2"},
    )
    shipped = _shipped(package)["flows"]["vivado_synth"]["set_synth_properties"]
    assert shipped == {"A": 1, "A\\.B": "2"}, "the document writes the names with escaped dots"


def test_dotted_names_of_a_vivado_table_reach_the_remote_whole(tmp_path):
    from xeda.flow_runner import DefaultRunner

    design_root = tmp_path / "design"
    design_root.mkdir()
    shutil.copy(SQRT / "sqrt.vhdl", design_root)
    (design_root / "sqrt.yaml").write_text(
        "name: sqrt\nrtl: {sources: [sqrt.vhdl], top: sqrt, clocks: [{port: clk}]}\n"
    )
    local_root = tmp_path / "local" / "xeda_run"
    prepared = DefaultRunner(local_root, display_results=False).prepare_request(
        "vivado_synth",
        design=design_root / "sqrt.yaml",
        flow_settings=[
            "fpga.part=xc7a12tcsg325-1",
            "clocks.main_clock.period=5ns",
            "set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0",
            "set_synth_properties.A.B=2",
            "set_synth_properties.A.C=3",
        ],
    )
    _assert_tables_reach_the_remote(
        tmp_path,
        prepared,
        local_root,
        "vivado_synth",
        "set_synth_properties",
        expected={"STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM": "0", "A.B": "2", "A.C": "3"},
    )


def test_an_openroad_table_that_holds_a_name_inside_another_reaches_the_remote_whole(tmp_path):
    from xeda.flow_runner import DefaultRunner

    from .openroad_utils import openroad_design, openroad_settings, register_openroad_flows
    from .tiny_platform import tiny_store

    design = openroad_design(tmp_path / "design")
    design.write_text(
        design.read_text() + "flows:\n  openroad:\n    ir_drop.instance_power: {u_core: 2 mW}\n"
    )
    local_root = tmp_path / "local" / "xeda_run"
    items = [*openroad_settings(tiny_store(tmp_path)), "ir_drop.instance_power.u_core.reg_1=1mW"]
    with register_openroad_flows():
        prepared = DefaultRunner(local_root, display_results=False).prepare_request(
            "openroad", design=design, flow_settings=items
        )
        _assert_tables_reach_the_remote(
            tmp_path,
            prepared,
            local_root,
            "openroad",
            "ir_drop",
            "instance_power",
            expected={"u_core": "0.002 W", "u_core.reg_1": "0.001 W"},
        )


def test_a_user_design_file_never_takes_a_target_whatever_its_name(tmp_path):
    from xeda.design import DesignValidationError

    for name in ("design.yaml", "d.xeda.json"):
        path = tmp_path / name
        path.write_text(
            json.dumps({"name": "d", "target": "arty", "rtl": {"sources": [], "top": "t"}})
        )
        with pytest.raises(DesignValidationError, match="not a key of a design"):
            Design.from_file(path)
    loaded = Design.from_file(path, packaged=True)
    assert loaded.target == "arty"


def test_only_a_packaged_document_takes_escaped_names_in_a_flow_table(tmp_path):
    from xeda.dataclass import AtomicName

    path = tmp_path / "d.xeda.json"
    document = {
        "name": "d",
        "rtl": {"sources": [], "top": "t"},
        "flows": {"vivado_synth": {"set_synth_properties": {"A": 1, "A\\.B": 2}}},
    }
    path.write_text(json.dumps(document))
    table = Design.from_file(path, packaged=True).flows["vivado_synth"]["set_synth_properties"]
    assert {type(key) for key in table} == {str, AtomicName}
    assert {
        key.name if isinstance(key, AtomicName) else key: item for key, item in table.items()
    } == {
        "A": 1,
        "A.B": 2,
    }
    # A user's design file gets no such key: the setting refuses the name when it is read.
    plain = Design.from_file(path).flows["vivado_synth"]["set_synth_properties"]
    assert not any(isinstance(key, AtomicName) for key in plain)


def test_a_packaged_document_names_its_target_by_the_rule_of_target_names(tmp_path):
    from xeda.design import DesignValidationError

    path = tmp_path / "d.xeda.json"
    path.write_text(json.dumps({"name": "d", "target": "../x", "rtl": {"sources": [], "top": "t"}}))
    with pytest.raises(DesignValidationError, match="not a target name"):
        Design.from_file(path, packaged=True)


def _unpacked(package: InputPackage, remote: Path) -> Design:
    """The design that the remote loads from the package, which it unpacks under `remote`."""
    import tarfile

    manifest = build_manifest(package.files, package.generated)
    archive = io.BytesIO()
    write_tar(manifest, package.files, package.generated, archive)
    archive.seek(0)
    with tarfile.open(fileobj=archive) as tar:
        tar.extractall(remote, filter="data")
    return Design.from_file(remote / package.design_file)


def test_the_package_names_every_source_s_type(tmp_path):
    """The remote reloads every type of source, including Data on unknown and HDL suffixes."""
    root = tmp_path / "d"
    root.mkdir()
    members = list(SourceType)
    sources = []
    for member in members:
        name = f"{member.name.lower()}.src"
        (root / name).write_text("x\n")
        sources.append({"file": name, "type": member.name})
    for name in ("notes.txt", "data.v"):
        (root / name).write_text("x\n")
        sources.append({"file": name, "type": "Data"})
        members.append(SourceType.Data)
    design = Design(
        name="d", design_root=root, rtl={"sources": sources, "top": "t"}, tb={"sources": sources}
    )
    package = _package(design, {}, tmp_path)
    shipped = _shipped(package)
    for part in ("rtl", "tb"):
        assert [source["type"] for source in shipped[part]["sources"]] == [m.name for m in members]
        assert all(set(source) <= {"file", "type"} for source in shipped[part]["sources"])
    restored = _unpacked(package, tmp_path / "remote")
    assert [[s.type for s in part.sources] for part in (restored.rtl, restored.tb)] == [members] * 2


EXAMPLES = sorted(
    p
    for p in (Path(__file__).parent.parent / "examples").rglob("*")
    if p.suffix in (".toml", ".yaml", ".yml")
    and not p.name.startswith("xedaproject.")
    and "xeda_run" not in p.parts
)


@pytest.mark.parametrize("design_file", EXAMPLES, ids=lambda p: p.name)
def test_every_example_design_unpacks_with_the_same_sources(design_file, tmp_path):
    """What the remote loads from the package has the sources of the example, each with its type
    and its content, in order, and the same settings of the design. (A source outside the design
    root has another place there, so the hash of the design may differ. The run identity does not:
    the host counts the place as this side does.)"""
    design = Design.from_file(design_file)
    restored = _unpacked(_package(design, {}, tmp_path), tmp_path / "remote")

    def sources(loaded: Design, part: str) -> list:
        return [
            (item["type"], item["content"])
            for item in getattr(loaded, f"{part}_fingerprint")["sources"]
        ]

    for part in ("rtl", "tb"):
        assert sources(restored, part) == sources(design, part)
    assert (restored.rtl.top, restored.rtl.defines) == (design.rtl.top, design.rtl.defines)


# ------------------------------------------------------------------ input bindings
#
# A binding (`flows.<flow>.inputs.<input>`) is wiring and not a setting: the planner takes it out
# of every origin before it merges the sections. The remote plans the packaged document alone, so
# the package has to carry the bindings that won, and the remote has to plan the edges that the
# local plan has.


def _wiring(plan, run_root: Path) -> list[tuple]:
    """The nodes of `plan` with their identities, places and edges: for each input, where it
    comes from and the ordered producer outputs. (The place where a binding was written is no
    part of the identity of a run, and the remote reads every binding from one file.)"""
    return [
        (
            node.name,
            node.flowrun_hash,
            node.run_path.relative_to(run_root).as_posix(),
            [
                (
                    declared.name,
                    declared.origin,
                    [(reference.node, reference.output) for reference in declared.references],
                )
                for declared in node.inputs
            ],
        )
        for node in plan.nodes
    ]


def _bound_root(tmp_path: Path) -> Path:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "input.txt").write_text("from the bound producer\n")
    return root


def _write_design(root: Path, flows: dict | None) -> Path:
    import yaml

    document = {"name": "d", "rtl": {"sources": [], "top": "t"}}
    if flows:
        document["flows"] = flows
    (root / "d.yaml").write_text(yaml.safe_dump(document))
    return root / "d.yaml"


def _write_project(root: Path, flows: dict, design_flows: dict | None) -> str:
    import yaml

    design = {"name": "d", "design_root": str(root), "rtl": {"sources": [], "top": "t"}}
    if design_flows:
        design["flows"] = design_flows
    path = root / "xedaproject.yaml"
    path.write_text(yaml.safe_dump({"flows": flows, "designs": [design]}))
    return str(path)


MADE_BY_INPUT_MAKER = {"made": "__input_maker.made"}
BOUND_PRODUCER = {"__input_maker": {"input_file": "input.txt"}}

#: name -> (flow, settings of the design file, settings of the project file, `-s` items,
#: nodes that the plan has, as `(node, input, producers of it)`).
BINDING_CASES = {
    "design-file": dict(
        flow="__taker",
        design={"__taker": {"inputs": MADE_BY_INPUT_MAKER}, **BOUND_PRODUCER},
        edges=[("__taker", "made", [("__input_maker", "made")])],
    ),
    "project-file": dict(
        flow="__taker",
        project={"__taker": {"inputs": MADE_BY_INPUT_MAKER}, **BOUND_PRODUCER},
        edges=[("__taker", "made", [("__input_maker", "made")])],
    ),
    "command-line": dict(
        flow="__taker",
        design=BOUND_PRODUCER,
        settings=["flows.__taker.inputs.made=__input_maker.made"],
        edges=[("__taker", "made", [("__input_maker", "made")])],
    ),
    "command-line-over-file": dict(
        flow="__taker",
        design={"__taker": {"inputs": {"made": "__maker.made"}}, **BOUND_PRODUCER},
        settings=["flows.__taker.inputs.made=__input_maker.made"],
        edges=[("__taker", "made", [("__input_maker", "made")])],
    ),
    "file-over-project": dict(
        flow="__taker",
        project={"__taker": {"inputs": {"made": "__maker.made"}}},
        design={"__taker": {"inputs": MADE_BY_INPUT_MAKER}, **BOUND_PRODUCER},
        edges=[("__taker", "made", [("__input_maker", "made")])],
    ),
    "chain-over-file": dict(
        flow="__maker+__taker",
        design={"__taker": {"inputs": MADE_BY_INPUT_MAKER}, **BOUND_PRODUCER},
        edges=[("__taker", "made", [("__maker", "made")])],
        shipped_inputs=False,
    ),
    "inputs-of-a-producer": dict(
        flow="__left",
        design={
            "__left": {"inputs": {"json_netlist": "__relay.netlist"}},
            "__relay": {"inputs": {"data": "__maker.made"}},
            "__fork": {"first": True},
        },
        edges=[
            ("__left", "json_netlist", [("__relay", "netlist")]),
            ("__relay", "data", [("__maker", "made")]),
        ],
    ),
    "two-producers-of-a-many-input": dict(
        flow="__join",
        design={"__join": {"inputs": {"more": ["__maker.made", "__input_maker.made"]}}}
        | BOUND_PRODUCER,
        edges=[("__join", "more", [("__maker", "made"), ("__input_maker", "made")])],
    ),
    "many-input-in-the-other-order": dict(
        flow="__join",
        design={"__join": {"inputs": {"more": ["__input_maker.made", "__maker.made"]}}}
        | BOUND_PRODUCER,
        edges=[("__join", "more", [("__input_maker", "made"), ("__maker", "made")])],
    ),
    "binding-of-a-flow-the-plan-leaves-out": dict(
        flow="__maker",
        design={"__taker": {"inputs": MADE_BY_INPUT_MAKER}},
        edges=[],
        shipped_inputs=True,
    ),
}


@pytest.mark.parametrize("hashed", [False, True])
@pytest.mark.parametrize("case", BINDING_CASES.values(), ids=list(BINDING_CASES))
def test_the_remote_plans_the_edges_of_the_local_plan(tmp_path, case, hashed):
    from xeda.flow_runner import DefaultRunner

    root = _bound_root(tmp_path)
    request: dict = {"design": _write_design(root, case.get("design"))}
    if case.get("project"):
        request = dict(
            design="d", xedaproject=_write_project(root, case["project"], case.get("design"))
        )
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=hashed)
    prepared = local.prepare_request(
        case["flow"], flow_settings=case.get("settings", []), **request
    )
    for node, name, producers in case["edges"]:
        declared = next(i for i in prepared.plan.node(node).inputs if i.name == name)
        assert [(r.node, r.output) for r in declared.references] == producers

    package, remote_plan, remote_root, _ = _remote_plan(
        tmp_path, prepared, local_root, case["flow"], hashed
    )
    assert _wiring(remote_plan, remote_root) == _wiring(prepared.plan, local_root)
    shipped = _shipped(package)["flows"]
    holding = {name for name, section in shipped.items() if "inputs" in section}
    if case.get("shipped_inputs") is False:
        assert not holding  # the chain binds the input, and the remote gets the chain
    else:
        assert holding and all(name not in ("__maker",) for name in holding)


# --- The identity of a node is the same on both ends -------------------------------------------


def _places_outside_the_root(tmp_path: Path) -> Design:
    """A design with the four paths that packaging moves or keeps by a rule of its own:

    - a source outside the design root, which travels among the flat sources,
    - a file outside the root that is a source and the value of a parameter,
    - a file outside the root that is only the value of a parameter,
    - a directory under the root that is the value of a parameter, as an absolute path."""
    root = tmp_path / "design"
    (root / "assets").mkdir(parents=True)
    (root / "top.v").write_text("module top(input clk); endmodule\n")
    (root / "assets" / "a.txt").write_text("a\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "ext.v").write_text("module ext(); endmodule\n")
    (tmp_path / "lib" / "shared.v").write_text("module shared(); endmodule\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rom.mem").write_text("00\n")
    return Design(
        name="d",
        design_root=root,
        rtl={
            "sources": [
                "top.v",
                str(tmp_path / "lib" / "ext.v"),
                str(tmp_path / "lib" / "shared.v"),
            ],
            "top": "top",
            "parameters": {
                "ROM": str(tmp_path / "data" / "rom.mem"),
                "SHARED": str(tmp_path / "lib" / "shared.v"),
                "ASSETS": str(root / "assets"),
                "W": 8,
            },
        },
    )


@pytest.mark.parametrize("hashed", [False, True])
def test_the_remote_has_the_identities_and_configurations_of_the_local_plan(tmp_path, hashed):
    """Packaging puts a source outside the design root among the flat sources and moves the files
    that parameters name. The host still computes the identities of the local plan, from the
    configuration as the client counted it."""
    from xeda.flow_runner import DefaultRunner
    from xeda.frozen import thaw

    design = _places_outside_the_root(tmp_path)
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=hashed)
    prepared = local.prepare_request("yosys", design=design)

    package, remote_plan, remote_root, _remote_dir = _remote_plan(
        tmp_path, prepared, local_root, "yosys", hashed
    )

    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    configuration = thaw(prepared.plan.node("yosys").configuration)
    assert thaw(remote_plan.node("yosys").configuration) == configuration
    # the client's spelling: places relative to the design root, `../` outside it
    assert [source["path"] for source in configuration["rtl"]["sources"]] == [
        "top.v",
        "../lib/ext.v",
        "../lib/shared.v",
    ]
    assert configuration["rtl"]["parameters"] == {
        "ROM": str(tmp_path / "data" / "rom.mem"),
        "SHARED": str(tmp_path / "lib" / "shared.v"),
        "ASSETS": "$DESIGN_ROOT/assets",
        "W": 8,
    }
    # the package moved the files: the host sees other places than the client does
    assert any(path.startswith("d/sources/") for path in package.files)


def test_a_converted_design_has_the_same_identities_on_the_remote(tmp_path):
    from xeda.flow_runner import DefaultRunner
    from xeda.frozen import thaw

    from .flow_registry import preserved_flow_registry
    from .test_node_configuration import bluespec_design, converted_chain

    with preserved_flow_registry():
        converter, consumer, chain = converted_chain()
        design = bluespec_design(tmp_path / "design")
        local_root = tmp_path / "local" / "xeda_run"
        local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=True)
        prepared = local.prepare_request(chain, design=design)

        _package_, remote_plan, remote_root, _remote_dir = _remote_plan(
            tmp_path, prepared, local_root, chain, True
        )

        assert [node.name for node in remote_plan.nodes] == [converter.name, consumer.name]
        assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
        for node in prepared.plan.nodes:
            assert thaw(remote_plan.node(node.name).configuration) == thaw(node.configuration)
        assert thaw(prepared.plan.node(consumer.name).configuration)["converted_from"] == (
            converter.name
        )


def test_a_file_that_is_a_source_and_a_parameter_has_a_text_for_each_role(tmp_path):
    design = _places_outside_the_root(tmp_path)
    package = _package(design, {}, tmp_path)

    shared = tmp_path / "lib" / "shared.v"
    (key,) = [
        key
        for key, roles in package.path_identities.items()
        if roles.get("source") == "../lib/shared.v"
    ]
    assert key.startswith(str(REMOTE_INPUTS / "d" / "sources"))
    assert package.path_identities[key] == {"source": "../lib/shared.v", "parameter": str(shared)}
    # a value that travels as it is is keyed by itself
    assets = str(design.root_path / "assets")
    assert package.path_identities[assets] == {"parameter": "$DESIGN_ROOT/assets"}


def test_two_texts_for_one_file_in_one_role_are_refused(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "rom.mem").write_text("00\n")
    design = _design(
        tmp_path,
        rtl={
            "sources": [],
            "top": "t",
            "parameters": {"A": str(lib / "rom.mem"), "B": str(lib / ".." / "lib" / "rom.mem")},
        },
    )

    with pytest.raises(FlowSettingsException, match="same role"):
        _package(design, {}, tmp_path)


def test_the_netlist_of_a_design_outside_its_root_has_the_same_identity_on_the_remote(tmp_path):
    """The origin of a declared input that the sources supply names the file by its place under
    the design root. Packaging moves a netlist from outside the root among the flat sources, and
    the host counts the place as the client does."""
    from xeda.flow_runner import DefaultRunner

    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "n.json").write_text("{}\n")
    root = tmp_path / "design"
    root.mkdir()
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": [{"file": str(tmp_path / "lib" / "n.json"), "type": "JsonNetlist"}],
            "top": "t",
        },
    )
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=True)
    prepared = local.prepare_request(
        "nextpnr", design=design, flow_settings=["fpga.part=LFE5U-25F-6BG381C"]
    )

    _package_, remote_plan, remote_root, _remote_dir = _remote_plan(
        tmp_path, prepared, local_root, "nextpnr", True
    )

    assert prepared.plan.node("nextpnr").origins[0] == (
        "netlist",
        ("source", (("../lib/n.json", "JsonNetlist"),)),
    )
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)


def test_a_testbench_with_cocotb_has_the_same_identity_on_the_remote(tmp_path):
    """The testbench part, its sources, its cocotb table and the defines (literal text, even when
    it looks like a path) are the same on both ends."""
    from xeda.flow_runner import DefaultRunner
    from xeda.frozen import thaw

    root = tmp_path / "design"
    root.mkdir()
    for name in ("top.v", "tb.v"):
        (root / name).write_text(f"module {name[:-2]}(); endmodule\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["top.v"],
            "top": "top",
            "clocks": ["clk"],
            "defines": {"DUMP": "/not/a/path/here", "FAST": 1},
        },
        tb={
            "sources": ["tb.v"],
            "top": "tb",
            "uut": "dut",
            "defines": {"TRACE": None},
            "cocotb": {"module": "test_top", "testcase": ["a", "b"]},
        },
    )
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=True)
    prepared = local.prepare_request("verilator", design=design)

    _package_, remote_plan, remote_root, _remote_dir = _remote_plan(
        tmp_path, prepared, local_root, "verilator", True
    )

    configuration = thaw(prepared.plan.node("verilator").configuration)
    assert configuration["tb"]["cocotb"]["testcase"] == ["a", "b"]
    assert configuration["rtl"]["defines"] == {"DUMP": "/not/a/path/here", "FAST": 1}
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    assert thaw(remote_plan.node("verilator").configuration) == configuration


# ---------------------------------------------- local and Git dependencies of one design


@pytest.mark.parametrize("order", ["local first", "git first"])
def test_the_host_merges_local_and_git_dependencies_as_the_client_does(
    tmp_path, remote, git_log, order
):
    """The package carries the entries in the order they are listed: a local one as the design
    document and the files it declares, a Git one as its reference. The host merges once, so its
    indexes, its source order and its identities are the client's."""
    from xeda.flow_runner import DefaultRunner

    from .test_git_dependencies import XILINX, _mixed

    local_root = tmp_path / "local" / "xeda_run"
    (tmp_path / "design").mkdir()
    design_file = _mixed(tmp_path / "design", remote, order)
    runner = DefaultRunner(local_root, display_results=False)
    prepared = runner.prepare_request("yosys_fpga", design=design_file, flow_settings=dict(XILINX))
    started = len(git_log())

    package, remote_plan, remote_root, remote_dir = _remote_plan(
        tmp_path, prepared, local_root, "yosys_fpga", False
    )

    assert len(git_log()) > started, "the host fetched the Git dependency itself"
    requested = prepared.plan.requested
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    assert remote_plan.node(requested).configuration == prepared.plan.node(requested).configuration
    shipped = _shipped(package)
    assert [bool(entry.get("repo_url")) for entry in shipped["dependencies"]] == [
        order == "git first",
        order == "local first",
    ]
