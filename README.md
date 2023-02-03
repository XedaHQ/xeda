
[![CI](https://github.com/XedaHQ/xeda/workflows/CI/badge.svg)](https://github.com/XedaHQ/xeda/actions?query=workflow%3ACI) [![Downloads](https://static.pepy.tech/personalized-badge/xeda?period=total&units=none&left_color=black&right_color=orange&left_text=Downloads)](https://pepy.tech/project/xeda) [![license](https://img.shields.io/github/license/XedaHQ/xeda)](https://github.com/XedaHQ/xeda/blob/master/LICENSE.txt) [![versions](https://img.shields.io/pypi/pyversions/xeda)](https://pypi.org/project/xeda) [![PyPI](https://img.shields.io/pypi/v/xeda)](https://pypi.org/project/xeda/)



[![Xeda Logo](https://raw.githubusercontent.com/XedaHQ/xeda/main/xeda.png?raw=true)](https://github.com/XedaHQ/xeda)


**Xeda** `/ˈziːdə/` is a cross-platform, cross-EDA, cross-target simulation and synthesis automation platform.
It assists hardware developers in verification, evaluation, and deployment of RTL designs. Xeda supports flows from multiple commercial and open-source electronic design automation suites.

**Xeda is the one tool to rule 'em all!**

For further details, visit the [Xeda's documentations](http://xeda.rtfd.io/) (Work In Progress).




## Installation
Python 3.8 or newer is required. To install the latest published version from [pypi](https://pypi.org/project/xeda) run:
```
python3 -m pip install -U xeda
```

### Development
```
git clone --recursive https://github.com/XedaHQ/xeda.git
cd xeda
python3 -m pip install -U --editable . --config-settings editable_mode=strict
```

## Usage
Run `xeda --help` to see a list of available commands and options.


### Design Description

Xeda design-specific descriptions and settings are organized through project files specified in [TOML](https://toml.io/). Every project contains one or more HDL designs. The default name for the project file is `xedaproject.toml`.

Sample Xeda design description [file](./examples/vhdl/sqrt/sqrt.toml):

```toml
name = "sqrt"
description = "Iterative computation of square-root of an integer"
language.vhdl.standard = "2008"

[rtl]
sources = ["sqrt.vhdl"]
top = "sqrt"
clock_port = "clk"
parameters = { G_IN_WIDTH = 32 }
# parameters = { G_IN_WIDTH = 32, G_ITERATIVE = true, G_STR= "abcd", G_BITVECTOR="7'b0101001" }

[tb]
sources = ["tb_sqrt.py"]
cocotb = true
# top = "tb_sqrt"  # FIXME

[flows.vivado_synth]
fpga.part = 'xc7a12tcsg325-1'
clock_period = 5.0
```

## Flows
A `Tool` is an abstraction for an executable which is responsible for one or several steps in an EDA flow. A `Tool` can be executed as a native binary already installed on the system, in a virtualized container (e.g. `docker`), or on a remote system.
A `Flow` is a collection of steps performed by one or several tools. A Xeda `Flow` implements the following methods:
- `init`(optional): initializations which need to happen after  the instantiation of a `Flow` instance. At this stage, the flow can specify and customize dependency flows, which will be run before execution of the flow. Seperation of this stage form Python `__init__` enables greater flexibility and more effective control over the execution of flows.
- `run`: main execution of the flow which includes generation of files, scripts, and tool arguments as well as execution of one or several tools. All dependencies have been already executed before `run` begins, and the completed dependencies (and their results and artifacts) will be available.
- `parse_results`(optional): evaluate and interpret generated reports or other artifacts.

### Supported Tools and Flows

- AMD-Xilinx [Vivado](https://www.xilinx.com/products/design-tools/vivado/vivado-ml.html) Design Suite
    - `vivado_synth`: FPGA synthesis and implementation.
    - `vivado_sim`: functional simulation of RTL design
    - `vivado_postsynthsim`: Post-implementation functional and timing simulation and power analysis
    - `vivado_power`: Post-implementation power estimation based on post-implementation timing simulation with real-world target testvectors
- AMD-Xilinx [ISE](https://www.xilinx.com/products/design-tools/ise-design-suite.html) Design Suite
- [GHDL](https://github.com/ghdl/ghdl) VHDL simulator
  - `ghdl_sim`
- Intel [Quartus Prime](https://www.intel.com/content/www/us/en/software/programmable/quartus-prime/overview.html) (Lite/Pro Editions):
  - `quartus`: FPGA synthesis and implementation flow
- Lattice Diamond
  - `diamond_synth`: FPGA synthesis and implementation flow
- Mentor (Siemens) [ModelSim](https://eda.sw.siemens.com/en-US/ic/modelsim/)
  - `modelsim` RTL and netlist simulation
- [nextpnr](https://github.com/YosysHQ/nextpnr) portable FPGA place and route tool
- [openFPGAloader](https://github.com/trabucayre/openFPGALoader): Open-source and multi-platform universal utility for programming FPGAs. Compatible with many boards, cables and FPGA from major manufacturers.
- [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD/): integrated chip physical design flow that takes a design from RTL sources to routed layout.
- Synopsys Design Compiler
- Synopsys VCS simulator
- [Verilator](https://github.com/verilator/verilator): the fastest (open-source) Verilog/SystemVerilog simulator.
- [Bluespec](https://github.com/B-Lang-org/bsc): Compiler, simulator, and tools for the Bluespec Hardware Description Language.
- [Yosys](https://github.com/YosysHQ/yosys) Open SYnthesis Suite (FPGA and ASICs synthesis)


Run `xeda list-flows` for the full list of supported flows in the installed version.
