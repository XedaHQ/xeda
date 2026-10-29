"""Two flows in a chain for the tests of cancellation and events, and a design to run them on.

The producer runs a program through a `Tool` when it is given one: the tests make it a program that
runs until Xeda stops it. The consumer copies the output of the producer.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

from xeda import Design
from xeda.design import SourceType
from xeda.flow import Flow, In, MetricSpec, Out
from xeda.tool import Tool


def cut_flows(program: str | None = None) -> tuple[type[Flow], type[Flow]]:
    """Define the producer `cut_producer` and its consumer `cut_consumer`.

    Call it inside `preserved_flow_registry`: the classes register themselves. With `program`, the
    producer runs that Python program (`python -c`) before it writes its output."""

    class CutProducer(Flow):
        """Runs a program if it has one, and writes outputs/made.txt."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Outputs(Flow.Outputs):
            made: Path = Out(SourceType.Data, description="The text that the producer wrote.")

        def run(self) -> None:
            if program is not None:
                Tool(executable=sys.executable).run("-c", program)
            out = self.run_path / "outputs" / "made.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text("made\n")
            self.artifacts.made = out
            self.outputs.made = out

    class CutConsumer(Flow):
        """Copies the output of its producer to outputs/copied.txt."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}
        design_source_types = frozenset()

        class Inputs(Flow.Inputs):
            made: Path = In(
                SourceType.Data,
                producer="cut_producer",
                output="made",
                description="The text of the producer.",
            )

        def run(self) -> None:
            out = self.run_path / "outputs" / "copied.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(self.inputs.made.read_text())
            self.artifacts.copied = out

    return CutProducer, CutConsumer


def cut_design(root: Path | str) -> Design:
    """Return a design in `root` for the flows of `cut_flows`."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.v").write_text("module a; endmodule\n")
    return Design(name="d", root=root, rtl={"sources": ["a.v"], "top": "a"})
