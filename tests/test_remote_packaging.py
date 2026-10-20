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
from xeda.remote.manifest import build_manifest, write_tar
from xeda.remote.packaging import InputPackage, package_inputs

from .io_flows import _ReadsInstalled, _ReadsInstalledNested  # noqa: F401  (registers the flows)
from .tiny_platform import TINY_OPENROAD_RESIDUE

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
    assert _shipped(local_only)["flow"][key] == expected(None)

    # both set: the host profile wins on the remote, the local value never travels
    with_host = _package(design, {key: local}, tmp_path, host_flows={key: host})
    assert _shipped(with_host)["flow"][key] == expected("/opt/pdk")
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
    assert _shipped(package)["flow"][STORE_KEY] == {"store": None}


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

    (shipped,) = _shipped(package)["flow"]["openroad_place"]["liberty"]
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
    assert package.path_identities[
        str(REMOTE_INPUTS / shipped.removeprefix("$DESIGN_ROOT/"))
    ] == str(library)


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
PROJECT = {
    "flows": {"vivado_synth": {"clock": {"period": "5 ns"}}, "nextpnr": {"seed": 9}},
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


@pytest.mark.parametrize("hashed", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        dict(flow="vivado_synth", target="arty", settings=[]),
        dict(flow="vivado_synth", target="arty", settings=["clock.period=8 ns"]),
        dict(flow="vivado_synth", target=None, settings=["fpga.part=xc7a100tcsg324-1"]),
        dict(flow="nextpnr", target="ulx3s", settings=["seed=4"]),
        dict(flow="nextpnr", target="ulx3s", settings=[], project=True),
        dict(flow="vivado_synth", target="arty", settings=["clock.period=8 ns"], project=True),
    ],
    ids=["target", "target-and-s", "s-only", "chain-target-s", "project-target", "project-s"],
)
def test_the_remote_plans_the_nodes_of_the_local_plan(tmp_path, case, hashed):
    from xeda.flow_runner import DefaultRunner

    directory = _targets_copy(tmp_path)
    local_root = tmp_path / "local" / "xeda_run"
    local = DefaultRunner(local_root, display_results=False, hashed_run_dirs=hashed)
    if case.get("project"):
        request = dict(design="knight", xedaproject=_project(directory))
    else:
        request = dict(design=directory / "knight.yaml")
    prepared = local.prepare_request(
        case["flow"], flow_settings=case["settings"], target=case["target"], **request
    )
    assert prepared.design.target == case["target"]

    package = package_inputs(
        prepared.design,
        prepared.sections,
        {node.name for node in prepared.plan.nodes},
        run_root=local_root,
        remote_inputs=REMOTE_INPUTS,
    )
    remote_dir = tmp_path / "remote_inputs"
    for place, source in package.files.items():
        (remote_dir / place).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, remote_dir / place)
    for place, content in package.generated.items():
        (remote_dir / place).parent.mkdir(parents=True, exist_ok=True)
        (remote_dir / place).write_bytes(content)

    remote_root = tmp_path / "remote" / "xeda_run"
    remote = DefaultRunner(remote_root, display_results=False, hashed_run_dirs=hashed)
    remote_plan = remote.plan(case["flow"], design=remote_dir / package.design_file)
    assert _nodes(remote_plan, remote_root) == _nodes(prepared.plan, local_root)
    assert [node.name for node in remote_plan.nodes][-1] == prepared.plan.nodes[-1].name
    if case["target"]:
        assert _shipped(package)["target"] == case["target"]
        assert all(
            case["target"] in node[2].split("/") for node in _nodes(remote_plan, remote_root)
        )
    else:
        assert "target" not in _shipped(package)


def test_a_user_design_file_still_cannot_write_a_target(tmp_path):
    from xeda.design import DesignValidationError

    path = tmp_path / "design.yaml"
    path.write_text("name: d\ntarget: arty\nrtl: {sources: [], top: t}\n")
    with pytest.raises(DesignValidationError, match="not a key of a design"):
        Design.from_file(path)


def test_a_packaged_document_names_its_target_by_the_rule_of_target_names(tmp_path):
    from xeda.design import DesignValidationError

    path = tmp_path / "d.xeda.json"
    path.write_text(json.dumps({"name": "d", "target": "../x", "rtl": {"sources": [], "top": "t"}}))
    with pytest.raises(DesignValidationError, match="not a target name"):
        Design.from_file(path)
