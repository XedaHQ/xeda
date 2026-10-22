"""Test-only flows that declare their inputs and outputs (`xeda.flow.io`), for the resolver, the
launcher's hand-over and the read locks. Their class names start with `_`, so the sweeps over
every flow (`settings_samples.flow_classes`) leave them out; they are registered as `__maker`,
`__taker`, ... (`camelcase_to_snakecase`)."""

from pathlib import Path
from typing import ClassVar, Optional

from xeda.flow import MetricSpec
from xeda.dataclass import LOCATOR, Field, XedaBaseModel
from xeda.design import SourceType
from xeda.flow import Flow, FpgaSynthFlow, In, Out
from xeda.utils import replacing_file


class _Maker(Flow):
    """Writes `made.txt`, its declared output `made`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

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

    results_description: ClassVar[dict[str, MetricSpec]] = {
        "read": MetricSpec("The text it read.", "text")
    }

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


class _ReadsInstalled(_Maker):
    """Makes its output from `lib.txt` in the store that `store` names, as a flow reads installed
    data. It registers that one file as its input."""

    results_description = {}

    class Settings(_Maker.Settings):
        store: Optional[Path] = Field(
            None, json_schema_extra=LOCATOR, description="Where the data is installed."
        )

    def prepare_inputs(self) -> None:
        self.implicit_inputs.extend(self.locate_prepared_inputs() or [])

    def locate_prepared_inputs(self) -> Optional[list[Path]]:
        store = self.settings.store
        return [] if store is None else [store / "lib.txt"]

    def run(self) -> None:
        store = self.settings.store
        if store is not None:
            self.settings.text = (store / "lib.txt").read_text()
        super().run()


class _InstalledTaker(_Taker):
    """Reads the output of `_ReadsInstalled`."""

    class Inputs(_Taker.Inputs):
        made: Path = In(
            SourceType.Data,
            producer="__reads_installed",
            output="made",
            description="What it reads.",
        )


class _Where(XedaBaseModel):
    """A model nested in settings that holds a locator."""

    store: Optional[Path] = Field(
        None, json_schema_extra=LOCATOR, description="Where the data is installed."
    )


class _ReadsInstalledNested(_Maker):
    """Has its locator inside a nested model."""

    results_description = {}

    class Settings(_Maker.Settings):
        where: _Where = Field(default_factory=_Where, description="Where the data is.")


class _PartialMaker(_Maker):
    """Writes one output but leaves another required output absent."""

    results_description = {}

    class Outputs(_Maker.Outputs):
        missing: Path = Out(SourceType.Data, description="The absent required output.")


class _InputTaker(_Taker):
    """Reads the settings-file producer's declared output."""

    class Inputs(_Taker.Inputs):
        made: Path = In(
            SourceType.Data, producer="__input_maker", output="made", description="What it reads."
        )


class _Reader(Flow):
    """Reads a `Data` file the design must list: an input with no default producer."""

    results_description: ClassVar[dict[str, MetricSpec]] = {
        "read": MetricSpec("The text it read.", "text")
    }

    class Inputs(Flow.Inputs):
        data: Path = In(SourceType.Data, description="The data file it reads.")

    def run(self) -> None:
        self.results["read"] = self.inputs.data.read_text()


class _Synth(FpgaSynthFlow):
    """Writes a netlist, its declared output."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

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
    """Places `_Synth`'s netlist, as `nextpnr` places `yosys_fpga`'s."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Settings(FpgaSynthFlow.Settings):
        """`_Place`'s settings: an FPGA implementation's."""

    class Inputs(FpgaSynthFlow.Inputs):
        netlist: Path = In(
            SourceType.JsonNetlist, producer="__synth", description="The netlist it places."
        )

    def run(self) -> None:
        self.results["placed"] = str(self.inputs.netlist)


