Trivium has two YAML configurations:

- `trivium.yaml` preserves the former `trivium.toml` design, parameters, testbench and
  Vivado/Yosys settings. It leaves `G_SETUP_ROUNDS` at the HDL default of four rounds.
- `trivium-dc.xeda.yaml` preserves the former `trivium.xeda.yaml` variant. It explicitly
  sets `G_SETUP_ROUNDS: 4` and also provides Design Compiler target-library settings.
  Adjust the DC library path for your installation.

Both retain the 64-bit input/output configuration that the cocotb reference testbench uses.
Keeping the explicit generic in the DC variant preserves its existing design hash, while the
primary configuration retains the former TOML model and hash. TOML design files remain accepted
by Xeda; the published examples use YAML.
