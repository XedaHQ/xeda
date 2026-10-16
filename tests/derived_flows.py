"""Test converter flows: flows with a design output (`Out(DESIGN, ...)`) that need no tool.

A converter registers itself when its class is defined, so `converter_flows` defines the classes
when it is called. Call it inside `preserved_flow_registry` (`tests/flow_registry.py`): the
registry is then as it was when the test ends.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from xeda.design import SourceType
from xeda.flow import DESIGN, Flow, Out
from xeda.flow.derived import DerivedSources, DesignConversion, Generated, Passed
from xeda.utils import replacing_file

#: The text of a module `FakeConverter` writes for a source.
MODULE_TEXT = "module {name}(); endmodule\n"


def converter_flows() -> tuple[type[Flow], type[Flow], type[Flow]]:
    """Define three converters and return them.

    The first one, `_FakeBsc`, converts Bluespec into Verilog like bsc: it writes a module named
    like each Bluespec source, and passes every other source through. The second one, `_Rival`,
    is the same flow under another name, so that two converters fit the same input. The third,
    `_ToVhdl`, converts Bluespec into VHDL, which a Verilog tool cannot read."""

    class _FakeBsc(Flow):
        """Converts each Bluespec source into a module of the same name, for the tests."""

        results_description: ClassVar[dict[str, str]] = {}
        reads_sources = frozenset({SourceType.Bluespec, SourceType.Verilog})
        written = SourceType.Verilog

        class Outputs(Flow.Outputs):
            design: DerivedSources | None = Out(DESIGN, description="The converted design.")

        @classmethod
        def design_conversion(cls, settings: Flow.Settings) -> DesignConversion | None:
            return DesignConversion(frozenset({SourceType.Bluespec}), frozenset({cls.written}))

        def run(self) -> None:
            entries: list[Generated | Passed] = []
            for index, source in enumerate(self.design.rtl.sources):
                if source.type is SourceType.Bluespec:
                    path = self.run_path / f"{Path(source.file).stem}.v"
                    with replacing_file(self.run_directory.writable(path)) as f:
                        f.write(MODULE_TEXT.format(name=path.stem))
                    entries.append(Generated(path, self.written))
                else:
                    entries.append(Passed(index))
            assert isinstance(self.outputs, self.Outputs)
            self.outputs.design = DerivedSources(self.design.rtl.top, tuple(entries))

    class _Rival(_FakeBsc):
        """The same converter under another name."""

    class _ToVhdl(_FakeBsc):
        """A converter whose output a Verilog tool cannot read."""

        written = SourceType.Vhdl

    return _FakeBsc, _Rival, _ToVhdl