class _ChainProducer(Flow):
    """Test producer with compatible, ambiguous, optional and many outputs."""

    aliases = ["chain_source"]
    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Settings(Flow.Settings):
        first: bool = Field(False, description="Enable the first optional output.")
        second: bool = Field(False, description="Enable the second optional output.")

    class Outputs(Flow.Outputs):
        json_a: Path | None = Out(
            SourceType.JsonNetlist, enabled_by="first", description="First netlist."
        )
        json_b: Path | None = Out(
            SourceType.JsonNetlist, enabled_by="second", description="Second netlist."
        )
        many_json: list[Path] = Out(SourceType.JsonNetlist, description="A list of netlists.")
        data: Path = Out(SourceType.Data, description="A scalar data file.")
        files: list[Path] = Out(SourceType.Data, description="An ordered list of files.")

    def run(self) -> None:
        pass


class _ChainConsumer(Flow):
    """Test consumer whose required inputs exercise all-compatible matching."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        left: Path = In(SourceType.JsonNetlist, description="The left netlist.")
        right: Path = In(SourceType.JsonNetlist, description="The right netlist.")
        files: list[Path] = In(SourceType.Data, description="An ordered file list.")
        maybe: Path | None = In(SourceType.Data, description="An optional file.")

    def run(self) -> None:
        pass


class _ChainSimpleProducer(Flow):
    """Test producer with one unambiguous output for each required input."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        netlist: Path = Out(SourceType.JsonNetlist, description="The netlist.")
        data: Path = Out(SourceType.Data, description="The data.")

    def run(self) -> None:
        pass


class _ChainSimpleConsumer(Flow):
    """Two required inputs must both bind from one adjacency."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        netlist: Path = In(SourceType.JsonNetlist, description="The required netlist.")
        data: Path = In(SourceType.Data, description="The required data.")

    def run(self) -> None:
        pass


class _ChainOptionalConsumer(Flow):
    """Only optional inputs, which adjacency must not bind automatically."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        maybe: Path | None = In(SourceType.Data, description="An optional scalar.")
        files: list[Path] = In(SourceType.Data, optional=True, description="Optional files.")

    def run(self) -> None:
        pass


class _ChainAmbiguousDefault(Flow):
    """A default edge explicitly selects one of two compatible outputs."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        netlist: Path = In(
            SourceType.JsonNetlist,
            producer="__chain_producer",
            output="json_b",
            description="The selected netlist.",
        )

    def run(self) -> None:
        pass


class _ChainAmbiguousConsumer(Flow):
    """An unqualified required input that sees both producer outputs."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        netlist: Path = In(SourceType.JsonNetlist, description="The ambiguous netlist.")

    def run(self) -> None:
        pass


class _ChainUndeclared(Flow):
    """A flow without a declared graph contract."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    def run(self) -> None:
        pass


class _ChainAction(Flow):
    """An action that may only be the final chain element."""

    action_reason: ClassVar[str] = "performs a test action"
    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        data: Path = In(SourceType.Data, description="The data consumed by this action.")

    def run(self) -> None:
        pass


class _Fork(Flow):
    """Writes each of two optional netlists a consumer asks for: the demand-union producer."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Settings(Flow.Settings):
        first: bool = Field(False, description="Write the `a` netlist.")
        second: bool = Field(False, description="Write the `b` netlist.")
        label: str = Field("", description="Text written into each netlist.")

    class Outputs(Flow.Outputs):
        a: Path | None = Out(SourceType.JsonNetlist, enabled_by="first", description="Netlist a.")
        b: Path | None = Out(SourceType.JsonNetlist, enabled_by="second", description="Netlist b.")

    def run(self) -> None:
        for name, enabled in (("a", self.settings.first), ("b", self.settings.second)):
            if enabled:
                path = self.run_path / f"{name}.json"
                with replacing_file(self.run_directory.writable(path)) as f:
                    f.write(f"{name}{self.settings.label}\n")
                setattr(self.outputs, name, path)


class _Branch(Flow):
    """Copies its netlist input to its data output; the base of `_Left` and `_Right`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="What it read.")

    def run(self) -> None:
        path = self.run_path / "out.txt"
        with replacing_file(self.run_directory.writable(path)) as f:
            f.write(self.inputs.json_netlist.read_text())
        self.outputs.out = path


class _Left(_Branch):
    """Reads `_Fork`'s `a` netlist."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        json_netlist: Path = In(
            SourceType.JsonNetlist, producer="__fork", output="a", description="Netlist a."
        )


