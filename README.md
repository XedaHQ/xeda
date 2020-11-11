[![Documentation Status](https://readthedocs.org/projects/xeda/badge/?version=latest)](https://xeda.readthedocs.io/en/latest/?badge=latest)

# Xeda

## Description
**Xeda** `/ˈziːdə/` is a cross-platform, cross-EDA, cross-target simulation and synthesis automation platform.
It assists hardware developers in verification, evaluation, and deployment of RTL designs. Xeda supports flows from multiple commercial and open-source electronic design automation suites.

For further details, visit the [Xeda's documentations](http://xeda.rtfd.io/) (Work In Progress).


## Definitions
- Tool: Single executable that performs an EDA action.
- Suite: A collection of Tools.
- Flow: Execution of chain of tools from one or several suites. 
- Flow dependencies:
    Dependencies are managed by the use of design-flow hash (DFH). DFH is a combined cryptographic hash of the content of dependency files (e.g. HDL sources) as well as design and flow settings. The directory where the flow is run and the results are created is based on this design design-flow hash.

## Dependencies
- Python 3.6.9+ (tested on cpython)

## Installation
- Install from GitHub's master branch (recommended during alpha development):
```
python3 -m pip install -U git+https://github.com/kammoh/xeda.git
```

- Install the latest published version from [pypi](https://pypi.org/project/xeda):
```
python3 -m pip install -U xeda
```

- Install from local git clone (with symlinks):
```
git clone --recursive https://github.com/kammoh/xeda.git
cd xeda
python3 -m pip install -U -e .
```



## Usage

## Configurations

Settings override in the following order, from lower to higher priority:
- System-wide `default.json` in `<DATA_DIR>/config/xeda/defaults.json`
- Design-specific settings in the current project (`xedaproject.toml`)
- `--override-settings` command line options

Sample `xedaproject.toml`:

```toml

```

entries in `design.sources` are either a dictionary structure, resembling the `DesignSource` class fields or a string which is either an absolute path or relative to the location of the current working directory.

### Design parameters
- "vhdl_std": Can be "93" (VHDL-1993), "02" (VHDL-2002 or default language version of the tool), "08" (VHDL-2008). 

## Supported Flows

- Xilinx® Vivado® Design Suite
    - synth: Full synthesis and implementation flow
    - sim: functional simulation of RTL design
    - post-synth-sim: Post-implementation functional and timing simulation and power analysis.
- Lattice® Diamond®
    - synth: Full synthesis and implementation flow
- Intel® Quartus® Prime Lite/Pro:
    - synth: Full synthesis and implementation flow
    - dse: Design Space Exploration

- GHDL
    - sim: VHDL simulation

<!-- ## Adding new Flows -->

© 2020 [Kamyar Mohajerani](mailto:kamyar@ieee.org)