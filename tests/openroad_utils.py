"""Helpers for the tests of the OpenROAD flows.

`openroad_design` writes a small design whose netlist uses the cells of the `tiny` platform
(`tiny_platform.py`). `openroad_settings` gives the settings that select that platform with the values of OpenROAD. A test of
the flows that are not registered yet registers them with `register_openroad_flows`.
"""

import json
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from xeda import Design
from xeda.design import SourceType
from xeda.flow import In, Out, registered_flows
from xeda.flows.openroad import Openroad, OpenroadCts, OpenroadGrt, OpenroadPlace, OpenroadStage

from .flow_registry import preserved_flow_registry
from .tiny_platform import TINY_OPENROAD_RESIDUE, tiny_settings, tiny_store
from .tool_utils import FAKE_TOOLS_DIR

#: The flows of the chain, in the order of their stages.
OPENROAD_FLOWS = (OpenroadPlace, OpenroadCts, OpenroadGrt, Openroad)

#: A 4-bit register with an inverter on each bit, mapped to the cells of the `tiny` library.
COUNTER_RTL = (
    "module counter(input clk, input [3:0] d, output reg [3:0] q);\n"
    "  always @(posedge clk) q <= ~d;\n"
    "endmodule\n"
)
COUNTER_NETLIST = "".join(
    [
        "module counter(clk, d, q);\n",
        "  input clk;\n",
        "  input [3:0] d;\n",
        "  output [3:0] q;\n",
        "  wire [3:0] n;\n",
        *(f"  INV_X1 inv{i} (.A(d[{i}]), .ZN(n[{i}]));\n" for i in range(4)),
        *(f"  DFF_X1 ff{i} (.D(n[{i}]), .CK(clk), .Q(q[{i}]));\n" for i in range(4)),
        "endmodule\n",
    ]
)


@contextmanager
def register_openroad_flows() -> Iterator[None]:
    """Register the four OpenROAD flows for the body, and restore the registry after it.

    A flow that is not complete is abstract, and Xeda does not register an abstract flow. Planning
    needs only the declarations, so this registers the classes as they are."""
    with preserved_flow_registry():
        for flow in OPENROAD_FLOWS:
            registered_flows[flow.name] = (flow.__module__, flow)
        yield


def openroad_design(
    root: Path,
    *,
    rtl: bool = False,
    sources: Sequence[tuple[str, str | None]] = (),
    top: str = "counter",
) -> Path:
    """Write a design `counter` under `root`; return its design file. The top module is `top`.

    The design lists the gate-level netlist `counter_netlist.v` as a source of type
    `VerilogNetlist`. With `rtl` true, it lists the RTL `counter.v` instead, so that a flow chain
    starts with synthesis. `sources` adds files, each a name and a type (`None` leaves the type
    to the suffix); a file that does not exist yet gets a one-line placeholder."""
    root.mkdir(parents=True, exist_ok=True)
    listed = []
    if rtl:
        (root / "counter.v").write_text(COUNTER_RTL)
        listed.append('"counter.v"')
    else:
        (root / "counter_netlist.v").write_text(COUNTER_NETLIST)
        listed.append('{file: "counter_netlist.v", type: VerilogNetlist}')
    for name, kind in sources:
        if not (root / name).exists():
            (root / name).write_text("x\n")
        listed.append(f'{{file: "{name}", type: {kind}}}' if kind else f'"{name}"')
    design = root / "design.yaml"
    design.write_text(
        "name: counter\n"
        "rtl:\n"
        f"  sources: [{', '.join(listed)}]\n"
        f"  top: {json.dumps(top)}\n"
        "  clocks: [{port: clk}]\n"
    )
    return design


def openroad_settings(store: Path, **given: Any) -> list[str]:
    """Return the `key=value` settings that select the `tiny` platform in `store`, then `given`.

    The platform settings are shared leaves, so one value reaches every flow of a chain. A key of
    `given` is a path that `flow_settings` reads, such as `flows.openroad_place.clocks`; write it
    with `**{...}` when it has dots."""
    settings = {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), **given}
    return [f"{key}={value}" for key, value in settings.items()]


# ---------------------------------------------------------------------------------------------
# Test flows over the shared base
# ---------------------------------------------------------------------------------------------