class _Right(_Branch):
    """Reads `_Fork`'s `b` netlist."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        json_netlist: Path = In(
            SourceType.JsonNetlist, producer="__fork", output="b", description="Netlist b."
        )


class _Join(Flow):
    """Reads both branches, which both read `_Fork`: a diamond."""

    results_description: ClassVar[dict[str, MetricSpec]] = {
        "read": MetricSpec("The text it read.", "text"),
        "more": MetricSpec("More text it read.", "text_list"),
    }

    class Inputs(Flow.Inputs):
        left: Path = In(SourceType.Data, producer="__left", output="out", description="Left.")
        right: Path = In(SourceType.Data, producer="__right", output="out", description="Right.")
        more: list[Path] = In(SourceType.Data, optional=True, description="Any further files.")

    def run(self) -> None:
        self.results["read"] = self.inputs.left.read_text() + self.inputs.right.read_text()
        self.results["more"] = [path.read_text() for path in self.inputs.more or []]


class _Relay(Flow):
    """Turns a data file into a netlist: with `_Left`, the two halves of a binding cycle."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        data: Path = In(SourceType.Data, description="The data it relays.")

    class Outputs(Flow.Outputs):
        netlist: Path = Out(SourceType.JsonNetlist, description="The relayed netlist.")

    def run(self) -> None:
        path = self.run_path / "relay.json"
        with replacing_file(self.run_directory.writable(path)) as f:
            f.write(self.inputs.data.read_text())
        self.outputs.netlist = path


class _RouteA(Flow):
    """The head of a three-stage default route: makes `x`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        x: Path = Out(SourceType.JsonNetlist, description="The first stage's file.")

    def run(self) -> None:
        pass


class _RouteB(Flow):
    """The middle of the route: by default reads `_RouteA`'s `x`, makes `y`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        x: Path = In(
            SourceType.JsonNetlist, producer="__route_a", output="x", description="Stage A."
        )

    class Outputs(Flow.Outputs):
        y: Path = Out(SourceType.Data, description="The second stage's file.")

    def run(self) -> None:
        pass


class _RouteC(Flow):
    """The end of the route: by default reads `_RouteB`'s `y`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        y: Path = In(SourceType.Data, producer="__route_b", output="y", description="Stage B.")

    def run(self) -> None:
        pass


class _LoopA(Flow):
    """Default producers that form a cycle (each other's), over a type nothing else makes."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        i: Path = In(SourceType.Sdc, producer="__loop_b", description="From B.")

    class Outputs(Flow.Outputs):
        o: Path = Out(SourceType.Sdc, description="A's file.")

    def run(self) -> None:
        pass


class _LoopB(Flow):
    """The other half of the cycle."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        i: Path = In(SourceType.Sdc, producer="__loop_a", description="From A.")

    class Outputs(Flow.Outputs):
        o: Path = Out(SourceType.Sdc, description="B's file.")

    def run(self) -> None:
        pass


class _ChainAliased(Flow):
    """A consumer with an alias, for completion."""

    aliases = ["sink_alias"]
    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        netlist: Path = In(SourceType.JsonNetlist, description="The netlist.")

    def run(self) -> None:
        pass


class _RouteBoth(Flow):
    """Takes `_RouteA` directly and `_RouteB`, which itself reads `_RouteA`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        x: Path = In(
            SourceType.JsonNetlist, producer="__route_a", output="x", description="Stage A."
        )
        y: Path = In(SourceType.Data, producer="__route_b", output="y", description="Stage B.")

    def run(self) -> None:
        pass


class _DiamondHead(Flow):
    """The head of a diamond: makes a netlist `o1` and a constraints file `o2`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        o1: Path = Out(SourceType.JsonNetlist, description="The netlist.")
        o2: Path = Out(SourceType.Sdc, description="The constraints file.")

    def run(self) -> None:
        pass


class _DiamondLeft(Flow):
    """One side of the diamond: reads the netlist of the head."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        i: Path = In(
            SourceType.JsonNetlist, producer="__diamond_head", output="o1", description="Netlist."
        )

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="The left file.")

    def run(self) -> None:
        pass


