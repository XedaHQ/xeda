"""`rtl.generator.outputs_to` names a directory that receives a copy of the generated sources.

The key is a location: absolute once `$DESIGN_ROOT`, `$DESIGN_DIR` or `$PWD` expands, as a
deliverable setting given as a location is. A relative value is refused when the design loads. The
destination is no part of any identity: it is not in the key of the storage, and not in the
identity of a node.
"""

import sys
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError, generator_of, loading_in_design_root
from xeda.flow_runner.launch_generation import generation_key

from .flow_registry import preserved_flow_registry
from .generation_world import World, reader_flow

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no storage locks")


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


def _design(root: Path, outputs_to) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "gen.py").write_text("")
    return Design(
        name="d",
        design_root=root,
        rtl={
            "sources": ["gen/top.v"],
            "top": "top",
            "generator": {
                "executable": sys.executable,
                "args": ["gen.py"],
                "sources": ["gen.py"],
                "outputs_to": outputs_to,
            },
        },
    )


@pytest.mark.parametrize("value", ["gen", "../gen", "./gen", "a/b"])
def test_a_relative_outputs_to_is_refused_with_the_fix(tmp_path, value):
    with pytest.raises(DesignValidationError) as error:
        _design(tmp_path, value)
    message = str(error.value)
    assert "outputs_to" in message and f"$DESIGN_ROOT/{value}" in message


def test_a_home_directory_shorthand_is_refused(tmp_path):
    with pytest.raises(DesignValidationError, match="outputs_to"):
        _design(tmp_path, "~/gen")


@pytest.mark.parametrize("form", ["$DESIGN_ROOT/gen", "${DESIGN_ROOT}/gen", "$DESIGN_DIR/gen"])
def test_the_design_root_variables_expand_to_the_root_of_the_design(tmp_path, form):
    design = _design(tmp_path, form)
    generator = generator_of(design.rtl, root=design.root_path)
    assert generator is not None and generator.outputs_to == design.root_path / "gen"


def test_an_absolute_outputs_to_is_accepted(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    design = _design(tmp_path / "design", str(elsewhere))
    generator = generator_of(design.rtl, root=design.root_path)
    assert generator is not None and generator.outputs_to == elsewhere


def test_a_location_below_the_start_directory_is_accepted_and_stays_to_expand(tmp_path):
    """No start directory exists when a design loads: the launch expands `$PWD`."""
    design = _design(tmp_path, "$PWD/out")
    generator = generator_of(design.rtl, root=design.root_path)
    assert generator is not None and str(generator.outputs_to) == "$PWD/out"


def test_an_outputs_to_is_idempotent_through_a_dump(tmp_path):
    design = _design(tmp_path, "$DESIGN_ROOT/gen")
    generator = generator_of(design.rtl, root=design.root_path)
    assert generator is not None
    with loading_in_design_root(design.root_path):
        again = type(generator)(**generator.model_dump())
    assert again.outputs_to == generator.outputs_to


def test_outputs_to_changes_no_identity_of_the_storage(tmp_path):
    reference = generation_key(World(tmp_path, directory="plain").load())[0]
    for index, value in enumerate(["$DESIGN_ROOT/one", "$DESIGN_ROOT/two", "/some/where"]):
        world = World(tmp_path, directory="plain", generator={"outputs_to": value})
        assert generation_key(world.load())[0] == reference, value


def test_outputs_to_changes_no_identity_of_a_node(tmp_path):
    flow = reader_flow()
    counter = tmp_path / "runs.log"
    plain = World(tmp_path, directory="plain", counter=counter)
    delivering = World(
        tmp_path,
        directory="delivering",
        counter=counter,
        generator={"outputs_to": "$DESIGN_ROOT/out"},
    )
    hashes = [
        {n.name: n.flowrun_hash for n in world.runner().plan(flow, world.load()).nodes}
        for world in (plain, delivering)
    ]
    assert hashes[0] == hashes[1]
