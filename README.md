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

- AMD-Xilinx [Vivado](https://www.xilinx.com/products/design-tools/vivado/vivado-ml.html):
  `vivado_synth`, `vivado_sim`, `vivado_postsynth_sim`, `vivado_power`, `vivado_project`
- AMD-Xilinx [ISE](https://www.xilinx.com/products/design-tools/ise-design-suite.html) Design Suite
- [GHDL](https://github.com/ghdl/ghdl), NVC, ModelSim, VCS, Verilator, and Vivado simulation
- Intel [Quartus Prime](https://www.intel.com/content/www/us/en/software/programmable/quartus-prime/overview.html)
- Lattice Diamond
- [Yosys](https://github.com/YosysHQ/yosys), [nextpnr](https://github.com/YosysHQ/nextpnr),
  OpenXC7, and [openFPGALoader](https://github.com/trabucayre/openFPGALoader)
- [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/) and Synopsys Design Compiler
- [Bluespec](https://github.com/B-Lang-org/bsc)

Run `xeda list-flows` for the complete list in the installed version; use
`xeda list-settings <flow>` and `xeda list-results <flow>` for the exact inputs and outputs.
