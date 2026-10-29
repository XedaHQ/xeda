"""A remote launch generates on the host.

The host runs the whole launch, so the host runs the generator of the design, into a storage
under its own run root. The package carries the recipe and the files and directories that the
recipe declares in `sources`, and the declarations of the sources that the generator writes; a
generator that declares no `sources` cannot be sent and is refused before any connection.
"""

import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from xeda import Design
from xeda.deliver import DeliveryError
from xeda.design import DesignValidationError
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dependencies import merged_design
from xeda.remote.packaging import package_inputs

from .generation_world import World
from .remote_fixtures import connected, fake_remote, fake_ssh, remote_host, remote_request
from .test_git_dependencies import git_log, remote
from .scoping import node_settings

FIXTURES = (fake_ssh, fake_remote, remote_host, git_log, remote)

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


def test_a_directory_that_the_recipe_names_travels_whole(tmp_path):
    world = _world(tmp_path, generator={"sources": ["spec.txt", "gen.py", "templates"]})
    (world.root / "templates").mkdir()
    (world.root / "templates" / "a.in").write_text("a\n")
    document, package = _document(tmp_path, world.load())
    assert document["rtl"]["generator"]["sources"] == ["spec.txt", "gen.py", "templates"]
    assert sorted(package.files) == sorted(["spec.txt", "gen.py", "templates/a.in"])


def test_a_generator_input_outside_the_design_root_cannot_be_sent(tmp_path, remote_host):
    world = _world(tmp_path, generator={"sources": ["spec.txt", "gen.py", "../lib/tool.py"]})
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "tool.py").write_text("# tool\n")
    with pytest.raises(DesignValidationError) as error:
        _document(tmp_path, world.load())
    text = str(error.value)
    assert str(tmp_path / "lib" / "tool.py") in text and "below the design root" in text
    # a request refuses it at planning: no connection, no generator
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    assert isinstance(outcome.error, DesignValidationError)
    assert world.runs == 0 and not connected(remote_host)
    # a local launch is not changed
    from xeda.flow_runner.launch_generation import generate

    world.run_root.mkdir()
    assert generate(world.load(), run_root=world.run_root.resolve(), environ=dict(os.environ))


def test_a_dependency_generator_input_outside_its_root_cannot_be_sent(tmp_path):
    chain = _chain(tmp_path, depth=1)
    path = tmp_path / "lib1" / "lib1.yaml"
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "tool.py").write_text("# tool\n")
    path.write_text(
        path.read_text().replace(
            "sources: [gen.py, spec.txt]", "sources: [gen.py, ../shared/tool.py]"
        )
    )
    with pytest.raises(DesignValidationError, match=r"lib1.*tool\.py.*below the design root"):
        _chain_document(tmp_path, chain)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
@pytest.mark.parametrize("where", ["inside", "outside", "in a declared directory"])
def test_a_generator_cwd_through_a_link_cannot_be_sent(tmp_path, where):
    cwd = "gen/sub" if where == "in a declared directory" else "link"
    world = _world(tmp_path, generator={"cwd": cwd, "sources": ["spec.txt", "gen.py", "gen"]})
    target = (tmp_path if where == "outside" else world.root) / "work"
    target.mkdir()
    (world.root / "gen").mkdir()
    (world.root / cwd).symlink_to(target, target_is_directory=True)
    with pytest.raises(DesignValidationError, match=rf"`cwd` '{cwd}', which a link leads to"):
        _document(tmp_path, world.load())
    assert world.runs == 0


def test_a_generator_without_sources_cannot_be_sent(tmp_path):
    world = _world(tmp_path, generator={"sources": []})
    with pytest.raises(DesignValidationError) as error:
        _document(tmp_path, world.load())
    text = str(error.value)
    assert "declares no `sources`" in text and "the generator of design 'generated'" in text
    assert "declare its `sources`" in text


def test_a_remote_request_for_such_a_generator_is_refused_before_connecting(tmp_path, remote_host):
    world = _world(tmp_path, generator={"sources": []})
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    assert isinstance(outcome.error, DesignValidationError)
    assert "declare its `sources`" in str(outcome.error)
    assert world.runs == 0 and not connected(remote_host)


def test_a_remote_request_refuses_the_copies_of_generated_sources_before_connecting(
    tmp_path, remote_host
):
    """The host would deliver them to the same path on the host, never into the local tree."""
    world = _world(tmp_path, generator={"outputs_to": "$DESIGN_ROOT/copies"})
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    assert isinstance(outcome.error, DeliveryError)
    assert "rtl.generator.outputs_to" in str(outcome.error) and "--remote" in str(outcome.error)
    assert world.runs == 0 and not connected(remote_host)
    assert not (world.root / "copies").exists()


