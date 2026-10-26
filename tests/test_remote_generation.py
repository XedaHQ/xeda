"""A remote launch generates on the host.

The host runs the whole launch, so the host runs the generator of the design, into a storage
under its own run root. The package carries the recipe and the files and directories that the
recipe declares in `sources`, and the declarations of the sources that the generator writes; a
generator that declares no `sources` cannot be sent and is refused before any connection.
"""

import json
import sys
import os
from pathlib import Path, PurePosixPath

import pytest

from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow_runner import DefaultRunner
from xeda.remote.packaging import package_inputs

from .generation_world import World
from .remote_fixtures import connected, fake_remote, fake_ssh, remote_host, remote_request

FIXTURES = (fake_ssh, fake_remote, remote_host)

pytestmark = [pytest.mark.skipif(os.name != "posix", reason="POSIX")]

SETTINGS = ["fpga.part=xc7a12tcsg325-1", "clocks.main_clock.period=5ns"]


def _world(tmp_path: Path, **options) -> World:
    world = World(tmp_path, **options)
    text = world.design_file.read_text().replace(
        "  top: top\n", "  top: top\n  clocks: [{port: clk}]\n", 1
    )
    world.design_file.write_text(text)
    return world


def _document(tmp_path: Path, design: Design) -> dict:
    package = package_inputs(
        design,
        {},
        {"vivado_synth"},
        run_root=tmp_path / "run_root",
        remote_inputs=PurePosixPath(tmp_path / "remote_inputs"),
    )
    return json.loads(package.generated[package.design_file]), package


def test_the_package_carries_the_recipe_the_declared_inputs_and_the_declarations(tmp_path):
    world = _world(tmp_path)
    (world.root / "unrelated.txt").write_text("not declared\n")
    document, package = _document(tmp_path, world.load())
    rtl = document["rtl"]
    assert rtl["sources"] == [{"path": "gen/top.v", "type": "Verilog"}]
    recipe = rtl["generator"]
    assert recipe["sources"] == ["spec.txt", "gen.py"]
    assert recipe["args"][0] == "$DESIGN_ROOT/gen.py"
    assert sorted(package.files) == ["gen.py", "spec.txt"]
    assert "gen/top.v" not in package.files and "unrelated.txt" not in package.files


