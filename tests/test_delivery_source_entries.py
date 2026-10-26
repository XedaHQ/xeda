"""A delivery never goes where a source entry of the design would read it.

An entry of `rtl.sources`, of `tb.sources` or of the `sources` of the generator names files by a
name, a pattern (`*` is the only pattern character) or a directory. A file delivered to a place
that an entry reads would become an input of the next run, so the launch refuses the destination
before any tool runs, naming the entry and its part. The rule judges names only: it reads no file,
so it holds for a destination that does not exist yet. The same rule applies to every delivery.
"""

import sys
from pathlib import Path
from typing import ClassVar, Optional

import pytest

from xeda import Design
from xeda.dataclass import Field, deliverable
from xeda.deliver import DeliveryError
from xeda.design import SourceType, source_entry_matches
from xeda.flow import Flow, MetricSpec, Out
from xeda.flow_runner import DefaultRunner

from .flow_registry import preserved_flow_registry

RUNS: list[str] = []


@pytest.fixture
def deliverer():
    """A flow that writes the netlist its setting names, and counts its runs."""
    with preserved_flow_registry():

        class _EntryDeliverer(Flow):
            """Writes the netlist its setting names."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Settings(Flow.Settings):
                netlist: Optional[Path] = Field(
                    None,
                    description="The netlist it writes.",
                    json_schema_extra=deliverable("outputs/{design}.v"),
                )

            class Outputs(Flow.Outputs):
                netlist: Path = Out(SourceType.Data, description="The netlist it writes.")

            def run(self) -> None:
                RUNS.append(self.name)
                assert self.settings.netlist is not None
                netlist = Path(self.settings.netlist)
                self.run_file(netlist).parent.mkdir(parents=True, exist_ok=True)
                self.run_file(netlist).write_text("net\n")
                self.artifacts["netlist"] = str(netlist)
                self.outputs.netlist = netlist

        RUNS.clear()
        yield _EntryDeliverer


def _tree(root: Path) -> None:
    for name in ("src/a.v", "tb/t.vhd", "scripts/gen.py", "top.v"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("// file\n")


def _design(root: Path, **parts) -> Design:
    _tree(root)
    rtl = {"sources": ["top.v"], "top": "top", **parts.pop("rtl", {})}
    return Design(name="d", design_root=root, rtl=rtl, **parts)


def _generator(*sources: str) -> dict:
    return {"executable": sys.executable, "args": ["scripts/gen.py"], "sources": list(sources)}


def _launch(tmp_path, flow, design, destination, **runner):
    return DefaultRunner(tmp_path / "xeda_run", display_results=False, **runner).launch_flow(
        flow, design, {"netlist": str(destination)}
    )


# ---------------------------------------------------------------- the rule on names


@pytest.mark.parametrize(
    "entry, candidate, expected",
    [
        ("src/*.v", "src/new.v", True),
        ("src/*.v", "src/gen/new.v", False),
        ("src/*.v", "src/new.sv", False),
        ("src/*.v", "other/new.v", False),
        ("src/*.v", "src/.hidden.v", False),
        ("src/.*.v", "src/.hidden.v", True),
        ("src/a*c.v", "src/abc.v", True),
        ("src/a*c.v", "src/ab.v", False),
        ("*/new.v", "src/new.v", True),
        ("*/new.v", "src/deeper/new.v", False),
        ("src/new.v", "src/new.v", True),
        ("src/new.v", "src/other.v", False),
        ("$DESIGN_ROOT/src/*.v", "src/new.v", True),
        ("${DESIGN_DIR}/src/new.v", "src/new.v", True),
        ("src", "src/new.v", True),
        ("src", "src/deeper/new.v", True),
        ("src", "srcs/new.v", False),
        ("src/fifo[1].v", "src/fifo[1].v", True),
        ("src/fifo[1].v", "src/fifo1.v", False),
        ("src/f?.v", "src/fa.v", False),
        ("src/f?.v", "src/f?.v", True),
    ],
)
def test_an_entry_matches_part_by_part(tmp_path, entry, candidate, expected):
    root = tmp_path / "design"
    assert source_entry_matches(entry, root, root / candidate) is expected


def test_the_value_of_a_variable_is_a_place_and_not_a_pattern(tmp_path):
    root = tmp_path / "proj[1]"
    assert source_entry_matches("$DESIGN_ROOT/*.v", root, root / "a.v")
    assert not source_entry_matches("$DESIGN_ROOT/*.v", root, tmp_path / "proj1" / "a.v")


def test_an_absolute_entry_is_judged_where_it_lies(tmp_path):
    outside = tmp_path / "lib"
    assert source_entry_matches(f"{outside}/*.v", tmp_path / "design", outside / "x.v")
    assert not source_entry_matches(f"{outside}/*.v", tmp_path / "design", tmp_path / "x.v")


def test_the_rule_reads_no_file_system(tmp_path):
    assert source_entry_matches(
        "nowhere/*.v", tmp_path / "missing", tmp_path / "missing/nowhere/a.v"
    )


# ---------------------------------------------------------------- the rule in a launch


@pytest.mark.parametrize(
    "entry, parts, destination, where",
    [
        ("src/*.v", {"rtl": {"sources": ["src/*.v"]}}, "src/new.v", "rtl"),
        ("tb/*.vhd", {"tb": {"sources": ["tb/*.vhd"]}}, "tb/new.vhd", "tb"),
        ("later.v", {"rtl": {"sources": ["top.v", {"path": "later.v"}]}}, "later.v", "rtl"),
        (
            "scripts/*.py",
            {"rtl": {"generator": _generator("scripts/*.py")}},
            "scripts/new.py",
            "generator",
        ),
        (
            "scripts",
            {"rtl": {"generator": _generator("scripts")}},
            "scripts/deeper/new.py",
            "generator",
        ),
    ],
    ids=["rtl pattern", "tb pattern", "rtl unchecked path", "generator pattern", "generator dir"],
)
def test_a_delivery_that_a_source_entry_would_read_is_refused_before_any_tool(
    tmp_path, deliverer, entry, parts, destination, where
):
    if where == "generator":
        parts["rtl"]["sources"] = ["gen/top.v"]
    design = _design(tmp_path / "design", **parts)
    target = design.root_path / destination
    assert not target.exists()
    with pytest.raises(DeliveryError) as error:
        _launch(tmp_path, deliverer, design, target)
    message = str(error.value)
    assert f"`{entry}`" in message and "sources" in message and where in message, message
    assert not RUNS and not target.exists()
    assert not (tmp_path / "xeda_run" / ".generated").exists(), "no generator ran"


def test_the_refusal_names_the_part_of_the_entry(tmp_path, deliverer):
    design = _design(tmp_path / "design", tb={"sources": ["tb/*.vhd"]})
    with pytest.raises(DeliveryError, match=r"`tb/\*.vhd` entry of `tb.sources`"):
        _launch(tmp_path, deliverer, design, design.root_path / "tb" / "new.vhd")


def test_a_star_does_not_cross_a_directory(tmp_path, deliverer):
    design = _design(tmp_path / "design", rtl={"sources": ["src/*.v"]})
    target = design.root_path / "src" / "gen" / "new.v"
    assert _launch(tmp_path, deliverer, design, target).succeeded
    assert target.read_text() == "net\n"


def test_a_file_that_no_entry_reads_is_delivered(tmp_path, deliverer):
    design = _design(tmp_path / "design", rtl={"sources": ["src/*.v"]})
    target = design.root_path / "src" / ".hidden.v"
    assert _launch(tmp_path, deliverer, design, target).succeeded
    assert target.read_text() == "net\n"


def test_the_entry_of_a_dependency_is_judged_against_its_own_root(tmp_path, deliverer):
    dependency = tmp_path / "design" / "dep"
    dependency.mkdir(parents=True)
    (dependency / "lib").mkdir()
    (dependency / "lib" / "a.v").write_text("module a; endmodule\n")
    (dependency / "dep.yaml").write_text("name: dep\nrtl:\n  sources: [lib/*.v]\n  top: a\n")
    design = _design(tmp_path / "design", dependencies=["dep/dep.yaml"])
    with pytest.raises(DeliveryError, match=r"`lib/\*.v` entry of `rtl.sources`"):
        _launch(tmp_path, deliverer, design, dependency / "lib" / "new.v")
    assert not RUNS


def test_outputs_to_of_a_launch_is_judged_by_the_same_rule(tmp_path, deliverer):
    """The destination is known only after the run: the files of `--outputs-to` come from it."""
    (tmp_path / "design" / "outputs").mkdir(parents=True)
    (tmp_path / "design" / "outputs" / "seed.v").write_text("// seed\n")
    design = _design(tmp_path / "design", rtl={"sources": ["outputs/*.v"]})
    runner = DefaultRunner(
        tmp_path / "xeda_run", display_results=False, outputs_to=design.root_path
    )
    with pytest.raises(DeliveryError, match=r"`outputs/\*.v` entry of `rtl.sources`"):
        runner.launch_flow(deliverer, design, {"netlist": "outputs/d.v"})
    assert not (design.root_path / "outputs" / "d.v").exists()


# ---------------------------------------------------------------- the entry is no identity


def test_the_entry_of_a_source_is_private_and_no_part_of_its_identity(tmp_path):
    """One file listed by name and through a pattern is one source: equal, equal in hash and in
    JSON value, and the designs have one configuration and one node identity."""
    from xeda.flow_runner.configuration import design_configuration_hash

    from .generation_world import reader_flow

    root = tmp_path / "design"
    _tree(root)
    by_name = Design(name="d", design_root=root, rtl={"sources": ["src/a.v"], "top": "a"})
    by_pattern = Design(name="d", design_root=root, rtl={"sources": ["src/*.v"], "top": "a"})
    (named,), (matched,) = by_name.rtl.sources, by_pattern.rtl.sources
    assert named == matched and hash(named) == hash(matched)
    assert named.as_json_value() == matched.as_json_value()
    assert not hasattr(named, "entry") and not hasattr(named, "entry_root")
    assert design_configuration_hash(by_name) == design_configuration_hash(by_pattern)
    assert by_name.source_entries() != by_pattern.source_entries(), "only the reader sees them"
    with preserved_flow_registry():
        flow = reader_flow()
        hashes = [
            {n.name: n.flowrun_hash for n in DefaultRunner(tmp_path / "run").plan(flow, d).nodes}
            for d in (by_name, by_pattern)
        ]
    assert hashes[0] == hashes[1]


# ---------------------------------------------------------------- every declared entry stays


def test_a_pattern_whose_matches_are_all_listed_already_stays_an_entry(tmp_path, deliverer):
    """`src/*.v` matches only `seed.v` today, and `seed.v` is listed once already. The pattern
    still reads `src/new.v` on the next load."""
    root = tmp_path / "design"
    (root / "src").mkdir(parents=True)
    (root / "src" / "seed.v").write_text("module seed; endmodule\n")
    design = Design(
        name="d", design_root=root, rtl={"sources": ["src/seed.v", "src/*.v"], "top": "seed"}
    )
    assert [e.text for e in design.source_entries()] == ["src/seed.v", "src/*.v"]
    target = root / "src" / "new.v"
    with pytest.raises(DeliveryError, match=r"`src/\*.v` entry of `rtl.sources`"):
        _launch(tmp_path, deliverer, design, target)
    assert not RUNS and not target.exists()


def test_an_entry_of_a_dependency_stays_when_the_design_lists_the_file_first(tmp_path, deliverer):
    root = tmp_path / "design"
    (root / "dep" / "lib").mkdir(parents=True)
    (root / "dep" / "lib" / "a.v").write_text("module a; endmodule\n")
    (root / "dep" / "dep.yaml").write_text("name: dep\nrtl:\n  sources: [lib/*.v]\n  top: a\n")
    (root / "top.v").write_text("module top; endmodule\n")
    design = Design(
        name="d",
        design_root=root,
        rtl={"sources": ["top.v", "dep/lib/a.v"], "top": "top"},
        dependencies=["dep/dep.yaml"],
    )
    assert sorted(s.path.name for s in design.rtl.sources) == ["a.v", "top.v"], "listed once"
    with pytest.raises(DeliveryError, match=r"`lib/\*.v` entry of `rtl.sources`"):
        _launch(tmp_path, deliverer, design, root / "dep" / "lib" / "new.v")


def test_a_file_listed_twice_stays_one_source_and_the_entries_are_no_identity(tmp_path):
    root = tmp_path / "design"
    _tree(root)
    once = Design(name="d", design_root=root, rtl={"sources": ["src/a.v"], "top": "a"})
    twice = Design(name="d", design_root=root, rtl={"sources": ["src/a.v", "src/*.v"], "top": "a"})
    assert once.rtl.sources == twice.rtl.sources and len(twice.rtl.sources) == 1
    assert once.rtl.sources[0].as_json_value() == twice.rtl.sources[0].as_json_value()
    assert len(twice.source_entries()) == 2
