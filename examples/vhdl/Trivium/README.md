Trivium has two YAML configurations:

- `trivium.yaml` holds the design, parameters, testbench and Vivado/Yosys settings. It leaves
  `G_SETUP_ROUNDS` at the HDL default of four rounds.
- `trivium-dc.xeda.yaml` is a variant that sets `G_SETUP_ROUNDS: 4` explicitly and also provides
  Design Compiler target-library settings. Adjust the DC library path for your installation.

Both use the 64-bit input/output configuration that the cocotb reference testbench uses.
