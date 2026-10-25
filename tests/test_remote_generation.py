"""A remote launch generates on the host.

The host runs the whole launch, so the host runs the generator of the design, into a storage
under its own run root. The package carries the recipe and the files and directories that the
recipe declares in `sources`, and the declarations of the sources that the generator writes; a
generator that declares no `sources` cannot be sent and is refused before any connection.
"""

import json
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
