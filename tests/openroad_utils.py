"""Helpers for the tests of the OpenROAD flows.

`openroad_design` writes a small design whose netlist uses the cells of the `tiny` platform
(`tiny_platform.py`). `openroad_settings` gives the settings that select that platform. A test of
the flows that are not registered yet registers them with `register_openroad_flows`.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from xeda.flow import registered_flows
from xeda.flows.openroad import Openroad, OpenroadCts, OpenroadGrt, OpenroadPlace

from .flow_registry import preserved_flow_registry
from .tiny_platform import tiny_settings

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
) -> Path:
    """Write a design `counter` under `root`; return its design file.

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
        "  top: counter\n"
        "  clock:\n"
        "    port: clk\n"
    )
    return design


def openroad_settings(store: Path, **given: Any) -> list[str]:
    """Return the `key=value` settings that select the `tiny` platform in `store`, then `given`.

    The platform settings are shared leaves, so one value reaches every flow of a chain. A key of
    `given` is a path that `flow_settings` reads, such as `flows.openroad_place.clocks`; write it
    with `**{...}` when it has dots."""
    settings = {**tiny_settings(store), **given}
    return [f"{key}={value}" for key, value in settings.items()]
