"""A launch copies the generated sources into the design tree when the generator asks.

`rtl.generator.outputs_to` is a delivery like the others. At the end of a launch that passed, each
file that a declaration matched in the storage is copied to `<outputs_to>/<its name in the output
of the generator>`, by the code that delivers deliverable settings, under the same rules: a
changed copy needs a yes or `overwrite_outputs`, and nothing goes onto an input, over a directory
or through a link. The first flow that reads the generated sources delivers them, and its record
lists each copy under the key `generator`.
"""

import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from xeda import Design
from xeda.deliver import DeliveryError, OutputExistsError
from xeda.flow import Flow, In, MetricSpec, Out
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.launch_result import launch_result
from xeda.outcomes import Outcome
from xeda.design import SourceType
from xeda.spec import Spec

from .flow_registry import preserved_flow_registry
from .generation_world import reader_flow

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no storage locks")

#: Appends a line to the counter its first argument names. Then it writes each file named after
#: it in its storage, with the text `from <name>`. A name `link:<name>` makes a link to the file
#: that the environment variable `LINK_TARGET` names.
GENERATOR = r"""
import os, sys
from pathlib import Path

root = Path(os.environ["XEDA_GENERATED"])
with open(sys.argv[1], "a") as counter:
    counter.write("ran\n")
for name in sys.argv[2:]:
    link = name.startswith("link:")
    path = root / name.removeprefix("link:")
    path.parent.mkdir(parents=True, exist_ok=True)
    if link:
        path.symlink_to(os.environ["LINK_TARGET"])
    else:
        path.write_text("// from " + name + "\n")
"""


@pytest.fixture(autouse=True)
def isolate_registration():
    with preserved_flow_registry():
        yield