def test_a_directory_and_a_file_outside_the_design_that_the_recipe_names_travel(tmp_path):
    world = _world(
        tmp_path, generator={"sources": ["spec.txt", "gen.py", "templates", "../lib/tool.py"]}
    )
    (world.root / "templates").mkdir()
    (world.root / "templates" / "a.in").write_text("a\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "tool.py").write_text("# tool\n")
    document, package = _document(tmp_path, world.load())
    names = document["rtl"]["generator"]["sources"]
    assert names[:3] == ["spec.txt", "gen.py", "templates"]
    outside = names[3]
    assert outside.startswith("_generator_inputs/") and outside.endswith("/tool.py")
    assert sorted(package.files) == sorted(["spec.txt", "gen.py", "templates/a.in", outside])
    assert package.files[outside].read_text() == "# tool\n"


def test_a_generator_without_sources_cannot_be_sent(tmp_path):
    world = _world(tmp_path, generator={"sources": []})
    with pytest.raises(DesignValidationError) as error:
        _document(tmp_path, world.load())
    text = str(error.value)
    assert "declares no `sources`" in text and "the generator of design 'generated'" in text
    assert "declare its `sources`" in text


def test_a_remote_request_for_such_a_generator_is_refused_before_connecting(tmp_path, remote_host):
    world = _world(tmp_path, generator={"sources": []})
    outcome = remote_request(tmp_path, world.design_file, "vivado_synth", flow_settings=SETTINGS)
    assert isinstance(outcome.error, DesignValidationError)
    assert "declare its `sources`" in str(outcome.error)
    assert world.runs == 0 and not connected(remote_host)


def test_the_host_generates_into_its_own_run_root(tmp_path, remote_host):
    world = _world(tmp_path)
    outcome = remote_request(tmp_path, world.design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert world.runs == 1
    host_root = remote_host.workspace() / "xeda_run"
    (storage,) = [p for p in (host_root / ".generated" / "generated").glob("*") if p.is_dir()]
    assert (storage / "gen" / "top.v").read_text() == "// one\n// W=8\n"
    assert not (tmp_path / "local" / "xeda_run" / ".generated").exists()
    assert not (world.root / "gen").exists(), "the design tree is not written"


def test_the_host_reuses_its_storage_and_rebuild_all_forces_the_generator(tmp_path, remote_host):
    world = _world(tmp_path)

    def run(**overrides) -> None:
        outcome = remote_request(
            tmp_path, world.design_file, "vivado_synth", flow_settings=SETTINGS, **overrides
        )
        if outcome.error is not None:
            raise outcome.error
        assert outcome.success, outcome.document

    run()
    assert world.runs == 1
    run()
    assert world.runs == 1, "an ordinary request reuses the storage of the host"
    run(rebuild_all=True)
    assert world.runs == 2


def test_the_remote_node_has_the_identity_of_the_local_plan(tmp_path, remote_host):
    world = _world(tmp_path)
    outcome = remote_request(tmp_path, world.design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    local = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        "vivado_synth", world.design_file, flow_settings=SETTINGS
    )
    assert outcome.document["nodes"][-1]["record"]["identity"] == (
        local.node("vivado_synth").flowrun_hash
    )
    assert world.runs == 1, "the plan on this side generated nothing"


def _world_with_a_dependency(tmp_path: Path) -> World:
    """A design whose own sources are generated (`generated_sources` is empty) and whose local
    dependency brings a written source."""
    world = _world(tmp_path, extra="dependencies:\n  - dep/dep.yaml\n")
    (world.root / "dep").mkdir()
    (world.root / "dep" / "lib.v").write_text("module lib; endmodule\n")
    (world.root / "dep" / "dep.yaml").write_text(
        "name: dep\nrtl:\n  sources: [lib.v]\n  top: lib\n"
    )
    return world


def test_the_package_says_what_is_written_and_what_is_generated(tmp_path):
    world = _world_with_a_dependency(tmp_path)
    design = world.load()
    assert [s.generated for s in design.rtl.sources] == [False, True]
    document, package = _document(tmp_path, design)
    assert document["rtl"]["sources"] == [
        {"file": "dep/lib.v", "type": "Verilog"},
        {"path": "gen/top.v", "type": "Verilog"},
    ]
    assert "dep/lib.v" in package.files


def test_a_written_source_of_a_dependency_travels_beside_a_generated_one(tmp_path, remote_host):
    world = _world_with_a_dependency(tmp_path)
    outcome = remote_request(tmp_path, world.design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert world.runs == 1


# ---------------------------------------------------------------- generators of dependencies


def _chain(tmp_path: Path, depth: int = 2, own: bool = False):
    from .test_dependency_generation import Chain

    chain = Chain(tmp_path, depth=depth, own=own)
    return chain


def _chain_document(tmp_path: Path, chain) -> tuple[dict, object]:
    return _document(tmp_path, chain.load())


def test_the_package_carries_each_unit_with_its_recipe_and_inputs(tmp_path):
    chain = _chain(tmp_path)
    document, package = _chain_document(tmp_path, chain)
    units = document["generation_units"]
    assert [u["name"] for u in units] == ["lib2", "lib1"]
    assert [u["design_root"] for u in units] == ["_units/0", "_units/1"]
    assert all(u["rtl"]["generator"]["sources"] == ["gen.py", "spec.txt"] for u in units)
    assert sorted(package.files) == sorted(
        ["top.v", *(f"_units/{i}/{name}" for i in (0, 1) for name in ("gen.py", "spec.txt"))]
    )
    # the design lists the generated sources of its dependencies, each with its unit
    sources = document["rtl"]["sources"]
    assert {(e["path"], e.get("generated_by")) for e in sources if "path" in e} == {
        ("gen/lib1.v", 1),
        ("gen/lib2.v", 0),
    }
    assert {"file": "top.v", "type": "Verilog"} in sources
    assert units[1]["rtl"]["sources"] == [
        {"path": "gen/lib2.v", "type": "Verilog", "generated_by": 0},
        {"path": "gen/lib1.v", "type": "Verilog"},
    ]
    assert units[0]["rtl"]["sources"] == [{"path": "gen/lib2.v", "type": "Verilog"}]


def test_a_dependency_generator_without_sources_cannot_be_sent(tmp_path):
    chain = _chain(tmp_path, depth=1)
    path = tmp_path / "lib1" / "lib1.yaml"
    path.write_text(path.read_text().replace("    sources: [gen.py, spec.txt]\n", ""))
    with pytest.raises(DesignValidationError, match="lib1.*declares no `sources`"):
        _chain_document(tmp_path, chain)


def test_the_host_generates_the_generators_of_dependencies_in_order(tmp_path, remote_host):
    chain = _chain(tmp_path, depth=2, own=True)
    text = chain.design_file.read_text().replace(
        "  top: top\n", "  top: top\n  clocks: [{port: clk}]\n", 1
    )
    chain.design_file.write_text(text)
    outcome = remote_request(tmp_path, chain.design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert chain.ran == ["lib2", "lib1", "top"]
    host_root = remote_host.workspace() / "xeda_run" / ".generated"
    assert {p.name for p in host_root.iterdir() if p.is_dir()} == {"lib1", "lib2", "top"}
    assert not (tmp_path / "local" / "xeda_run" / ".generated").exists()
    # the second request reuses the storages of the host; `--rebuild-all` regenerates them all
    again = remote_request(tmp_path, chain.design_file, "vivado_synth", flow_settings=SETTINGS)
    assert again.success and chain.ran == ["lib2", "lib1", "top"]
    forced = remote_request(
        tmp_path, chain.design_file, "vivado_synth", flow_settings=SETTINGS, rebuild_all=True
    )
    assert forced.success and chain.ran == ["lib2", "lib1", "top"] * 2


def test_the_remote_node_of_a_design_with_dependency_generators_has_the_local_identity(
    tmp_path, remote_host
):
    chain = _chain(tmp_path, depth=1)
    text = chain.design_file.read_text().replace(
        "  top: top\n", "  top: top\n  clocks: [{port: clk}]\n", 1
    )
    chain.design_file.write_text(text)
    outcome = remote_request(tmp_path, chain.design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    local = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        "vivado_synth", chain.design_file, flow_settings=SETTINGS
    )
    assert (
        outcome.document["nodes"][-1]["record"]["identity"]
        == local.node("vivado_synth").flowrun_hash
    )


def test_two_dependency_designs_in_one_directory_are_two_units_on_the_host(tmp_path, remote_host):
    from .test_dependency_generation import two_designs_in_one_directory

    log, design_file = two_designs_in_one_directory(tmp_path)
    text = design_file.read_text().replace(
        "  top: top\n", "  top: top\n  clocks: [{port: clk}]\n", 1
    )
    design_file.write_text(text)
    document, package = _document(tmp_path, Design.from_file(design_file))
    assert [u["name"] for u in document["generation_units"]] == ["a", "b"]
    outcome = remote_request(tmp_path, design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert sorted(log.read_text().split()) == ["a", "b"]
    host_root = remote_host.workspace() / "xeda_run" / ".generated"
    assert {p.name for p in host_root.iterdir() if p.is_dir()} == {"a", "b"}


def _rich_dependency(tmp_path: Path) -> Path:
    """A design that merges two dependencies of one name that differ in a parameter and in the
    name of the module they generate. Each has a written source, a define, a clock and a
    testbench top beside its generator."""
    from .test_dependency_generation import SCRIPT

    library = tmp_path / "lib"
    library.mkdir()
    (library / "gen.py").write_text(SCRIPT)
    (library / "spec.txt").write_text("one\n")
    (library / "extra.v").write_text("module extra; endmodule\n")
    for name, width in (("a", 1), ("b", 2)):
        (library / f"{name}.yaml").write_text(
            "name: lib\n"
            "rtl:\n"
            f"  sources: [extra.v, gen/{name}.v]\n"
            f"  top: {name}\n"
            f"  parameters: {{W: {width}}}\n"
            "  defines: {FAST: 1}\n"
            "  clocks: [{port: clk}]\n"
            "  generator:\n"
            f"    executable: {sys.executable!r}\n"
            f"    args: ['$DESIGN_ROOT/gen.py', {name!r}, {str(tmp_path / 'order.log')!r}]\n"
            "    sources: [gen.py, spec.txt]\n"
            "    generated_sources: [gen]\n"
            "language:\n  vhdl:\n    standard: '08'\n"
        )
    top = tmp_path / "top"
    top.mkdir()
    (top / "top.v").write_text("module top; endmodule\n")
    (top / "top.yaml").write_text(
        "name: top\ndependencies: [../lib/a.yaml, ../lib/b.yaml]\nrtl:\n  sources: [top.v]\n"
        "  top: top\n  clocks: [{port: clk}]\n"
    )
    return top / "top.yaml"


def test_a_unit_carries_its_whole_configuration_to_the_host(tmp_path, remote_host):
    from xeda.flow_runner.launch_generation import generating_units, generation_key

    design_file = _rich_dependency(tmp_path)
    design = Design.from_file(design_file)
    document, package = _document(tmp_path, design)
    unit = document["generation_units"][0]
    assert unit["rtl"]["parameters"] == {"W": 1} and unit["rtl"]["defines"] == {"FAST": 1}
    assert unit["rtl"]["clocks"] == [{"port": "clk", "name": "clk"}]
    assert unit["rtl"]["sources"][0] == {"file": "extra.v", "type": "Verilog"}
    assert unit["language"]["vhdl"]["standard"] == "08"
    assert "_units/0/extra.v" in package.files

    outcome = remote_request(tmp_path, design_file, "vivado_synth", flow_settings=SETTINGS)
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    host = remote_host.workspace() / "xeda_run" / ".generated" / "lib"
    on_host = {p.name for p in host.iterdir() if p.is_dir()}
    here = {generation_key(unit)[0] for _owner, unit in generating_units(design)}
    assert len(here) == 2, "two units that differ in a parameter have two keys"
    assert on_host == here, "the host computes the keys of this side"
