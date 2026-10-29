# Examples

Each example has a design file that says what to build. The flows you run on it choose how. Run
the commands below from the root directory of the repository. Xeda writes the run directories to
`./xeda_run`. Add `--dry-run` to any `xeda run` command to see the plan without starting a tool.

The cocotb testbenches need the Python packages of the dependency group `examples`. From the root
of the repository, `uv sync` installs them, and so does `pip install --group examples` (pip 25.1 or
newer). The group also has `pyserial`, which the host test of
[`bluespec/uart_echo`](bluespec/uart_echo/README.md) and the hardware script of
[`bluespec/uart_link`](bluespec/uart_link/README.md) need. Both can use `pyftdi` too, which
you install yourself (`pip install pyftdi`). On Python 3.15, pip needs
`COCOTB_IGNORE_PYTHON_REQUIRES=1` to build cocotb:
see "Development" in the main README. A flow needs its own tool: GHDL, NVC, Verilator, Yosys,
nextpnr, Vivado and so on.

## VHDL

`vhdl/sqrt` is an iterative square root with a cocotb testbench. One design file serves two
simulators and three synthesis flows:

```bash
xeda run ghdl_sim examples/vhdl/sqrt/sqrt.yaml          # simulate with GHDL and run the cocotb tests
xeda run nvc examples/vhdl/sqrt/sqrt.yaml               # the same design and testbench with NVC
xeda run yosys_fpga examples/vhdl/sqrt/sqrt.yaml        # synthesize with Yosys
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml      # synthesize with Vivado
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s vivado_synth.clock.period=4ns   # change one setting
```

Other VHDL examples:

- `vhdl/pipeline/pipelined_adder.yaml`: a VHDL pipelined adder with a valid/ready interface.
- `vhdl/Trivium`: the Trivium stream cipher with a cocotb testbench that calls a C reference model.
  See its README.
- `vhdl/xedaproject.yaml`: a project file with one design, `full-adder`, and settings for several
  flows (`xeda run ghdl_sim --xedaproject examples/vhdl/xedaproject.yaml --design-name full-adder`).

## SystemVerilog and mixed languages

```bash
xeda run verilator examples/sv/fifo/fifo.xeda.yaml         # SystemVerilog testbench
xeda run verilator examples/sv/fifo/fifo_cocotb.xeda.yaml  # the same FIFO with a cocotb testbench
xeda run yosys_fpga examples/sv/fifo/fifo.xeda.yaml        # FPGA synthesis for a Xilinx 7-series part
xeda run yosys_sim examples/mixed_language/blink/blinky.xeda.yaml   # SystemVerilog design, C++ testbench, CXXRTL
```

## FPGA boards

`boards/ulx3s` has two projects for the ULX3S board (Lattice ECP5): `blinky`, with two designs,
and `dvi_test`. A project holds its designs and the settings of the flows. Each flow runs the
flows it needs first, so one command builds the bitstream:

```bash
xeda run yosys_fpga+nextpnr+fpga_pack --xedaproject examples/boards/ulx3s/blinky/xedaproject.yaml --design-name blinky --dry-run  # show the chain
xeda run fpga_pack --xedaproject examples/boards/ulx3s/blinky/xedaproject.yaml --design-name blinky  # build the bitstream
xeda run openfpgaloader --xedaproject examples/boards/ulx3s/blinky/xedaproject.yaml --design-name blinky  # build, then program
```

Only the last command touches hardware. Use `--design-name blinky-vhdl` for the VHDL version.
`blinky.xeda.yaml` and `blinky_vhdl.xeda.yaml` hold the same two designs as separate design
files, with fewer flow settings. The `dvi_test` project is a VGA video generator with a DVI encoder:

```bash
xeda run fpga_pack --xedaproject examples/boards/ulx3s/dvi_test/xedaproject.yaml --design-name dvi_test
```

## ASIC

`bluespec/gcd` has the settings for the ASIC chain on the platform nangate45. One design file
serves the simulation, the ASIC chain, Vivado and two FPGA targets. Xeda runs `bsc` first, to
convert the Bluespec design to Verilog. Yosys maps the Verilog to the cells of the platform, and
four OpenROAD flows place, route and finish it. Install the PDK files of the platform once, then
run the chain:

```bash
xeda pdk install nangate45                                      # download the PDK files that the platform pins
xeda run openroad examples/bluespec/gcd/gcd.yaml --dry-run      # show the six flows
xeda run openroad examples/bluespec/gcd/gcd.yaml                # run them: bsc, yosys and OpenROAD must be on PATH
```

The routed design and its reports are in `xeda_run/gcd/openroad`.

## Bluespec

`bluespec` has eight self-checking designs in Bluespec SystemVerilog and Bluespec Classic. See
[its README](bluespec/README.md).