class Delivering:
    """A design whose generator writes `writes`, listed in `rtl.sources` as `sources`."""

    def __init__(
        self,
        tmp_path: Path,
        writes: list[str],
        *,
        outputs_to: str | None = "$DESIGN_ROOT/out",
        sources: list | None = None,
        generated_sources: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.root = tmp_path / "design"
        self.root.mkdir(exist_ok=True)
        self.counter = tmp_path / "generator.log"
        self.run_root = tmp_path / "xeda_run"
        (self.root / "gen.py").write_text(GENERATOR)
        recipe: dict = {
            "executable": sys.executable,
            "args": ["$DESIGN_ROOT/gen.py", str(self.counter), *writes],
            "sources": ["gen.py"],
        }
        if outputs_to is not None:
            recipe["outputs_to"] = outputs_to
        if generated_sources is not None:
            recipe["generated_sources"] = generated_sources
        if env is not None:
            recipe["env"] = {**env, "PATH": os.environ.get("PATH", "")}
        self.file = self.root / "design.json"
        self.file.write_text(
            json.dumps(
                {
                    "name": "delivering",
                    "rtl": {
                        "sources": sources if sources is not None else ["gen/top.v"],
                        "top": "top",
                        "generator": recipe,
                    },
                }
            )
        )
        self.flow = reader_flow()

    @property
    def runs(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def design(self) -> Design:
        return Design.from_file(self.file)

    def runner(self, **settings) -> DefaultRunner:
        return DefaultRunner(self.run_root, display_results=False, **settings)

    def launch(self, runner: DefaultRunner | None = None, flow: type[Flow] | None = None) -> Flow:
        return (runner or self.runner()).launch_flow(flow or self.flow, self.design(), {})


def _delivered(flow: Flow) -> dict[str, str]:
    return {Path(d.destination).as_posix(): d.state for d in flow.deliveries}


def test_a_passed_launch_copies_the_generated_sources_under_their_names(tmp_path):
    world = Delivering(
        tmp_path, ["gen/top.v", "gen/sub/more.v"], sources=["gen/*.v", "gen/sub/*.v"]
    )
    flow = world.launch()
    assert flow.succeeded
    out = world.root / "out"
    assert (out / "gen" / "top.v").read_text() == "// from gen/top.v\n"
    assert (out / "gen" / "sub" / "more.v").read_text() == "// from gen/sub/more.v\n"
    assert {d.key for d in flow.deliveries} == {"generator"}
    assert _delivered(flow) == {
        (out / "gen" / "top.v").as_posix(): "delivered",
        (out / "gen" / "sub" / "more.v").as_posix(): "delivered",
    }


def test_a_result_lists_each_copy_under_the_key_generator(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    runner = world.runner()
    design = world.design()
    result = launch_result(
        runner,
        Spec(world.flow.name, design),
        lambda: runner.run_flow(world.flow, design, {}),
    )
    assert result.outcome is Outcome.PASSED
    (record,) = result.nodes[world.flow.name].deliveries
    assert (record.key, record.destination, record.delivered) == (
        "generator",
        world.root / "out" / "gen" / "top.v",
        True,
    )


def test_a_pattern_delivers_each_file_that_it_matched(tmp_path):
    world = Delivering(tmp_path, ["gen/a.v", "gen/b.v", "gen/c.v"], sources=["gen/*.v"])
    assert world.launch().succeeded
    assert sorted(p.name for p in (world.root / "out" / "gen").iterdir()) == ["a.v", "b.v", "c.v"]


def test_without_outputs_to_nothing_is_copied(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"], outputs_to=None)
    flow = world.launch()
    assert flow.succeeded and not flow.deliveries
    assert not (world.root / "out").exists()


def test_the_design_tree_gets_nothing_until_the_launch_passed(tmp_path):
    class Failing(Flow):
        """Reads the sources and reports a failure."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        def run(self) -> None:
            pass

        def parse_reports(self) -> bool:
            return False

    world = Delivering(tmp_path, ["gen/top.v"])
    flow = world.launch(flow=Failing)
    assert not flow.succeeded
    assert world.runs == 1, "the generator ran"
    assert not (world.root / "out").exists()


def test_an_unchanged_copy_is_found_again_and_a_changed_one_is_asked_about(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    assert world.launch().succeeded
    copy = world.root / "out" / "gen" / "top.v"
    again = world.launch()
    assert again.succeeded and _delivered(again) == {copy.as_posix(): "unchanged"}
    copy.write_text("// edited\n")
    with pytest.raises(OutputExistsError, match="changed since Xeda wrote it"):
        world.launch()
    assert copy.read_text() == "// edited\n"
    assert world.runs == 1, "the question came before any new generation"


def test_a_changed_copy_is_replaced_with_overwrite_outputs(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    assert world.launch().succeeded
    copy = world.root / "out" / "gen" / "top.v"
    copy.write_text("// edited\n")
    flow = world.launch(world.runner(overwrite_outputs=True))
    assert flow.succeeded and copy.read_text() == "// from gen/top.v\n"


def test_a_changed_copy_is_replaced_when_the_user_says_yes(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    assert world.launch().succeeded
    copy = world.root / "out" / "gen" / "top.v"
    copy.write_text("// edited\n")
    asked: list[str] = []
    runner = world.runner()
    runner.confirm_overwrite = lambda conflicts: asked.append(str(conflicts[0].destination)) or True
    assert world.launch(runner).succeeded
    assert asked == [str(copy)] and copy.read_text() == "// from gen/top.v\n"


def test_a_file_of_the_user_in_the_way_is_asked_about_before_the_generator_runs(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    copy = world.root / "out" / "gen" / "top.v"
    copy.parent.mkdir(parents=True)
    copy.write_text("// the user's\n")
    with pytest.raises(OutputExistsError, match="top.v"):
        world.launch()
    assert world.runs == 0 and copy.read_text() == "// the user's\n"


def test_a_directory_in_the_way_is_refused_before_the_generator_runs(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    (world.root / "out" / "gen" / "top.v").mkdir(parents=True)
    with pytest.raises(DeliveryError, match="a directory"):
        world.launch()
    assert world.runs == 0


def test_a_link_in_the_way_is_not_written_through(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    elsewhere = tmp_path / "elsewhere.v"
    elsewhere.write_text("// the user's\n")
    link = world.root / "out" / "gen" / "top.v"
    link.parent.mkdir(parents=True)
    link.symlink_to(elsewhere)
    with pytest.raises(OutputExistsError, match="symbolic link"):
        world.launch()
    assert elsewhere.read_text() == "// the user's\n" and link.is_symlink()


def test_a_run_root_is_no_destination(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"], outputs_to=str(tmp_path / "xeda_run" / "mine"))
    with pytest.raises(DeliveryError, match="run root"):
        world.launch()
    assert world.runs == 0


# ---------------------------------------------------------------- the source-entry rule


def test_a_destination_that_an_entry_of_the_sources_reads_is_refused_before_the_generator(
    tmp_path,
):
    """The file `new.v` does not exist yet: the entry `lib/*.v` names it all the same."""
    (tmp_path / "design" / "lib").mkdir(parents=True)
    (tmp_path / "design" / "lib" / "seed.v").write_text("module seed; endmodule\n")
    world = Delivering(
        tmp_path,
        ["new.v"],
        outputs_to="$DESIGN_ROOT/lib",
        sources=["lib/*.v", "new.v"],
        generated_sources=["new.v"],
    )
    target = world.root / "lib" / "new.v"
    assert not target.exists()
    with pytest.raises(DeliveryError, match=r"`lib/\*.v` entry of `rtl.sources`"):
        world.launch()
    assert world.runs == 0 and not target.exists()


def test_a_destination_that_an_entry_of_the_generator_reads_is_refused(tmp_path):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "inputs").mkdir()
    (tmp_path / "design" / "inputs" / "spec.txt").write_text("spec\n")
    world = Delivering(tmp_path, ["new.v"], outputs_to="$DESIGN_ROOT/inputs", sources=["new.v"])
    recipe = json.loads(world.file.read_text())
    recipe["rtl"]["generator"]["sources"] = ["gen.py", "inputs/*.txt"]
    world.file.write_text(json.dumps(recipe))
    # `new.v` lands in `inputs/`, which `inputs/*.txt` does not read: allowed.
    assert world.launch().succeeded
    recipe["rtl"]["generator"]["sources"] = ["gen.py", "inputs"]
    world.file.write_text(json.dumps(recipe))
    with pytest.raises(DeliveryError, match="entry of `rtl.generator.sources`"):
        world.launch(world.runner(rebuild_all=True))


def test_a_star_does_not_cross_a_directory(tmp_path):
    (tmp_path / "design" / "src").mkdir(parents=True)
    (tmp_path / "design" / "src" / "a.v").write_text("module a; endmodule\n")
    world = Delivering(
        tmp_path,
        ["gen/top.v"],
        outputs_to="$DESIGN_ROOT/src/gen",
        sources=["src/*.v", "gen/top.v"],
        generated_sources=["gen/top.v"],
    )
    assert world.launch().succeeded
    assert (world.root / "src" / "gen" / "gen" / "top.v").is_file()


def test_a_delivery_that_restores_a_listed_generated_file_is_allowed(tmp_path):
    """The design reads `gen/top.v` from the storage, never from the tree."""
    world = Delivering(tmp_path, ["gen/top.v"], outputs_to="$DESIGN_ROOT")
    first = world.launch()
    assert first.succeeded and (world.root / "gen" / "top.v").is_file()
    second = world.launch(world.runner(rebuild_all=True))
    assert second.succeeded and world.runs == 2
    assert {Path(d.destination) for d in second.deliveries} == {world.root / "gen" / "top.v"}


# ---------------------------------------------------------------- the files of the storage


def test_a_link_the_generator_left_to_a_file_outside_the_storage_is_skipped(tmp_path, caplog):
    outside = tmp_path / "outside.v"
    outside.write_text("// not the generator's\n")
    world = Delivering(
        tmp_path,
        ["gen/top.v", "link:gen/linked.v"],
        sources=["gen/top.v", "gen/linked.v"],
        env={"LINK_TARGET": str(outside)},
    )
    with caplog.at_level("WARNING", logger="xeda.deliver"):
        flow = world.launch()
    assert flow.succeeded
    assert (world.root / "out" / "gen" / "top.v").is_file()
    assert not (world.root / "out" / "gen" / "linked.v").exists()
    states = {Path(d.destination).name: d.state for d in flow.deliveries}
    assert states == {"top.v": "delivered", "linked.v": "skipped"}
    assert "outside the generation storage" in caplog.text


def test_post_cleanup_purge_copies_the_files_of_the_storage_and_leaves_them(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    runner = world.runner()
    runner.settings.post_cleanup = True
    runner.settings.post_cleanup_purge = True
    assert world.launch(runner).succeeded
    (storage,) = [p for p in (world.run_root / ".generated").glob("*/*") if p.is_dir()]
    assert (storage / "gen" / "top.v").is_file(), "the storage keeps its file"
    assert (world.root / "out" / "gen" / "top.v").is_file()


def test_the_record_of_the_node_that_delivered_remembers_the_copy(tmp_path):
    world = Delivering(tmp_path, ["gen/top.v"])
    flow = world.launch()
    record = json.loads((flow.run_path.parent / f"{flow.run_path.name}.delivered.json").read_text())
    (entry,) = record["files"].values()
    assert entry["setting"] == "generator"


# ---------------------------------------------------------------- which node delivers


def test_only_the_first_flow_that_reads_the_sources_delivers_them(tmp_path):
    """Two flows of a plan read the generated sources, and each file is copied once."""

    class _First(Flow):
        """Reads the Verilog sources of the design and writes a file for the next flow."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        class Outputs(Flow.Outputs):
            made: Path = Out(SourceType.Data, description="What it writes.")

        def run(self) -> None:
            self.outputs.made = self.run_path / "made.txt"
            self.outputs.made.write_text("made\n")

    class _Second(Flow):
        """Reads the Verilog sources of the design, and the file of the first flow."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        class Inputs(Flow.Inputs):
            made: Path = In(
                SourceType.Data, producer=_First.name, output="made", description="The file."
            )

        def run(self) -> None:
            pass

    world = Delivering(tmp_path, ["gen/top.v"])
    runner = world.runner()
    design = world.design()
    result = launch_result(
        runner, Spec(_Second.name, design), lambda: runner.run_flow(_Second, design, {})
    )
    assert result.outcome is Outcome.PASSED, result.error
    assert [r.key for r in result.nodes[_First.name].deliveries] == ["generator"]
    assert result.nodes[_Second.name].deliveries == ()
    assert (world.root / "out" / "gen" / "top.v").is_file()


# ---------------------------------------------------------------- what a pattern reveals


def test_a_destination_that_only_a_pattern_reveals_is_refused_before_any_flow_runs(tmp_path):
    """The names that `*.v` matches are known after the generator ran. The refusal still comes
    before the first flow of the plan runs, and nothing is copied."""
    (tmp_path / "design" / "lib").mkdir(parents=True)
    (tmp_path / "design" / "lib" / "seed.v").write_text("module seed; endmodule\n")
    world = Delivering(
        tmp_path,
        ["new.v"],
        outputs_to="$DESIGN_ROOT/lib",
        sources=["lib/*.v", "*.v"],
        generated_sources=["*.v"],
    )
    runner = world.runner()
    with pytest.raises(DeliveryError, match=r"`lib/\*.v` entry of `rtl.sources`"):
        world.launch(runner)
    assert world.runs == 1 and not runner.launched
    assert not (world.root / "lib" / "new.v").exists()


# ---------------------------------------------------------------- dependencies


def test_a_dependency_delivers_its_own_generated_sources_below_its_own_root(tmp_path):
    from .test_dependency_generation import Chain

    chain = Chain(tmp_path)
    library = tmp_path / "lib1" / "lib1.yaml"
    library.write_text(library.read_text() + "    outputs_to: $DESIGN_ROOT/copies\n")
    with preserved_flow_registry():
        flow = reader_flow()
        result = chain.launch(flow)
    assert result.outcome is Outcome.PASSED, result.error
    assert (tmp_path / "lib1" / "copies" / "gen" / "lib1.v").read_text().startswith("module lib1;")
    assert not (tmp_path / "top" / "copies").exists()


def test_a_copy_of_a_generated_source_and_a_named_output_never_share_a_destination(tmp_path):
    from typing import Optional

    from xeda.dataclass import Field, deliverable

    class _Reports(Flow):
        """Reads the Verilog sources and writes a report to a location."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        reads_sources = frozenset({SourceType.Verilog})

        class Settings(Flow.Settings):
            report: Optional[Path] = Field(
                None,
                description="The report it writes.",
                json_schema_extra=deliverable("outputs/{design}.rpt"),
            )

        def run(self) -> None:
            report = self.run_file(Path("outputs") / "delivering.rpt")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("report\n")

    world = Delivering(tmp_path, ["gen/top.v"])
    shared = world.root / "out" / "gen" / "top.v"
    with pytest.raises(DeliveryError, match="both name") as refused:
        world.runner().launch_flow(_Reports, world.design(), {"report": str(shared)})
    assert "rtl.generator.outputs_to" in str(refused.value) and "report" in str(refused.value)
    assert world.runs == 0
