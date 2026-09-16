---
name: xeda
description: Use when running, configuring, or debugging Xeda EDA flows - simulation (GHDL, NVC, Verilator, ModelSim, VCS, xsim), FPGA synthesis (Vivado, Quartus, Diamond, ISE, yosys+nextpnr, OpenXC7), or ASIC synthesis (OpenROAD, Design Compiler) - and when writing or fixing a Xeda design file (`*.toml`/`*.yaml` with `[rtl]`/`[tb]` sections, or `xedaproject.toml`). Also use for reading a run's `results.json`, interpreting Fmax/timing/utilization numbers, or diagnosing `FlowSettingsError`, `FlowNotFoundError`, or `ExecutableNotFound`.
---

# Driving Xeda

Xeda turns one declarative design description into runs across many EDA tools. Its CLI is
self-describing, so **discover, do not guess**: unknown settings are a hard error, not a warning.

## The one rule for machine-readable output

`--json` always means **"a parseable result on stdout"**.

- Query commands take `--format {table,json,jsonl,yaml}`; `--json` is shorthand for `--format json`.
- `run`, `dse` and `scrub` take a plain `--json` flag, which moves tool output and logs to
  **stderr** so stdout carries only the JSON document.

Always prefer `--json`. The table renderings wrap to the terminal width.

## Core workflow

```bash
# 1. What flows exist? (name, aliases, category, dependencies, description)
xeda list-flows --json

# 2. What can I set on one? Every name here is usable as -s <name>=<value>
xeda list-settings vivado_synth --json

# 3. What will come back?
xeda list-results vivado_synth --json

# 4. Run it, and read the result in the same pipe
xeda run vivado_synth sqrt.toml -s clock_period=5.0 --json
```

`xeda run --json` emits one object: `success`, `results`, `run_path`, `results_json`,
`settings_json` - or `success: false` plus an `error` object. Exit status is non-zero on failure.

```bash
xeda run vivado_synth sqrt.toml --json | jq '.results.Fmax'
```

## Writing a design file

`xeda design-schema` is the authoritative JSON Schema. The shape:

```toml
name = "sqrt"                      # required; names the run directory
description = "..."
language.vhdl.standard = "2008"    # or language.verilog.standard

[rtl]
sources = ["pkg.vhdl", "sqrt.vhdl"]   # required, IN COMPILATION ORDER
top = "sqrt"                          # required by synthesis flows
clock_port = "clk"                    # names the clock PORT, not its period
parameters = { G_IN_WIDTH = 32 }      # or `generics`; the two are interchangeable

[tb]
sources = ["tb_sqrt.py"]              # a .py source is detected as cocotb automatically
top = "tb_sqrt"                       # required by simulation flows for non-cocotb testbenches

[flows.vivado_synth]                  # per-flow settings, applied only for that flow
fpga.part = "xc7a100tftg256-2L"
clock_period = 5.0
```

Key points that are easy to get wrong:

- **Paths resolve against the design file's directory**, not the working directory.
- **`sources` order is compilation order.** VHDL packages must precede their users.
- **Clocks split in two.** `[rtl]` names the clock *port*; the *period or frequency* is a flow
  setting, because it constrains a particular build. Single clock: `clock_port = "clk"` in `[rtl]`
  plus `clock_period` (ns) per flow. Multiple: `[[rtl.clocks]]` entries plus a `clocks` mapping in
  the flow settings.
- Give a source a table instead of a string when inference is not enough:
  `{ file = "legacy.v", type = "SystemVerilog" }`. Use `path =` instead of `file =` for a source a
  generator will produce (it is not checked for existence).

See `references/design-file.md` for the full reference.

## Settings

Precedence, lowest to highest: flow defaults -> the design file's `[flows.<flow>]` section ->
command-line `-s`.

```bash
# dotted keys reach nested settings; several can be given at once
xeda run vivado_synth sqrt.toml -s clock_period=4.5 synth.strategy=Flow_PerfOptimized_high
```

