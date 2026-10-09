"""A design's hash is the content of its sources when it is asked for, not when it was first.

A `Design` that an application keeps and launches again (a notebook, a service, a search) must not
carry the hash of a file as it was at the first launch: the `design_hash` a launch records, and the
make-like check of the next one, would name content that is no longer there.
"""

import shutil
from pathlib import Path

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth

from .tool_utils import use_fake_tools

RESOURCES = Path(__file__).parent / "resources"


def _design(root: Path) -> Design:
    return Design(name="d", design_root=root, rtl={"sources": ["a.v"], "top": "a"})


def test_a_design_hash_follows_the_content_of_its_sources(tmp_path) -> None:
    source = tmp_path / "a.v"
    source.write_text("module a; endmodule\n")
    design = _design(tmp_path)
    before = design.rtl_hash
    assert design.rtl_hash == before  # asked again, the file unchanged
    source.write_text("module a; wire x; endmodule\n")
    assert design.rtl_hash != before
    assert design.rtl_hash == _design(tmp_path).rtl_hash


def test_a_launch_of_an_edited_design_records_the_hash_of_what_it_ran(
    tmp_path, monkeypatch
) -> None:
    use_fake_tools(monkeypatch)
    shutil.copytree(RESOURCES, tmp_path / "resources")
    work = tmp_path / "resources" / "design0"
    monkeypatch.chdir(work)
    settings = {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}
    root = tmp_path / "run"
    design = Design.from_file(work / "design0.toml")
    first = DefaultRunner(root, display_results=False).launch_flow(VivadoSynth, design, settings)
    source = design.rtl.sources[0].file
    source.write_text(source.read_text() + "\n-- edited\n")

    second = DefaultRunner(root, display_results=False).launch_flow(VivadoSynth, design, settings)
    fresh = Design.from_file(work / "design0.toml")
    assert second.design_hash != first.design_hash
    assert second.design_hash == fresh.parts_hash(VivadoSynth.design_parts)

    # nothing changed since: the next launch, of a design loaded again, finds the run fresh
    third = DefaultRunner(root, display_results=False).launch_flow(VivadoSynth, fresh, settings)
    assert third.reused, third.stale_reason
