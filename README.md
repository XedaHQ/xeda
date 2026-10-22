[![CI](https://github.com/XedaHQ/xeda/workflows/CI/badge.svg)](https://github.com/XedaHQ/xeda/actions?query=workflow%3ACI) [![Downloads](https://static.pepy.tech/personalized-badge/xeda?period=total&units=none&left_color=black&right_color=orange&left_text=Downloads)](https://pepy.tech/project/xeda) [![license](https://img.shields.io/github/license/XedaHQ/xeda)](https://github.com/XedaHQ/xeda/blob/main/LICENSE.txt) [![versions](https://img.shields.io/pypi/pyversions/xeda)](https://pypi.org/project/xeda) [![PyPI](https://img.shields.io/pypi/v/xeda)](https://pypi.org/project/xeda/)

[![Xeda Logo](https://raw.githubusercontent.com/XedaHQ/xeda/main/xeda.png?raw=true)](https://github.com/XedaHQ/xeda)

**Xeda** automates simulation, synthesis and implementation with commercial and open-source EDA
tools. One declarative design file drives all of them: you switch tools by naming another flow.

Read the [Xeda documentation](https://xeda.readthedocs.io/) for the complete references of the
design file, the flows and the run directories.

## Installation

Xeda supports every Python release series that has not reached end of life. Currently, that means
Python 3.11 through 3.15. Xeda drives EDA tools but does not install them. The tools that a flow
needs must be available locally, in Docker, or on the remote host.

For command-line use, install Xeda in an isolated [`uv`](https://docs.astral.sh/uv/) tool
environment:

```bash
uv tool install --upgrade xeda
```

[`pipx`](https://pipx.pypa.io/stable/) does the same:

```bash
pipx install --force xeda
```

To import Xeda as a Python library, install it in the virtual environment of your project:

```bash
python3 -m pip install -U xeda
```

Check the installation with `xeda --version`.

## Usage

### Design file

A design file says what to build: the sources in compilation order, the top module, parameters,
the clock port and, optionally, a testbench. The settings of one tool go in a section for its
flow. Paths are relative to the design file. YAML is the recommended format. TOML and JSON also
work.

This is [`examples/vhdl/sqrt/sqrt.yaml`](./examples/vhdl/sqrt/sqrt.yaml):

```yaml
name: sqrt
description: Iterative computation of the square root of an integer
language.vhdl.standard: 2008
rtl:
  sources:
    - "sqrt.vhdl"
  top: sqrt
  clock.port: clk
  parameters.G_IN_WIDTH: 32
tb:
  sources:
    - "tb_sqrt.py"
flows:
  yosys_fpga:
    fpga.part: xc7a100tftg256-2L
    clock.freq: 100 MHz
  vivado_synth:
    fpga.part: xc7a100tftg256-2L
    clock.period: 5 ns
  vivado_alt_synth:
    fpga.part: xc7a100tftg256-2L
    synth.strategy: ExtraTiming
    impl.strategy: ExtraTimingCongestion
    clock.period: 5 ns
```

A dotted key stands for a nested table: `clock.period: 5 ns` is a `clock` table with the key
`period`. The command line uses the same keys, for example `-s clock.period=4.5ns`.

A `.py` testbench source makes the testbench a cocotb one. The clock period is a setting of each
flow, because it constrains one build.

Run `xeda design-schema` for the JSON Schema of a design file. YAML uses the 1.2 core schema. It
rejects duplicate keys and keys that are not text. Quote values that must stay text, such as
`"010"`, `"0x1F"` and `"1e3"`. The words `yes`, `no`, `on` and `off` are text. A boolean setting
accepts only `true` or `false`, so `debug: yes` and `debug: 1` are errors.

### Running flows

The same design file serves every flow:

```bash
xeda run ghdl_sim examples/vhdl/sqrt/sqrt.yaml          # simulate with GHDL and run the cocotb tests
xeda run nvc examples/vhdl/sqrt/sqrt.yaml               # the same design and testbench with NVC
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml      # synthesize and implement with Vivado
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=4.5ns   # change one setting
xeda dse vivado_synth --design examples/vhdl/sqrt/sqrt.yaml   # search for the highest clock frequency
```

A flow whose inputs and outputs are declared can follow another flow. A chain joins them with
`+`, and `--dry-run` shows the plan without starting a tool. This chain builds the bitstream of
the ULX3S example, in `examples/boards/ulx3s/blinky`, and programs no hardware:

```bash
xeda run yosys_fpga+nextpnr+fpga_pack examples/boards/ulx3s/blinky/blinky.xeda.yaml --dry-run
```

Flow names accept snake case, dashes, CamelCase class names and the documented aliases. Setting
names are exact: an unknown setting is an error.

### Remote runs

`--remote HOST` runs the whole request on a Linux machine over SSH. `HOST` is any destination that
`ssh` accepts. The host needs no Xeda: the first run installs it, and the results come back to your
run root.

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --remote <host>
```

See the [remote runs](https://xeda.readthedocs.io/en/latest/remote.html) page for the setup,
`xeda remote check` and runs that keep going after you disconnect.

### Finding out what is available

```bash
xeda list-flows                   # every flow: category, what it takes and makes, what follows it
xeda list-settings vivado_synth   # every setting of a flow, with type, default and meaning
xeda list-results vivado_synth    # the metrics that flow writes, with types and units
xeda design-schema                # JSON Schema of a design file
```

### Scripting and coding agents

The query commands accept `--json`. So do `run`, `dse` and `scrub`: they then write tool output
and logs to stderr, and stdout holds one JSON document:

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --json | jq '.results.fmax'
```

Xeda also ships a skill for coding agents. Its flow catalog comes from the installed version:

```bash
xeda skill install                # writes ./.claude/skills/xeda/
```

## Flows

A flow is a named sequence of steps that one or more tools perform. A flow can need the output
of another flow, its producer, and Xeda runs the producer first. For example, `nextpnr` needs the
netlist of `yosys_fpga`, so it runs `yosys_fpga` before place and route. A flow runs again only
when its sources, settings, tools or outputs changed since its last successful run. Results,
generated scripts, reports and the effective settings are in the run root, `xeda_run/`.

### Supported Tools and Flows

- AMD-Xilinx [Vivado](https://www.xilinx.com/products/design-tools/vivado/vivado-ml.html) Design Suite
  - `vivado_synth`: FPGA synthesis and implementation in project mode, run in batch
  - `vivado_alt_synth`: the same in non-project mode, running `synth_design` through
    `route_design` from a generated TCL script
  - `vivado_impl`: place and route of an EDIF netlist in non-project mode, with no synthesis: the
    netlist `yosys_fpga` writes for a Xilinx device (`yosys_fpga+vivado_impl`), or an EDIF source
  - `vivado_project`: creates a Vivado project for the design, to work on in Vivado (`gui: true`
    opens it)
  - `vivado_sim`: functional simulation of an RTL design with the Vivado simulator (`xsim`)
  - `vivado_postsynth_sim`: simulation of the testbench on the netlist that `vivado_synth` writes,
    optionally with timing from its SDF file
  - `vivado_power`: post-implementation power estimation from the real switching activity of a
    timing-annotated netlist simulation, rather than a vectorless estimate
- AMD-Xilinx [ISE](https://www.xilinx.com/products/design-tools/ise-design-suite.html) Design Suite
  - `ise_synth`: FPGA synthesis and implementation for older Xilinx device families
- [Bluespec](https://github.com/B-Lang-org/bsc) compiler (bsc 2026.07.1 or newer), for designs in
  Bluespec SystemVerilog (BSV) and Bluespec Classic (BH)
  - `bsc`: compiles the design to Verilog, and collects the Verilog files that its top module
    needs (including the bsc library modules it instantiates) for a later synthesis or simulation
    flow
  - `bsc_sim`: compiles a Bluespec testbench and simulates it with Bluesim, or with Verilator,
    Icarus Verilog or another Verilog simulator through bsc's own Verilog link step
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
  - `nextpnr`: places and routes the netlist that `yosys_fpga` writes, and reports achieved
    frequency, slack and utilization. The tested device families are Lattice ECP5 and iCE40, and
    Xilinx 7-series through nextpnr-himbaechel of openXC7. Nexus is best-effort. Families other
    than ECP5 and 7-series report only the bel-type counts
  - `fpga_pack`: packs the routed configuration that `nextpnr` records (or a configuration source
    of the matching type) into a bitstream, with ecppack, icepack or fpga-as of openXC7. It
    programs nothing
- [openFPGALoader](https://github.com/trabucayre/openFPGALoader): open-source and multi-platform
  universal utility for programming FPGAs, compatible with many boards, cables and FPGAs from
  major manufacturers
  - `openfpgaloader`: loads a bitstream onto the board: the one that `fpga_pack` builds after
    `yosys_fpga` and `nextpnr`, or a bitstream source. It is the only flow that touches hardware
- [OpenROAD](https://github.com/The-OpenROAD-Project/OpenROAD) open-source ASIC place and route
  - `openroad_place`: floorplan, power grid, placement and timing repair of the gate-level
    netlist that `yosys` writes for a standard-cell platform, with the constraints of the design
    and its clocks. It writes the placed database and its SDC file
  - `openroad_cts`: clock tree synthesis, legalization and timing repair of the placed database.
    It writes the database and its SDC file, with propagated clocks
  - `openroad_grt`: pin access, global route, repair of the design, the timing and the antenna
    violations. It writes the database, its SDC file and the route guides
  - `openroad`: detailed route, antenna repair and filler cells of the globally routed database,
    then the final database, SDC file, DEF file, netlist and parasitics, with the timing, power
    and IR drop report. It checks timing, design rule violations and antenna violations. It can
    also merge the GDSII layout with KLayout and save images of the design
- Synopsys Design Compiler
  - `dc`: ASIC logic synthesis
- Synopsys VCS simulator
  - `vcs`: Verilog/SystemVerilog simulation
- [Verilator](https://github.com/verilator/verilator): open-source Verilog/SystemVerilog simulator
  - `verilator`: compiles the design to C++ or SystemC and builds a native executable. It
    supports cocotb testbenches, plain C++ or SystemC harnesses, and VCD, FST and SAIF waveform
    tracing
- [Yosys](https://github.com/YosysHQ/yosys) Open SYnthesis Suite
  - `yosys`: ASIC and generic gate/LUT synthesis
  - `yosys_fpga`: FPGA synthesis; it comes first in the builds of `nextpnr`, `fpga_pack` and
    `openfpgaloader`, and it can feed `vivado_impl`
  - `yosys_sim`: simulation with CXXRTL

Run `xeda list-flows` for the complete list in the installed version. Run
`xeda list-settings <flow>` and `xeda list-results <flow>` for the settings and the results of a
flow.

## Development

```bash
git clone https://github.com/XedaHQ/xeda.git
cd xeda
uv sync
uv run pytest tests/                      # serial
uv run pytest tests/ -n logical           # one worker per logical CPU, several times faster
uv run pytest -n logical -m "python_compat and not real_tool"  # faster cross-version check
uv run pytest tests/ -n logical --shard 2/4  # the second of four disjoint parts, as CI runs it
```

`uv sync` installs xeda in editable mode, with the dependency group `dev` of `pyproject.toml`. This
group includes the group `test` (pytest and the packages of the tests), the group `examples` (the
Python packages of the cocotb testbenches of the examples, which the tests run, and `pyserial` for
the host programs of the `uart_echo` and `uart_link` examples) and the quality tools. The
dependency groups are not part of the published package: `pip install xeda` installs none of them.

The marked command runs the compatibility cases that CI runs on Python 3.11 through 3.14. CI runs
the full tool-backed suite on Python 3.15 as four parallel jobs, one shard each, and one Verilator
cocotb smoke test on Python 3.11. A `--shard K/N` command runs only one of N disjoint parts of the
suite. The first two commands run every test.

Bare `tox` runs those compatibility checks, the full Python 3.15 suite and the quality checks. To
run the full test suite on every supported Python version, use
`tox -e py311,py312,py313,py314,py315`.

An editable pip installation also works if `uv` is not available. pip 25.1 or newer installs the
dependency groups:

```bash
COCOTB_IGNORE_PYTHON_REQUIRES=1 python -m pip install --editable . --group dev
```

Cocotb 2.1.0 supports Python through 3.14, and it has no wheel for Python 3.15. On Python 3.15, its
upstream `setup.py` rejects a source build unless `COCOTB_IGNORE_PYTHON_REQUIRES=1` is set.
`uv sync` sets this variable for the build of cocotb, and so does the `py315` environment of tox.
It overrides the build-time version check of cocotb. It does not add upstream support for
Python 3.15.

Do not edit `CHANGELOG.md` to record a change. Add one file to `changelog.d/` for each change
that a user can see. At release, the maintainer folds the files into the changelog with
`python tools/fold_changelog.py v<version>`. Name the file `<slug>.<type>.md`:

- `<slug>` is a short name for the change, in kebab-case.
- `<type>` is `fixed`, `added`, `changed` or `removed`.

The file holds one Markdown bullet: one or two short sentences about what changed for a user. For
a breaking change, say what to do instead. Wrap the lines at 100 columns, and indent each line
after the first by two spaces. For example, `changelog.d/scrub-keeps-new-runs.fixed.md` can hold
this text:

```markdown
- `xeda scrub` no longer removes a run that finished after it listed the directories.
```

`tests/test_changelog_fragments.py` checks every fragment.
