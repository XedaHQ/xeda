# Open-source FPGA flows and `vivado_impl`

Open this file to build or program an FPGA with `yosys_fpga`, `nextpnr`, `fpga_pack` and
`openfpgaloader`, to target a bundled board, to constrain pins, or to implement a yosys netlist in
Vivado. Chains and bindings: `SKILL.md`. Failures: `troubleshooting.md`.

## The four flows

| Flow | Reads | Writes |
| --- | --- | --- |
| `yosys_fpga` | the design's HDL sources | `netlist` (JSON); `netlist_edif` (flat Xilinx synthesis only) |
| `nextpnr` | `netlist` (`yosys_fpga`'s, or exactly one `JsonNetlist` source); optional `constraints` (`Lpf`, `Pcf`, `Pdc`, `Xdc` sources); optional `sdc` (`Sdc` sources) | `config`: ECP5 text configuration, iCE40 `asc`, Nexus or Xilinx `fasm` |
| `fpga_pack` | `config` (`nextpnr`'s, or a typed `EcpConfig`, `IceAsc` or `Fasm` source) | `bitstream` (`outputs/<design>.<ext>`); programs nothing |
| `openfpgaloader` | `bitstream` (`fpga_pack`'s, or a typed `Bitstream` source) | nothing; programs the device and always runs |

`nextpnr` always writes its configuration (an ECP5 `out_of_context` run has none); a missing one
after the tool exits fails the run (`FlowFatalError`, naming the setting and path).

## Xilinx 7-series (openXC7 1.0)

Put openXC7's `bin` first on `PATH` (`yosys`, `nextpnr-himbaechel`, `fpga-as`). Give the full part:
device, package and speed grade.

```yaml
name: blinky
rtl:
  sources: ["blinky.v", "blinky.xdc"] # pins as a typed Xdc source
  top: blinky
flows:
  yosys_fpga:
    fpga.part: xc7a100tcsg324-1
  nextpnr:
    clock.port: clk
    clock.freq: 100MHz
```

```bash
xeda run fpga_pack blinky.yaml
```

This builds `outputs/blinky.bit` and programs nothing.

- The first build under a run root generates the die's chip database into
  `<run root>/.cache/xilinx-chipdb/` (about a minute and gigabytes of memory). Every later launch
  reuses it. `chipdb` names a database file instead; `prjxray_db` names another Project X-Ray
  root.
- openXC7 builds before `26f5e17a5` can write wrong bits with no error. See
  `troubleshooting.md`.

## Boards

`nextpnr`, `fpga_pack` and `openfpgaloader` take `board: <name>`; `yosys_fpga` and the Vivado flows
take `fpga.part`. A board gives the part and, when `rtl.sources` holds no pin file of the family,
the board's pin names. Names are lower case and match in any letter case. `xeda list-boards --json` lists each
board with its part, its pin file (`xdc` or `lpf`, under `xeda/data/`) and `openfpgaloader_board`.

| Board | Ports the design must use |
| --- | --- |
| `arty_a7_100t`, `arty_a7_35t` | Digilent names: `CLK100MHZ`, `led[0]`, `btn[0]`, `sw[0]`, ... |
| `basys_3` | `clk`, `led[15:0]`, `btnC` (reset), `btnU`, `btnL`, `btnR`, `btnD`, `RsRx`, `RsTx`; LEDs and buttons are active high |
| `stlv7325_v2` | `clk_p`, `clk_n` (differential: the design needs an `IBUFDS` and a `BUFG`), `led[7:0]`, `btn[1:0]` (`btn[0]` is reset), `uart_rx`, `uart_tx`; LEDs and buttons are active low |
| `ulx3s_85f` | the pins of its `board.lpf` (`clk_25mhz`, `btn[...]`, ...) |

The bundled Xilinx pin files have no `create_clock`: give the clock in the flow settings. The
`ulx3s_85f` file sets 25 MHz on `clk_25mhz`.

`custom_boards_file` replaces the bundled database with your own, TOML or YAML by suffix (`.toml`,
`.yaml`, `.yml`; any other suffix is an error). A relative path resolves against the design
directory, a board's `lpf` against the database file. Board names there are case-sensitive. An
entry has `fpga`, `lpf` or `xdc`, and optionally `openfpgaloader_board`. A flow that shares `board`
with a producer must share `custom_boards_file`.

## Pin and timing constraints

- List pin files in `rtl.sources` with their types (`Lpf`, `Pcf`, `Pdc`, `Xdc`). `nextpnr` merges
  the selected family's files in source order and falls back to the board's file when there are
  none. The settings `lpf_cfg`, `pcf_cfg` and `pdc_cfg` were removed.
- `Sdc` sources merge in source order, then the `sdc` setting's file; the result reaches
  `--sdc` once.
- A clock needs one timing authority across pin files, SDC files and settings. A duplicate fails and
  names the original files, lines and the clock setting. A clock port without a period is a
  declaration, not a constraint.

## Programming

`openfpgaloader` needs openFPGALoader 0.13.1 or newer; it asks `-V` before it builds or programs
anything and refuses an older loader. It loads SRAM by default.

| Setting | Effect |
| --- | --- |
| `write_flash` | Program flash. Needs a device (`fpga`, or a `board` that gives one). |
| `verify` | Verify the flash write; needs `write_flash`. |
| `board` | Pass `--board <openfpgaloader_board>`, with no `--fpga-part`, unless `cable` is set. |
| `cable` | Pass `--cable` instead of a board name, and `--fpga-part` when the part is known. |

With no board, a known part becomes `--fpga-part` (a Xilinx part without its speed grade); with
no part, the loader detects the device. The flow fails on a nonzero exit and also when
`openfpgaloader.log` shows the device was not programmed although the loader exited 0
(`ReportedFailure`, quoting the lines). A load that fails in another way passes: read the log.

## `vivado_impl`: Vivado implements the yosys netlist

```bash
xeda run yosys_fpga+vivado_impl blinky.yaml -s bitstream=$PWD/blinky.bit
```

- `vivado_impl` places and routes an EDIF netlist in non-project mode and runs no synthesis. Its
  `netlist` is `yosys_fpga`'s `netlist_edif` or one `{file: "top.edf", type: Edif}` source, which
  skips synthesis. A Verilog netlist is refused.
- It needs `rtl.top`, a Xilinx `fpga`, a clock and `Xdc` pin sources. A bitstream needs every port
  constrained; a one-bit bus (`input [0:0] a`) is the scalar port `a` in the netlist.
- Its outputs and switches are `vivado_alt_synth`'s: `write_checkpoint` gives `checkpoint_route`,
  `write_netlist` gives `netlist`, `write_timing_netlist` gives `netlist_timing` and `sdf`, and
  `bitstream` is written when set or when `openfpgaloader` follows. It has no `checkpoint_synth`.
  Its results are `vivado_synth`'s (`Fmax`, `wns`, `lut`, `ff`, ...).
- `yosys_fpga` writes the EDIF when a Xilinx synthesis is flat. `flatten: false`, `keep_hierarchy`
  (also as an attribute in `set_mod_attribute` or `set_attribute`), `black_box`, or
  `synth_pass_only` without `flatten: true` writes none, and planning refuses the chain naming the
  cause. A hierarchy that only the HDL (`(* keep_hierarchy *)`) or `rtl.attributes` asks for stops
  `vivado_impl` before Vivado starts and names the modules.
- An EDIF from elsewhere must be flat and written with `-pvector bra`; otherwise Vivado reads a
  bus with its range reversed and says nothing.
- To feed `vivado_postsynth_sim` or `vivado_power` from it, bind their inputs to `vivado_impl`.
  `yosys_fpga+vivado_impl+vivado_power` is refused until the three bindings are added (the chain
  binds the checkpoint):

```bash
xeda run yosys_fpga+vivado_impl+vivado_power <design.yaml> -s flows.vivado_postsynth_sim.inputs.netlist=vivado_impl.netlist flows.vivado_postsynth_sim.inputs.netlist_timing=vivado_impl.netlist_timing flows.vivado_postsynth_sim.inputs.sdf=vivado_impl.sdf
```