class Probes:
    """Two small OpenROAD flows that exist for the tests of the shared base. Their drivers are in
    `tests/templates/drivers/` and include the restart header and the procedures that the
    package ships. `netlist` reads a netlist, and `database` reads a database and its SDC file.
    Each runs one process and writes `probe.odb`, `metrics.json` and `reports/probe.rpt`. A test
    can set `before_run` on a class to a function of the flow, which runs just before the process
    starts, as a tool of an earlier run would have left its files."""

    def __init__(self, netlist: type[OpenroadStage], database: type[OpenroadStage]):
        self.netlist = netlist
        self.database = database

    def flow(
        self,
        tmp_path: Path,
        *,
        database: bool = False,
        settings: dict[str, Any] | None = None,
        design: Design | None = None,
        store_name: str = "store",
    ) -> OpenroadStage:
        """Return a flow of the `tiny` platform that is ready to render and run: its template
        helpers are added, its inputs are bound and `prepare_inputs()` has run. It is no launched
        flow, so its run directory is an `unlaunched` one under `tmp_path`. The PDK store is
        `tmp_path/store_name`. A `design` that the caller gives must list the netlist
        `counter_netlist.v` in its design root, as `openroad_design` writes it."""
        store = tiny_store(tmp_path, store_name)
        root = tmp_path / "design"
        if design is None:
            design = Design.load(openroad_design(root))
        cls = self.database if database else self.netlist
        given = {**tiny_settings(store, residue=TINY_OPENROAD_RESIDUE), **(settings or {})}
        run_path = tmp_path / "run" / cls.name
        run_path.mkdir(parents=True, exist_ok=True)
        flow = cls(
            cls.Settings.from_input(given, design_root=design.root, runner_cwd=tmp_path),
            design,
            run_path,
        )
        flow.init()
        if database:
            inputs = tmp_path / "inputs"
            inputs.mkdir(exist_ok=True)
            (inputs / "in.odb").write_text("odb\n")
            (inputs / "in.sdc").write_text("create_clock -name clk -period 10 [get_ports clk]\n")
            flow.inputs.odb = inputs / "in.odb"
            flow.inputs.sdc = inputs / "in.sdc"
        else:
            flow.inputs.netlist = design.root / "counter_netlist.v"
        flow.outputs.odb = run_path / "probe.odb"
        flow.prepare_inputs()
        return flow


@contextmanager
def probe_flows() -> Iterator[Probes]:
    """Define the probe flows, and register them for the body. The registry is as it was after."""
    with preserved_flow_registry():

        class TOpenroadProbe(OpenroadStage):
            """A netlist in, a database out."""

            results_description = {}
            metrics_files = ("metrics.json",)

            class Inputs(OpenroadStage.Inputs):
                netlist: Path = In(
                    SourceType.VerilogNetlist,
                    producer="yosys",
                    output="netlist",
                    description="The netlist.",
                )

            class Outputs(OpenroadStage.Outputs):
                odb: Path = Out(SourceType.OpenroadDbPlaced, description="The database.")

            before_run: Callable[[OpenroadStage], None] | None = None

            def run(self) -> None:
                self.outputs.odb = self.run_path / "probe.odb"
                if self.before_run is not None:
                    self.before_run()
                self.run_openroad(
                    "openroad_probe",
                    "metrics.json",
                    [self.outputs.odb, self.failed_odb, "reports/probe.rpt"],
                )

        class TOpenroadProbeDb(OpenroadStage):
            """A database and its SDC file in, a database out."""

            results_description = {}
            metrics_files = ("metrics.json",)

            class Inputs(OpenroadStage.Inputs):
                odb: Path = In(SourceType.OpenroadDbPlaced, description="The database.")
                sdc: Path = In(
                    SourceType.OpenroadSdcPlaced,
                    same_producer_as="odb",
                    paired=True,
                    description="Its SDC file.",
                )

            class Outputs(OpenroadStage.Outputs):
                odb: Path = Out(SourceType.OpenroadDbCts, description="The database.")

            before_run: Callable[[OpenroadStage], None] | None = None

            def run(self) -> None:
                self.outputs.odb = self.run_path / "probe.odb"
                if self.before_run is not None:
                    self.before_run()
                self.run_openroad(
                    "openroad_probe_db",
                    "metrics.json",
                    [self.outputs.odb, self.failed_odb, "reports/probe.rpt"],
                )

        yield Probes(TOpenroadProbe, TOpenroadProbeDb)


def render_driver(flow: OpenroadStage, driver: str) -> str:
    """Return the script that `run_openroad` would write for `driver`, without writing it."""
    flow.bind_template_values()
    template = flow.jinja_env.get_template(f"drivers/{driver}.tcl.j2")
    return template.render(settings=flow.settings, design=flow.design, artifacts=flow.artifacts)


def run_fake_openroad(
    work: Path, script: str, *, driver: str = "openroad_probe", metrics: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the fake OpenROAD on `script` (a file `<driver>.tcl`) in `work`, as the flows do."""
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{driver}.tcl").write_text(script)
    command = [
        sys.executable,
        str(FAKE_TOOLS_DIR / "openroad"),
        "-no_init",
        "-no_splash",
        "-threads",
        "2",
        "-exit",
    ]
    if metrics is not None:
        command += ["-metrics", metrics]
    command.append(f"{driver}.tcl")
    return subprocess.run(command, cwd=work, capture_output=True, text=True, check=False)
