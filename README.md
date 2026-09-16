[![CI](https://github.com/XedaHQ/xeda/workflows/CI/badge.svg)](https://github.com/XedaHQ/xeda/actions?query=workflow%3ACI) [![Downloads](https://static.pepy.tech/personalized-badge/xeda?period=total&units=none&left_color=black&right_color=orange&left_text=Downloads)](https://pepy.tech/project/xeda) [![license](https://img.shields.io/github/license/XedaHQ/xeda)](https://github.com/XedaHQ/xeda/blob/main/LICENSE.txt) [![versions](https://img.shields.io/pypi/pyversions/xeda)](https://pypi.org/project/xeda) [![PyPI](https://img.shields.io/pypi/v/xeda)](https://pypi.org/project/xeda/)

[![Xeda Logo](https://raw.githubusercontent.com/XedaHQ/xeda/main/xeda.png?raw=true)](https://github.com/XedaHQ/xeda)

**Xeda** is a cross-platform automation framework for RTL simulation, FPGA synthesis
and implementation, and ASIC synthesis and physical design. A single declarative design file can
drive commercial and open-source EDA tools without duplicating tool-specific build scripts.

Read the [Xeda documentation](https://xeda.readthedocs.io/) for the complete design-file, flow,
and run-directory references.

## Installation

Python 3.11 or newer is required. Xeda orchestrates EDA tools but does not install them; the tools
needed by a selected flow must be available locally, in Docker, or on the configured remote host.

For CLI use, the preferred installation is an isolated [`uv`](https://docs.astral.sh/uv/)
tool environment:

```bash
uv tool install --upgrade xeda
```

[`pipx`](https://pipx.pypa.io/stable/) is an equivalent alternative:

```bash
pipx install --force xeda
```

To import Xeda as a Python library, install it in your project's virtual environment:

```bash
python3 -m pip install -U xeda
```

Verify the installation with `xeda --version`.

### Development

```bash
git clone --recursive https://github.com/XedaHQ/xeda.git
cd xeda
uv sync
uv run pytest tests/
```

An editable pip installation also works if `uv` is unavailable.

## Usage

The CLI is self-describing. Discover the installed flows and their exact contracts before running
one:

```bash
xeda list-flows                   # every flow, with its aliases, category and dependencies
xeda list-settings vivado_synth   # every setting of a flow, with type, default and meaning
xeda list-results vivado_synth    # the keys that flow writes to results.json
xeda design-schema                # JSON Schema of a design description file
xeda run vivado_synth sqrt.toml -s clock_period=5.0
```

Flow names accept snake case, dashes, CamelCase class names, and documented aliases. Setting
names are exact; unknown settings fail instead of being silently ignored.

### Scripting and coding agents

The query commands above accept `--json` for machine-readable output. The `run`, `dse`, and
`scrub` commands also accept it; tool output and logs then move to stderr so stdout contains one
parseable JSON document:

```bash
xeda run vivado_synth sqrt.toml --json | jq '.results.Fmax'
```

Xeda also ships a coding-agent skill whose flow catalog is generated from the installed version:

```bash
xeda skill install                # writes ./.claude/skills/xeda/
```

### Design description

A design file describes what to build: sources in compilation order, the top level, parameters,
logical clock ports, and an optional testbench. Tool constraints remain per-flow settings. TOML,
YAML, and JSON are accepted; paths resolve relative to the design file.

For example, [`examples/vhdl/sqrt/sqrt.toml`](./examples/vhdl/sqrt/sqrt.toml):

```toml
name = "sqrt"
description = "Iterative computation of square-root of an integer"
language.vhdl.standard = "2008"

[rtl]
sources = ["sqrt.vhdl"]
top = "sqrt"
clock_port = "clk"
parameters = { G_IN_WIDTH = 32 }

[tb]
sources = ["tb_sqrt.py"]
cocotb = true

[flows.vivado_synth]
fpga.part = "xc7a100tftg256-2L"
clock_period = 5.0
```

Use `xeda design-schema` for the authoritative input schema.

## Flows

A flow describes how to build or test a design with one or more tools. Dependencies run
automatically. For example, `nextpnr` runs its `yosys_fpga` synthesis dependency before place and
route. Results, generated scripts, reports, and effective settings are kept under `xeda_run/`.

### Supported Tools and Flows

- AMD-Xilinx [Vivado](https://www.xilinx.com/products/design-tools/vivado/vivado-ml.html) Design Suite
  - `vivado_synth`: FPGA synthesis and implementation in non-project (batch) mode, driving
    `synth_design` through `route_design` from a generated TCL script
  - `vivado_project`: the project-based equivalent of `vivado_synth`
  - `vivado_alt_synth`: an alternative TCL-based synthesis and implementation script
  - `vivado_sim`: functional simulation of an RTL design with the Vivado simulator (`xsim`)
  - `vivado_postsynth_sim`: post-synthesis and post-implementation simulation of the generated
    netlist, optionally annotated with timing from an SDF file
  - `vivado_power`: post-implementation power estimation from the real switching activity of a
    timing-annotated netlist simulation, rather than a vectorless estimate
- AMD-Xilinx [ISE](https://www.xilinx.com/products/design-tools/ise-design-suite.html) Design Suite
  - `ise_synth`: FPGA synthesis and implementation for older Xilinx device families
- [Bluespec](https://github.com/B-Lang-org/bsc): compiler, simulator, and tools for the Bluespec
  Hardware Description Language
  - `bsc`: compiles BSV/BH sources to Verilog for any downstream synthesis or simulation flow
- [GHDL](https://github.com/ghdl/ghdl) VHDL simulator
  - `ghdl_sim` (alias: `ghdl`): VHDL simulation
  - `ghdl_synth`: VHDL elaboration through `ghdl --synth`; for general-purpose synthesis prefer
    `yosys`, which handles VHDL, Verilog and mixed-language designs
- Intel [Quartus Prime](https://www.intel.com/content/www/us/en/software/programmable/quartus-prime/overview.html) (Lite/Pro Editions)
  - `quartus`: FPGA synthesis and implementation flow
- Lattice Diamond
  - `diamond_synth`: synthesis, place & route and bitstream generation for Lattice devices
- [NVC](https://github.com/nickg/nvc) VHDL simulator
  - `nvc`: VHDL simulation
- Siemens (Mentor) [ModelSim](https://eda.sw.siemens.com/en-US/ic/modelsim/)
  - `modelsim`: RTL and gate-level netlist simulation of VHDL, Verilog, SystemVerilog and
    mixed-language designs, with optional SDF timing annotation
- [nextpnr](https://github.com/YosysHQ/nextpnr) portable FPGA place and route tool
  - `nextpnr`: places and routes the netlist produced by its `yosys_fpga` dependency and reports
    achieved frequency, slack and utilization. Lattice ECP5 is the supported and tested target;
    other backends are best-effort and report raw per-bel-type counts
  - `open_xc7` (alias: `openxc7`): Xilinx 7-series place and route via nextpnr-xilinx
- [openFPGALoader](https://github.com/trabucayre/openFPGALoader): open-source and multi-platform
  universal utility for programming FPGAs, compatible with many boards, cables and FPGAs from
  major manufacturers
  - `openfpgaloader`: runs the full `yosys_fpga` -> `nextpnr` chain, packs the routed design into
    a bitstream, and loads it onto the board. The only flow that touches real hardware
- [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/): integrated chip physical design
  flow that takes a design from RTL sources to routed layout
  - `openroad`: ASIC implementation on top of a `yosys` synthesis dependency, against the bundled
    PDKs (`xeda list-platforms`)
- Synopsys Design Compiler
  - `dc`: ASIC logic synthesis
- Synopsys VCS simulator
  - `vcs`: Verilog/SystemVerilog simulation
- [Verilator](https://github.com/verilator/verilator): the fastest (open-source)
  Verilog/SystemVerilog simulator
  - `verilator`: compiles the design to C++/SystemC and builds a native executable. Supports
    cocotb testbenches, plain C++/SystemC harnesses, and VCD/FST waveform tracing
- [Yosys](https://github.com/YosysHQ/yosys) Open SYnthesis Suite
  - `yosys`: ASIC and generic gate/LUT synthesis
  - `yosys_fpga`: FPGA synthesis; the dependency of `nextpnr`, `open_xc7` and `openfpgaloader`
  - `yosys_sim`: simulation with CXXRTL

Run `xeda list-flows` for the complete list in the installed version; use
`xeda list-settings <flow>` and `xeda list-results <flow>` for the exact inputs and outputs.
