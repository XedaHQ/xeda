
[![CI Tests](https://github.com/XedaHQ/xeda/workflows/CI%20Tests/badge.svg?branch=dev)](https://github.com/XedaHQ/xeda/actions) [![Documentation Status](https://readthedocs.org/projects/xeda/badge/?version=latest)](https://xeda.readthedocs.io/en/latest/?badge=latest) ![visitors](https://page-views.glitch.me/badge?page_id=XedaHQ.xeda)

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



## Xeda Project File

Xeda design-specific descriptions and settings are organized through project files specified in [TOML](https://toml.io/). Every project contains one or more HDL designs. The default name for the project file is `xedaproject.toml`.

Sample `xedaproject.toml`:

```toml
[project]
name = "Project1"
description = "My Project with 2 designs"

[[design]]
name = 'Design1'
[design.rtl]
sources = [
    'src_rtl/module1.vhd',
    'src_rtl/top.v'
]
top = 'Top'
clock = 'clk'
[design.tb]
sources = [
    'top_tb.vhd',
]
top = 'TopTB'

[[design]]
name = 'Design2'
[design.rtl]
sources = [
    'src_rtl/module2.v',
    'src_rtl/top2.vhd'
]
top = 'Top2'
clock = 'clk'
[design.tb]
sources = [
    'cocoTestBench.py',
]
top = 'cocoTestBench'

```
Flow- or plugin-specific settings can also be stored in design or project sections.


- Design-specific settings in the current project file (`xedaproject.toml`)
- `--flow-settings` command line options

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


## Supported Flow Runners
- `fmax`: determine the maximum frequency of a design through a smart binary search
