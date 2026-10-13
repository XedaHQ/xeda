# Trivium

Trivium has two design files:

- `trivium.yaml` holds the design, its parameters, the testbench and the settings for Vivado and
  Yosys. It leaves `G_SETUP_ROUNDS` at the HDL default of four rounds.
- `trivium-dc.xeda.yaml` is a variant. It sets `G_SETUP_ROUNDS: 4` and adds the target libraries
  for Design Compiler (`dc`). Change the library path to match your installation.

Both use the 64-bit input and output configuration of the cocotb reference testbench.

```bash
cd examples/vhdl/Trivium
xeda run ghdl_sim trivium.yaml           # simulate with GHDL
xeda run yosys_fpga trivium.yaml         # synthesize with Yosys for the Xilinx 7 series part
```
