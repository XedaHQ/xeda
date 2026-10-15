# Examples

Each example has a design file that says what to build. The flows you run on it choose how. Run
the commands below from the directory of the example. Add `--dry-run` to any `xeda run` command to
see the plan without starting a tool.

The cocotb testbenches need `pip install -r examples/requirements.txt`, which you run from the
root of the repository. A flow needs its own tool: GHDL, NVC, Verilator, Yosys, nextpnr, Vivado and so on.

## VHDL

`vhdl/sqrt` is an iterative square root with a cocotb testbench. One design file serves two
simulators and three synthesis flows:

```bash
cd examples/vhdl/sqrt
xeda run ghdl_sim sqrt.yaml          # simulate with GHDL and run the cocotb tests
xeda run nvc sqrt.yaml               # the same design and testbench with NVC
xeda run yosys_fpga sqrt.yaml        # synthesize with Yosys
xeda run vivado_synth sqrt.yaml      # synthesize with Vivado
xeda run vivado_synth sqrt.yaml -s clock.period=4ns   # change one setting for this run
```

Other VHDL examples:

- `vhdl/pipeline/pipelined_adder.yaml`: a VHDL pipelined adder with a valid/ready interface.
- `vhdl/Trivium`: the Trivium stream cipher with a cocotb testbench that calls a C reference model.
  See its README.
- `vhdl/xedaproject.yaml`: a project file with one design, `full-adder`, and settings for several
  flows (`xeda run ghdl_sim --design-name full-adder`).

## SystemVerilog and mixed languages

```bash
cd examples/sv/fifo
xeda run verilator fifo.xeda.yaml         # SystemVerilog testbench
xeda run verilator fifo_cocotb.xeda.yaml  # the same FIFO with a cocotb testbench

cd ../../mixed_language/blink
xeda run yosys_sim blinky.xeda.yaml       # SystemVerilog design, C++ testbench, CXXRTL
```

## FPGA boards

`boards/ulx3s` has two projects for the ULX3S board (Lattice ECP5): `blinky`, with two designs,
and `dvi_test`. A project holds its designs and the settings of the flows. Each flow runs the
flows it needs first, so one command builds the bitstream:

```bash
cd examples/boards/ulx3s/blinky
xeda run yosys_fpga+nextpnr+fpga_pack --design-name blinky --dry-run   # show the chain
xeda run fpga_pack --design-name blinky                                 # build the bitstream
xeda run openfpgaloader --design-name blinky                            # build, then program
```

Only the last command touches hardware. Use `--design-name blinky-vhdl` for the VHDL version.
`blinky.xeda.yaml` and `blinky_vhdl.xeda.yaml` hold the same two designs as separate design
files, with fewer flow settings. The
`dvi_test` project is a VGA video generator with a DVI encoder:

```bash
cd examples/boards/ulx3s/dvi_test
xeda run fpga_pack --design-name dvi_test
```

## Bluespec

`bluespec` has five self-checking designs in Bluespec SystemVerilog and Bluespec Classic. See
[its README](bluespec/README.md).
