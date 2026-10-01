"""Test-only flows that declare their inputs and outputs (`xeda.flow.io`), for the resolver, the
launcher's hand-over and the read locks. Their class names start with `_`, so the sweeps over
every flow (`settings_samples.flow_classes`) leave them out; they are registered as `__maker`,
`__taker`, ... (`camelcase_to_snakecase`)."""

from pathlib import Path
from typing import ClassVar

from xeda.dataclass import Field
from xeda.design import SourceType
from xeda.flow import Flow, FpgaSynthFlow, In, Out
from xeda.utils import replacing_file


class _Maker(Flow):
    """Writes `made.txt`, its declared output `made`."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        text: str = Field("made\n", description="What `made.txt` holds.")
        write: bool = Field(True, description="Whether the run writes `made.txt`.")

    class Outputs(Flow.Outputs):
        made: Path | None = Out(SourceType.Data, enabled_by="write", description="The file.")

    def run(self) -> None:
        if self.settings.write:
            path = self.run_path / "made.txt"
            with replacing_file(self.run_directory.writable(path)) as f:
                f.write(self.settings.text)
            self.outputs.made = path


class _Taker(Flow):
    """Reads its declared input `made`: by default `_Maker`'s output."""

    results_description: ClassVar[dict[str, str]] = {}

    class Inputs(Flow.Inputs):
        made: Path = In(
            SourceType.Data, producer="__maker", output="made", description="What it reads."
        )

    def run(self) -> None:
        self.results["read"] = self.inputs.made.read_text()


class _InputMaker(_Maker):
    """Makes its output from a file named only in settings."""

    results_description = {}

    class Settings(_Maker.Settings):
        input_file: Path | None = Field(None, description="The file to copy into its output.")

    def run(self):
        assert self.settings.input_file is not None
        self.settings.text = self.settings.input_file.read_text()
        super().run()


class _PartialMaker(_Maker):
    """Writes one output but leaves another required output absent."""

    results_description = {}

    class Outputs(_Maker.Outputs):
        missing: Path = Out(SourceType.Data, description="The absent required output.")


class _InputTaker(_Taker):
    """Reads the settings-file producer's declared output."""

    results_description = {}

    class Inputs(_Taker.Inputs):
        made: Path = In(
            SourceType.Data, producer="__input_maker", output="made", description="What it reads."
        )


class _Reader(Flow):
    """Reads a `Data` file the design must list: an input with no default producer."""

    results_description: ClassVar[dict[str, str]] = {}

    class Inputs(Flow.Inputs):
        data: Path = In(SourceType.Data, description="The data file it reads.")

    def run(self) -> None:
        self.results["read"] = self.inputs.data.read_text()


class _Wrapper(Flow):
    """Declares nothing: registers `_Taker` in its `init()`, as flows without declarations do."""

    results_description: ClassVar[dict[str, str]] = {}

    def init(self) -> None:
        self.add_dependency(_Taker, _Taker.Settings())

    def run(self) -> None:
        (taker,) = self.completed_dependencies
        self.results["read"] = taker.results["read"]


class _Synth(FpgaSynthFlow):
    """Writes a netlist, its declared output."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(FpgaSynthFlow.Settings):
        """`_Synth`'s settings: an FPGA synthesis's."""

    class Outputs(FpgaSynthFlow.Outputs):
        netlist: Path = Out(SourceType.JsonNetlist, description="Its netlist.")

    def run(self) -> None:
        path = self.run_path / "net.json"
        with replacing_file(self.run_directory.writable(path)) as f:
            f.write("{}\n")
        self.outputs.netlist = path


class _Place(FpgaSynthFlow):
    """Places `_Synth`'s netlist, holding its settings as `nextpnr` holds `yosys_fpga`'s."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(FpgaSynthFlow.Settings):
        synth: _Synth.Settings = Field(
            default_factory=_Synth.Settings,
            description="Settings for the `_Synth` that makes its netlist.",
        )

        dependency_settings: ClassVar[dict[str, tuple[str, ...]]] = {"synth": ("fpga", "clocks")}

    class Inputs(FpgaSynthFlow.Inputs):
        netlist: Path = In(
            SourceType.JsonNetlist, producer="__synth", description="The netlist it places."
        )

    def run(self) -> None:
        self.results["placed"] = str(self.inputs.netlist)