def test_the_host_generates_into_its_own_run_root(tmp_path, remote_host):
    world = _world(tmp_path)
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
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
            tmp_path,
            world.design_file,
            "vivado_synth",
            flow_settings=node_settings("vivado_synth", SETTINGS),
            **overrides,
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
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    local = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        "vivado_synth", world.design_file, flow_settings=node_settings("vivado_synth", SETTINGS)
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
    design = merged_design(world.load(), run_root=None)
    assert [s.generated for s in design.rtl.sources] == [False, True]
    document, package = _document(tmp_path, world.load())
    # the design as it was given: its own source is a declaration, and the dependency is a
    # design document of its own with the file it declares
    assert document["rtl"]["sources"] == [{"path": "gen/top.v", "type": "Verilog"}]
    (entry,) = document["dependencies"]
    assert entry["uri"] == "_dependencies/0/dep.xeda.json"
    dependency = json.loads(package.generated[entry["uri"]])
    assert dependency["rtl"]["sources"] == [{"file": "lib.v", "type": "Verilog"}]
    assert "_dependencies/0/lib.v" in package.files


def test_a_written_source_of_a_dependency_travels_beside_a_generated_one(tmp_path, remote_host):
    world = _world_with_a_dependency(tmp_path)
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
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


def test_the_package_carries_each_dependency_with_its_recipe_and_inputs(tmp_path):
    chain = _chain(tmp_path)
    document, package = _chain_document(tmp_path, chain)
    assert "generation_units" not in document
    assert sorted(package.files) == sorted(
        [
            "top.v",
            *(
                f"{base}/{name}"
                for base in ("_dependencies/0", "_dependencies/1")
                for name in ("gen.py", "spec.txt")
            ),
        ]
    )
    (entry,) = document["dependencies"]
    lib1 = json.loads(package.generated[entry["uri"]])
    assert lib1["name"] == "lib1"
    assert lib1["rtl"]["generator"]["sources"] == ["gen.py", "spec.txt"]
    assert lib1["rtl"]["sources"] == [{"path": "gen/lib1.v", "type": "Verilog"}]
    (nested,) = lib1["dependencies"]
    lib2 = json.loads(package.generated[os.path.normpath(f"_dependencies/0/{nested['uri']}")])
    assert lib2["name"] == "lib2"
    assert lib2["rtl"]["generator"]["sources"] == ["gen.py", "spec.txt"]
    assert {"file": "top.v", "type": "Verilog"} in document["rtl"]["sources"]


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
    outcome = remote_request(
        tmp_path,
        chain.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert chain.ran == ["lib2", "lib1", "top"]
    host_root = remote_host.workspace() / "xeda_run" / ".generated"
    assert {p.name for p in host_root.iterdir() if p.is_dir()} == {"lib1", "lib2", "top"}
    assert not (tmp_path / "local" / "xeda_run" / ".generated").exists()
    # the second request reuses the storages of the host; `--rebuild-all` regenerates them all
    again = remote_request(
        tmp_path,
        chain.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    assert again.success and chain.ran == ["lib2", "lib1", "top"]
    forced = remote_request(
        tmp_path,
        chain.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
        rebuild_all=True,
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
    outcome = remote_request(
        tmp_path,
        chain.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    local = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        "vivado_synth", chain.design_file, flow_settings=node_settings("vivado_synth", SETTINGS)
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
    document, package = _document(tmp_path, Design.load(design_file))
    assert [e["uri"] for e in document["dependencies"]] == [
        "_dependencies/0/a.xeda.json",
        "_dependencies/0/b.xeda.json",
    ], "two designs in one directory share one root on the host"
    assert [json.loads(package.generated[e["uri"]])["name"] for e in document["dependencies"]] == [
        "a",
        "b",
    ]
    outcome = remote_request(
        tmp_path, design_file, "vivado_synth", flow_settings=node_settings("vivado_synth", SETTINGS)
    )
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
    design = merged_design(Design.load(design_file), run_root=None)
    document, package = _document(tmp_path, Design.load(design_file))
    assert "generation_units" not in document
    unit = json.loads(package.generated[document["dependencies"][0]["uri"]])
    assert unit["rtl"]["parameters"] == {"W": 1} and unit["rtl"]["defines"] == {"FAST": 1}
    assert unit["rtl"]["clocks"] == [{"port": "clk", "name": "clk"}]
    assert unit["rtl"]["sources"][0] == {"file": "extra.v", "type": "Verilog"}
    assert unit["language"]["vhdl"]["standard"] == "08"
    assert "_dependencies/0/extra.v" in package.files

    outcome = remote_request(
        tmp_path, design_file, "vivado_synth", flow_settings=node_settings("vivado_synth", SETTINGS)
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    host = remote_host.workspace() / "xeda_run" / ".generated" / "lib"
    on_host = {p.name for p in host.iterdir() if p.is_dir()}
    here = {generation_key(unit)[0] for _owner, unit in generating_units(design)}
    assert len(here) == 2, "two units that differ in a parameter have two keys"
    assert on_host == here, "the host computes the keys of this side"


@pytest.mark.parametrize("cwd", ["work", "$DESIGN_ROOT/work", "${DESIGN_DIR}/work", "absolute"])
def test_the_package_keeps_an_expanded_working_directory_relative_to_the_host_root(tmp_path, cwd):
    value = str(tmp_path / "design" / "work") if cwd == "absolute" else cwd
    world = _world(tmp_path, generator={"cwd": value})
    (world.root / "work").mkdir()
    document, package = _document(tmp_path, world.load())
    assert document["rtl"]["generator"]["cwd"] == "work"
    assert "work" in package.directories


def test_an_empty_working_directory_of_a_generator_exists_on_the_host(tmp_path, remote_host):
    world = _world(tmp_path, generator={"cwd": "work"})
    (world.root / "work").mkdir()
    document, package = _document(tmp_path, world.load())
    assert "work" in package.directories
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    assert world.runs == 1
    cwd, _design_root, _storage = world.where
    assert cwd.endswith("/work")


def test_the_host_plans_the_identity_of_a_design_that_merges_units_with_content(
    tmp_path, remote_host
):
    """The units carry parameters, defines, clocks and written sources, and the design merges
    them. The node on the host has the identity of the node in the plan of this side."""
    design_file = _rich_dependency(tmp_path)
    outcome = remote_request(
        tmp_path, design_file, "vivado_synth", flow_settings=node_settings("vivado_synth", SETTINGS)
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    local = DefaultRunner(tmp_path / "plan_only", display_results=False).plan(
        "vivado_synth", design_file, flow_settings=node_settings("vivado_synth", SETTINGS)
    )
    assert outcome.document["nodes"][-1]["record"]["identity"] == (
        local.node("vivado_synth").flowrun_hash
    )
    # a dry run on the host plans the same node
    dry = remote_request(
        tmp_path,
        design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
        dry_run=True,
    )
    if dry.error is not None:
        raise dry.error
    planned = dry.document["nodes"]
    assert [n["name"] for n in planned] == [n.name for n in local.nodes]


# ----------------------------------------- what is refused before a Git dependency is fetched


def _with_a_git_dependency_first(tmp_path: Path, remote, local: bool, sources: list[str]) -> World:
    """A design whose first dependency is a Git repository over `file://`. With `local`, its
    second dependency is a local design whose generator declares `sources`. Without it, the
    generator of the design declares them."""
    git = f"git+{remote.url}?commit={remote.first}#dep.yaml"
    extra = f"dependencies:\n  - {git}\n" + ("  - dep/dep.yaml\n" if local else "")
    world = _world(tmp_path, extra=extra, **({} if local else {"generator": {"sources": sources}}))
    # the file that `../lib/tool.py` names, from the root of the design and from that of `dep`
    for library in (tmp_path / "lib", world.root / "lib"):
        library.mkdir()
        (library / "tool.py").write_text("# tool\n")
    if local:
        (world.root / "dep").mkdir()
        (world.root / "dep" / "dep.yaml").write_text(
            "name: dep\nrtl:\n  sources: []\n  generator:\n"
            f"    executable: {sys.executable!r}\n    args: ['-c', 'pass']\n"
            f"    sources: {json.dumps(sources)}\n"
        )
    return world


@pytest.mark.parametrize("local", [False, True], ids=["own generator", "local dependency"])
@pytest.mark.parametrize(
    "sources, said",
    [([], "declares no `sources`"), (["../lib/tool.py"], "below the design root")],
    ids=["no sources", "input outside the root"],
)
def test_a_generator_that_cannot_be_sent_is_refused_before_any_git_process(
    tmp_path, remote_host, remote, git_log, local, sources, said
):
    """The refusal needs no value that a Git dependency supplies, so nothing is resolved or
    fetched first. The request starts no generator and makes no connection either."""
    world = _with_a_git_dependency_first(tmp_path, remote, local, sources)
    started = len(git_log())
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    assert isinstance(outcome.error, DesignValidationError)
    assert said in str(outcome.error)
    assert len(git_log()) == started, "a Git command was started"
    assert world.runs == 0 and not connected(remote_host)


# ------------------------------------------- the host fetches the commit that the client resolved


def test_the_host_fetches_the_commit_the_client_resolved(
    tmp_path, remote_host, remote, git_log, monkeypatch
):
    """A branch that moves on the server between the plan of the client and the plan of the host
    changes nothing: the package holds the commit of the client, and the host fetches that one."""
    import xeda.flow_runner.dependencies as dependencies
    from xeda.flow_runner.dependencies import dependency_key

    git = f"git+{remote.url}?branch=main#dep.yaml"
    world = _world(tmp_path, extra=f"dependencies:\n  - {git}\n")
    first, fetched = remote.first, dependencies.fetch
    moved: list[str] = []

    def fetch_then_move_the_branch(*args, **kwargs):
        storage = fetched(*args, **kwargs)
        if not moved:  # the fetch of the client: the branch moves once it has been planned
            moved.append(remote.advance())
        return storage

    monkeypatch.setattr(dependencies, "fetch", fetch_then_move_the_branch)
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    (second,) = moved
    assert second != first
    stored = {p.name for p in (remote_host.workspace() / "xeda_run" / ".dependencies").iterdir()}
    assert dependency_key(remote.url, first) in stored
    assert dependency_key(remote.url, second) not in stored, "the host fetched the moved branch"
    # the provenance is the same on both sides: the branch as written, and the commit it ran with
    (record,) = outcome.document["nodes"][-1]["record"]["dependencies"]
    assert (record["reference"], record["commit"]) == ("main", first)


def test_the_host_fetches_the_commit_of_a_dependency_of_a_git_dependency(
    tmp_path, remote_host, remote, git_log, monkeypatch
):
    """The design lists repository A by commit, and A lists repository B by branch. The branch of
    B moves after the client resolved it, and the host still fetches the commit of the client."""
    import yaml

    import xeda.flow_runner.dependencies as dependencies
    from xeda.flow_runner.dependencies import dependency_key

    from .test_git_dependencies import Remote

    (tmp_path / "other").mkdir()
    outer = Remote(tmp_path / "other")
    spec = yaml.safe_load((outer.work / "dep.yaml").read_text())
    spec["dependencies"] = [{"uri": f"git+{remote.url}?branch=main#dep.yaml"}]
    (outer.work / "dep.yaml").write_text(yaml.safe_dump(spec))
    outer_commit = outer.commit("lists the other repository")
    subprocess.run(["git", "push", "--quiet", "origin", "HEAD:main"], cwd=outer.work, check=True)
    world = _world(
        tmp_path, extra=f"dependencies:\n  - git+{outer.url}?commit={outer_commit}#dep.yaml\n"
    )
    first, fetched, moved = remote.first, dependencies.fetch, []

    def fetch_then_move_the_branch(ref, *args, **kwargs):
        storage = fetched(ref, *args, **kwargs)
        if ref.repo_url == remote.url and not moved:  # the client has fetched the nested one
            moved.append(remote.advance())
        return storage

    monkeypatch.setattr(dependencies, "fetch", fetch_then_move_the_branch)
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    (second,) = moved
    stored = {p.name for p in (remote_host.workspace() / "xeda_run" / ".dependencies").iterdir()}
    assert dependency_key(remote.url, first) in stored
    assert dependency_key(remote.url, second) not in stored, "the host fetched the moved branch"


def test_two_entries_of_one_repository_get_each_its_own_commit_on_the_host(
    tmp_path, remote_host, remote, git_log, monkeypatch
):
    """One entry pins commit A, the other has no reference and so names the `HEAD`. The `HEAD`
    moves after the client resolved it. The host fetches A for the first entry and the commit of
    the client for the second, though both name one repository and one design file."""
    import xeda.flow_runner.dependencies as dependencies
    from xeda.flow_runner.dependencies import dependency_key

    pinned = remote.first
    head = remote.advance()
    git = f"git+{remote.url}"
    world = _world(
        tmp_path,
        extra=f"dependencies:\n  - {git}?commit={pinned}#dep.yaml\n  - {git}#dep.yaml\n",
    )
    fetched, moved = dependencies.fetch, []

    def fetch_then_move_the_head(ref, commit, *args, **kwargs):
        storage = fetched(ref, commit, *args, **kwargs)
        if commit == head and not moved:  # the client has fetched the commit of the `HEAD`
            moved.append(remote.advance())
        return storage

    monkeypatch.setattr(dependencies, "fetch", fetch_then_move_the_head)
    outcome = remote_request(
        tmp_path,
        world.design_file,
        "vivado_synth",
        flow_settings=node_settings("vivado_synth", SETTINGS),
    )
    if outcome.error is not None:
        raise outcome.error
    assert outcome.success, outcome.document
    (later,) = moved
    stored = {p.name for p in (remote_host.workspace() / "xeda_run" / ".dependencies").iterdir()}
    assert dependency_key(remote.url, pinned) in stored
    assert dependency_key(remote.url, head) in stored
    assert dependency_key(remote.url, later) not in stored, "the host fetched the moved HEAD"
    rows = outcome.document["nodes"][-1]["record"]["dependencies"]
    assert [row["commit"] for row in rows] == [pinned, head]