class _DiamondRight(Flow):
    """The other side of the diamond: reads the netlist of the head."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        i: Path = In(
            SourceType.JsonNetlist, producer="__diamond_head", output="o1", description="Netlist."
        )

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="The right file.")

    def run(self) -> None:
        pass


class _DiamondTail(Flow):
    """The end of the diamond: reads both sides."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        left: Path = In(
            SourceType.Data, producer="__diamond_left", output="out", description="Left."
        )
        right: Path = In(
            SourceType.Data, producer="__diamond_right", output="out", description="Right."
        )

    def run(self) -> None:
        pass


class _SkipMake(Flow):
    """The head of a route a link can skip: makes a netlist."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        made: Path = Out(SourceType.JsonNetlist, description="The netlist.")

    def run(self) -> None:
        pass


class _SkipSide(Flow):
    """Makes the constraints file that `_SkipMid` also reads. Its own input has no default."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        net: Path = In(SourceType.JsonNetlist, description="The netlist it reads.")

    class Outputs(Flow.Outputs):
        side: Path = Out(SourceType.Sdc, description="The constraints file.")

    def run(self) -> None:
        pass


class _SkipMid(Flow):
    """The middle of the route: reads `_SkipMake`'s netlist and `_SkipSide`'s file, makes data."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        made: Path = In(
            SourceType.JsonNetlist, producer="__skip_make", output="made", description="Netlist."
        )
        side: Path = In(
            SourceType.Sdc, producer="__skip_side", output="side", description="Constraints."
        )

    class Outputs(Flow.Outputs):
        mid: Path = Out(SourceType.Data, description="The data.")

    def run(self) -> None:
        pass


class _SkipEnd(Flow):
    """Reads `_SkipMid`'s data and makes a netlist, which `_SkipMid` and `_SkipSide` could take
    directly: `_SkipMake+_SkipEnd` skips `_SkipMid`, which a flow after it could name."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        mid: Path = In(
            SourceType.Data, producer="__skip_mid", output="mid", description="The data."
        )

    class Outputs(Flow.Outputs):
        end: Path = Out(SourceType.JsonNetlist, description="The netlist.")

    def run(self) -> None:
        pass


class _SkipSink(Flow):
    """Reads `_SkipMid`'s data: `_SkipSide+_SkipSink` skips `_SkipMid` as `_SkipMake+_SkipEnd` does."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        mid: Path = In(
            SourceType.Data, producer="__skip_mid", output="mid", description="The data."
        )

    def run(self) -> None:
        pass


class _FanHead(Flow):
    """The head of a fan: makes the netlist that only `_FanBase` reads."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Outputs(Flow.Outputs):
        net: Path = Out(SourceType.JsonNetlist, description="The netlist.")

    def run(self) -> None:
        pass


class _FanBase(Flow):
    """Reads the netlist of `_FanHead` and makes one file for each of `_FanLeft` and `_FanRight`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        net: Path = In(
            SourceType.JsonNetlist, producer="__fan_head", output="net", description="Netlist."
        )

    class Outputs(Flow.Outputs):
        left: Path = Out(SourceType.Sdc, description="The file for the left flow.")
        right: Path = Out(SourceType.Lpf, description="The file for the right flow.")

    def run(self) -> None:
        pass


class _FanLeft(Flow):
    """Reads the left file of `_FanBase`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        side: Path = In(
            SourceType.Sdc, producer="__fan_base", output="left", description="The left file."
        )

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Data, description="The left result.")

    def run(self) -> None:
        pass


class _FanRight(Flow):
    """Reads the right file of `_FanBase`."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        side: Path = In(
            SourceType.Lpf, producer="__fan_base", output="right", description="The right file."
        )

    class Outputs(Flow.Outputs):
        out: Path = Out(SourceType.Pcf, description="The right result.")

    def run(self) -> None:
        pass


class _FanTail(Flow):
    """Reads both results. The link from `_FanHead` binds an input of `_FanBase` only, and passes
    through `_FanLeft` and `_FanRight` too."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    class Inputs(Flow.Inputs):
        left: Path = In(
            SourceType.Data, producer="__fan_left", output="out", description="The left result."
        )
        right: Path = In(
            SourceType.Pcf, producer="__fan_right", output="out", description="The right result."
        )

    def run(self) -> None:
        pass
