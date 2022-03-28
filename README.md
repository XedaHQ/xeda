
[![CI](https://github.com/XedaHQ/xeda/workflows/CI/badge.svg)](https://github.com/XedaHQ/xeda/actions?query=event%3Apush+branch%3Adev+workflow%3ACI) [![Downloads](https://static.pepy.tech/personalized-badge/xeda?period=total&units=none&left_color=black&right_color=orange&left_text=Downloads)](https://pepy.tech/project/xeda) ![visitors](https://page-views.glitch.me/badge?page_id=XedaHQ.xeda) [![license](https://img.shields.io/github/license/XedaHQ/xeda)](https://github.com/XedaHQ/xeda/blob/master/LICENSE.txt) [![versions](https://img.shields.io/pypi/pyversions/xeda?color=lightgray)](https://pypi.org/project/xedac) ![PyPI](https://img.shields.io/pypi/v/xeda?color=lightgray)



![Xeda Logo](logo.svg)


**Xeda** `/ˈziːdə/` is a cross-platform, cross-EDA, cross-target simulation and synthesis automation platform.
It assists hardware developers in verification, evaluation, and deployment of RTL designs. Xeda supports flows from multiple commercial and open-source electronic design automation suites.

**Xeda is the one tool to rule 'em all!**

For further details, visit the [Xeda's documentations](http://xeda.rtfd.io/) (Work In Progress).




## Installation

Requires Python >= 3.6.9

- Install from GitHub's master branch (recommended during alpha development):
```
python3 -m pip install -U git+https://github.com/XedaHQ/xeda.git
```

- Install the latest published version from [pypi](https://pypi.org/project/xeda):
```
python3 -m pip install -U xeda
```

### Development
```
git clone --recursive https://github.com/XedaHQ/xeda.git
cd xeda
python3 -m pip install -U -e .
```



## Design Description

Xeda design-specific descriptions and settings are organized through project files specified in [TOML](https://toml.io/). Every project contains one or more HDL designs. The default name for the project file is `xedaproject.toml`.

Sample Xeda design description [file](./examples/vhdl/sqrt/sqrt.toml):

```toml
name = 'sqrt'
description = 'Integer Square Root'
language.vhdl.standard = "2008"

[rtl]
sources = ['sqrt.vhdl']
top = 'sqrt'
clock_port = 'clk'
parameters = { G_IN_WIDTH = 32 }
# parameters = { G_IN_WIDTH = 32, G_ITERATIVE = true, G_STR= "abcd", G_BITVECTOR="7'b0101001" }

[tb]
sources = ['tb/tb_sqrt.py']
cocotb = true
top = 'tb_sqrt'
```


## Supported Flows

- Xilinx® Vivado® Design Suite
    - `vivado_synth`: Full synthesis and implementation flow. Supported strategies:
      - `Timing`, `Timing2`, `Timing3`: Optimize for timing performance
      - `Area`: Optimize the flow for lowest resource usage
      - `Runtime`: Quick run of implementation flow
      - `Debug`: Quick flow keeping details of the design hierarchy, suitable for debugging post-synthesis issues
    - `vivado_sim`: functional simulation of RTL design
    - `vivado_postsynth_sim`: Post-implementation functional and timing simulation and power analysis
    - `vivado_power`: Post-implementation power estimation based on post-implementation timing simulation with real-world target testvectors
- Lattice® Diamond®
    - synth: Full synthesis and implementation flow
- Intel® Quartus® Prime Lite/Pro:
    - synth: Full synthesis and implementation flow

- Synopsys Design Compiler
- Synopsys VCS simulator
- Mentor ModelSim

Open Source Tool Support:
- GHDL
- Verilator
- Yosys
- nextpnr, prjtrellis, and openFPGAloader

<!-- 
## Supported Flow Runners
- `fmax`: determine the maximum frequency of a design through a smart binary search -->

## What's Under the Hood?
### Settings as Dataclasses