Every flow also accepts `ncpus` (alias `nthreads`), `dockerized`/`docker`, `clean`,
`quiet`/`verbose`/`debug`, `redirect_stdout` and `lib_paths`. `xeda list-settings <flow> --json`
marks these with `"common": true`; `--no-common` omits them.

A setting may have an `alias`; both names work (`vcd`/`waveform`, `nthreads`/`ncpus`).

## Flow dependencies

Run the flow you want, not the chain leading to it - dependencies run automatically:

```
openfpgaloader -> nextpnr -> yosys_fpga
vivado_power   -> vivado_postsynth_sim -> vivado_synth
openroad       -> yosys
```

Reach a dependency's settings through a nested key:

```bash
xeda run openfpgaloader blinky.toml -s nextpnr.yosys.flatten=true
```

`xeda list-flows --json` reports each flow's `dependencies`.

## Reading results

`results.json` in the run directory is authoritative. Every flow reports `success`, `runtime`,
`tools`, `artifacts`, `design`, `flow`, `run_path`, `timestamp` and the two hashes. Flow-specific
keys: `xeda list-results <flow> --json`.

Canonical keys the runner adds alongside whatever the flow reported, so a script need not know
which flow produced the file:

| Canonical | Also reported as |
| --- | --- |
| `Fmax` | `f_max`, `maximum_frequency` |
| `lut` | `LUT` |
| `ff` | `FF` |

**`clock_frequency` is not `Fmax`.** `clock_frequency` is the frequency that was *constrained*;
`Fmax` is the maximum that was *achieved*. Reporting one as the other is a real error.

Keys beginning with `_` are internal and may change.

## Where output lands

Default `./xeda_run/<design>/<flow>/`, holding the generated tool scripts, the tool logs,
`reports/`, `outputs/`, `checkpoints/`, plus:

- `settings.json` - the **effective** settings after every override was merged. Read this first
  when a run did something unexpected.
- `results.json` - what was parsed back out.

`xeda run --json` reports all three paths, so there is no need to guess.

## When something fails

`error.type` in the `--json` output names the exception class:

| `error.type` | Meaning | What to do |
| --- | --- | --- |
| `FlowSettingsError` | A setting is unknown or has the wrong type | `xeda list-settings <flow> --json` and match the name exactly |
| `FlowNotFoundError` | Unknown flow name | The message suggests close matches; `xeda list-flows` |
| `ExecutableNotFound` | The tool is not on `PATH` | Install it, or use `-s dockerized=true` |
| `NonZeroExitCode` | The tool itself failed | Read the tool log in `run_path` |
| `DesignValidationError` | The design file is invalid | Validate against `xeda design-schema` |
| `FlowFailed` | The flow ran but reported failure | Read `results` and the reports under `run_path` |
| `NoSuccessfulRun` | A DSE search found no successful run | Inspect the attempted runs and relax or correct the search settings |

Flow names are forgiving - `vivado_synth`, `vivado-synth` and `VivadoSynth` all work. Setting
names are not.

Add `--debug` to get verbose logs and a real traceback instead of a one-line failure.

See `references/troubleshooting.md` for more.

## Reference files

- `references/flows.md` - catalog of every flow in the installed version, with its aliases,
  category, dependencies and result keys. Regenerate with `xeda skill install`.
- `references/design-file.md` - full design-file reference.
- `references/troubleshooting.md` - failure modes and how to read a run directory.

## Using Xeda as a library

```python
from xeda import Design, DefaultRunner
from xeda.introspect import flows_info, settings_info, results_info, design_schema

design = Design.from_file("sqrt.toml")
flow = DefaultRunner("xeda_run").run("vivado_synth", design, flow_settings=["clock_period=5.0"])
if flow and flow.results.success:
    print(flow.results.Fmax)
```

`xeda.introspect` returns the same data the CLI's `--json` renders, as plain Python objects.
