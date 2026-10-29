"""A design whose sources a generator writes, and a flow that reads them, for the tests of the
generation stage of a launch.

The generator is a Python script. It writes `gen/top.v` into the storage that Xeda names in
`XEDA_GENERATED`, from `spec.txt` in the design root, and it appends a line to a counter file
outside the design, so a test counts the runs. It needs nothing but Python, so these tests run on
every operating system.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, In, MetricSpec
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.launch_result import launch_result
from xeda.outcomes import Result
from xeda.spec import Spec

#: Writes `gen/top.v` from `spec.txt`, notes where it ran, and counts its runs. Its arguments are
#: the counter file, a width that goes into the text, and an optional note file. A file named
#: `fail` in the design root makes it fail; the counter counts the failed run too.
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
out = Path(os.environ["XEDA_GENERATED"])
counter, width = sys.argv[1], sys.argv[2]
with open(counter, "a") as stream:
    stream.write("ran\\n")
Path(counter + ".where").write_text(
    "\\n".join([os.getcwd(), os.environ["DESIGN_ROOT"], os.environ["XEDA_GENERATED"]]) + "\\n"
)
if (root / "fail").exists():
    raise SystemExit(3)
(out / "gen").mkdir(exist_ok=True)
(out / "gen" / "top.v").write_text("// " + (root / "spec.txt").read_text() + "// W=" + width + "\\n")
"""


def reader_flow() -> type[Flow]:
    """Define a flow that reads the Verilog sources of its design and reports their text.

    Call it inside `preserved_flow_registry`: the class registers itself."""

    class TGenReader(Flow):
        """Reads the Verilog sources of the design, for the tests of generation."""

        results_description: ClassVar[dict[str, MetricSpec]] = {
            "seen": MetricSpec("The text of its sources, in order.", "text")
        }
        reads_sources = frozenset({SourceType.Verilog})

        def run(self) -> None:
            self.results["seen"] = "".join(src.path.read_text() for src in self.sources_read())

    return TGenReader


def input_reader_flow() -> type[Flow]:
    """Define a flow that takes a Verilog design source through a declared input.

    Call it inside `preserved_flow_registry`: the class registers itself."""

    class TGenInputReader(Flow):
        """Reads its declared input, which the design supplies, for the tests of generation."""

        results_description: ClassVar[dict[str, MetricSpec]] = {
            "seen": MetricSpec("The text of its input.", "text")
        }

        class Inputs(Flow.Inputs):
            netlist: Path = In(SourceType.Verilog, description="The Verilog source it reads.")

        def run(self) -> None:
            self.results["seen"] = self.inputs.netlist.read_text()

    return TGenInputReader


def blocking_reader_flow(started: Path, gate: Path) -> type[Flow]:
    """Define a flow that reads the Verilog sources, tells the test that it holds them, and reads
    them again after the test opens the gate (`started` and `gate` are named pipes).

    Call it inside `preserved_flow_registry`: the class registers itself."""

    class TGenBlockingReader(Flow):
        """Holds the generated sources for as long as the test wants, for the tests of leases."""

        results_description: ClassVar[dict[str, MetricSpec]] = {
            "seen": MetricSpec("The text of its sources before and after the wait.", "text")
        }
        reads_sources = frozenset({SourceType.Verilog})

        def run(self) -> None:
            first = "".join(src.path.read_text() for src in self.sources_read())
            open(started, "wb").close()  # blocks until the test reads the pipe
            open(gate, "rb").read()  # blocks until the test writes the pipe
            last = "".join(src.path.read_text() for src in self.sources_read())
            self.results["seen"] = first + "|" + last

    return TGenBlockingReader


class World:
    """A design with a generator, in `tmp_path / <directory>`, and the run root beside it."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        directory: str = "design",
        width: str = "8",
        sources: list[str] | None = None,
        parameters: str = "",
        generator: dict | None = None,
        extra: str = "",
        counter: Path | None = None,
    ) -> None:
        self.root = tmp_path / directory
        self.root.mkdir(parents=True, exist_ok=True)
        self.counter = counter or tmp_path / f"{directory}-runs.log"
        self.run_root = tmp_path / "xeda_run"
        (self.root / "gen.py").write_text(GENERATOR)
        (self.root / "spec.txt").write_text("one\n")
        recipe = {
            "executable": sys.executable,
            "args": ["$DESIGN_ROOT/gen.py", str(self.counter), width],
            "sources": ["spec.txt", "gen.py"],
            **(generator or {}),
        }
        self.design_file = self.root / "design.yaml"
        lines = [
            "name: generated",
            "rtl:",
            f"  sources: {sources if sources is not None else ['gen/top.v']!r}",
            "  top: top",
            *([f"  parameters: {parameters}"] if parameters else []),
            "  generator:",
            *(f"    {key}: {value!r}" for key, value in recipe.items()),
        ]
        self.design_file.write_text("\n".join(lines) + "\n" + extra)

    @property
    def runs(self) -> int:
        """How many times the generator has run."""
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    @property
    def where(self) -> tuple[str, str, str]:
        """The working directory, `DESIGN_ROOT` and `XEDA_GENERATED` of the last run."""
        cwd, design_root, generated = Path(str(self.counter) + ".where").read_text().splitlines()
        return cwd, design_root, generated

    def load(self) -> Design:
        return Design.load(self.design_file)

    def runner(self, **settings) -> DefaultRunner:
        return DefaultRunner(self.run_root, display_results=False, **settings)

    def storages(self) -> list[Path]:
        """The storage directories under the run root."""
        base = self.run_root / ".generated"
        return sorted(p for p in base.glob("*/*") if p.is_dir()) if base.is_dir() else []

    def launch(
        self, flow: type[Flow], runner: DefaultRunner | None = None, design: Design | None = None
    ) -> Result:
        """Launch `flow` on the design and return what the launch did."""
        runner = runner or self.runner()
        design = design or self.load()
        return launch_result(
            runner,
            Spec(flow.name, design),
            lambda: runner.run_flow(flow, design, {}),
        )
