---
name: xeda
description: Use when running, configuring, or debugging Xeda EDA flows - simulation (GHDL, NVC, Verilator, ModelSim, VCS, xsim, Bluesim), Bluespec (bsc), FPGA synthesis and implementation (Vivado, Quartus, Diamond, ISE, yosys+nextpnr, openXC7), ASIC synthesis (yosys, Design Compiler) - and when writing or fixing a Xeda design file (`*.yaml`; TOML and JSON also load) or project file (`xedaproject.yaml`). Also use for reading a run's `results.json`, interpreting Fmax/timing/utilization numbers, and fixing a failed run (`FlowSettingsError`, `DesignValidationError`, `ExecutableNotFound`, `NonZeroExitCode`, `FlowFailed`).
---

# Driving Xeda

Xeda runs one flow (simulation, synthesis, implementation) on one design file. Query the CLI for
names, settings and result keys; do not guess them. An unknown setting is an error.

## Machine-readable route

`--json` always means "one parseable document on stdout".

| Need | Command |
| --- | --- |
| Flows, with aliases, default producers, declared I/O and chain relations | `xeda list-flows --json` |
| Settings of a flow (name, alias, type, default, enum, `writes`, `common`) | `xeda list-settings <flow> --json` (`--no-common` omits the settings every flow has) |
| Result keys of a flow | `xeda list-results <flow> --json` |
| Design file schema | `xeda design-schema` |
| Boards, ASIC platforms, DSE optimizers | `xeda list-boards --json`, `xeda list-platforms --json`, `xeda list-optimizers --json` |
| Which PDK files of a platform are installed, and where | `xeda pdk list --json`, `xeda pdk path <platform> --json` (fails with the install command when they are missing) |
| What a run would do | `xeda run <flow> <design> --dry-run --json` |
| Run | `xeda run <flow> <design> -s KEY=VALUE --json` |
| Search for the highest clock frequency | `xeda dse <flow> --design <design> --json` |
| Remove a flow's old run directories | `xeda scrub <flow> <design> [--target NAME] --json` |

Query commands take `--format {table,json,jsonl,yaml}` (`design-schema`: `json`, `yaml`); `--json` is
`--format json`. On `run`, `dse`
and `scrub`, `--json` moves tool output and logs to stderr. Table output wraps at the terminal
width, so parse JSON.

`xeda scrub` and `run --scrub` prompt on stdin and remove nothing unless you type `yes`:
`echo yes | xeda scrub vivado_synth sqrt --json`. `scrub --json` reports `scrubbed`, `kept`, `gone`
and `skipped` paths.

```bash
xeda run vivado_synth sqrt.yaml -s clock.period=5.0 --json | jq '.results.Fmax'
```

The exit status is 0 on success, 1 on a failed run and 2 on a usage error. The `run` document has:

| Key | Value |
| --- | --- |
| `flow`, `design`, `design_file`, `target` | requested flow; the design's name; the design file you named (absolute, or `null`); the selected target (or `null`) |
| `success`, `results` | verdict; the flow's `results.json` content (keys: `xeda list-results`) |
| `run_path`, `results_json`, `settings_json` | the run directory and two of its files |
| `request`, `nodes` | the flows asked for; every run directory touched, with `state` (`fresh`, `ran`, `failed`, `rejected again`, `blocked`, `not run`), `reason`, `deliveries`, `inputs` |
| `error` | on failure only: `{type, message}`; see "When a run fails" |

Node states: `failed` (the flow ran and failed, or a problem with what its producers handed over
stopped it), `rejected again` (the recorded rejection of an unchanged run, reported without running
the tool), `blocked` (a producer failed, was rejected or is blocked, so the flow did not run).

A dry run adds `dry_run: true` and `plan` (`requested`, `target`, `nodes`). A plan node has `name`,
`flow`, `run_path`, `flowrun_hash`, `settings_hash`, `inputs` (`origin`: `producer`, `source` or
`none`; `binding_origin`), `switched_on`, `state`, `reason`, `action_reason`, `input_types` and
`output_types`. `state` is `fresh` (reused), `stale` (runs; `reason` is the one the log gives),
`unknown` (`reason` says why, for example `depends on yosys_fpga`), `rejected again` (the
checks rejected the last run and nothing it consumes changed; `reason` is the recorded message, and
the flow does not run) or `blocked` (a producer is `rejected again` or blocked; `reason` names it).

## Plan before you run

```bash
xeda run nextpnr blinky.yaml --dry-run --json
```

A dry run runs no tool and changes no run root, trace, lock or delivery. It refuses what a run
would refuse at planning: unknown or conflicting settings, a source type the flow cannot read, a
missing required setting, a chain that does not fit. It refuses a design that needs a generator
run or a Git fetch; materialize that design first. `--dry-run` with `--remote` is a usage error.

## Design file

`xeda design-schema` is the schema; `references/design-file.md` is the full reference (sources,
clocks, targets, projects, dotted keys).

```yaml
name: sqrt # optional; defaults to the file's stem and names the run directory
language.vhdl.standard: 2008
rtl:
  sources: ["pkg.vhdl", "sqrt.vhdl"] # in compilation order
  top: sqrt
  clock.port: clk # names the clock port; the period is a flow setting
  parameters.G_IN_WIDTH: 32
tb:
  sources: ["tb_sqrt.py"] # a .py source is a cocotb testbench
flows:
  vivado_synth:
    fpga.part: xc7a100tftg256-2L
    clock.period: 5.0
```

- Relative paths resolve against the design file's directory.
- `rtl.sources` order is compilation order: VHDL packages come before their users.
- Every source has a type. A suffix gives it; `.json`, `.bin`, `.cfg`, `.config`, `.dcp` need
  `{file: "top.json", type: JsonNetlist}`. A source a generator writes is `{path: "gen/top.v"}`.
- A non-cocotb HDL testbench needs `tb.top`; simulation flows refuse the design without it
  (`ghdl_sim` finds a VHDL testbench's top itself).
- Give the clock port in `rtl.clock.port` and the period in the flow: `clock.period` (ns) or
  `clock.freq` (`200MHz`). For several clocks, use `rtl.clocks` and the flow's `clocks.<name>`.
- Write a table of at most two settings as dotted keys, a file section (`rtl`, `tb`, `flows`,
  `flows.<flow>`, `targets`) as a block. Keep `inputs` and tool-named keys nested.
- YAML is 1.2 core: `yes`, `on` are text; a boolean is `true` or `false` and nothing else
  (`debug: yes` is an error). Quote text that reads as a number (`"010"`).
- `targets.<name>` overlays the design for one board. Select it with `--target NAME` on `run` and
  `dse`; a design with one target needs no flag.

## Settings

Layers, lowest to highest: flow defaults, project `flows.<flow>`, design `flows.<flow>`, selected
target's `flows.<flow>`, `-s`, API. They merge key by key, so `-s clock.freq=100MHz` refines a
nested section. Write a flow's settings under its own `flows.<flow>` only.

```bash
xeda run vivado_synth sqrt.yaml -s clock.period=4.5 synth.strategy=Flow_PerfOptimized_high
xeda run nextpnr blinky.yaml -s flows.yosys_fpga.flatten=true
```

- `-s` takes `KEY=VALUE` items up to the next option or the first token that is not `KEY=VALUE`.
  Put the design before `-s`, or end the options with `--`.
- A dotted key reaches a nested setting. `-s flows.<flow>.<key>=<value>` sets any flow of the run (a
  producer too); `-s <key>` and `-s flows.<requested>.<key>` are one setting.
- A setting with `"opaque_keys": true` (`set_synth_properties` and `set_impl_properties` of
  `vivado_synth` and `vivado_project`, ISE `*_options`) takes a dotted tool name as one key:
  `-s set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0`. Quote a name with a space.
- Settings every flow accepts (`"common": true`) include `nthreads` (alias `ncpus`), `dockerized`
  (run the tools from their Docker image), `docker`, `quiet`, `verbose`, `debug`,
  `redirect_stdout` and `lib_paths`. A setting with an `alias` accepts both names. A flow whose
  tool has no matching option accepts `nthreads`, `redirect_stdout` or `lib_paths` and ignores it:
  only the Vivado flows redirect output, and only GHDL, ModelSim, NVC and the Vivado simulation
  flows read `lib_paths`. `ghdl_sim`, `ghdl_synth` and `nvc` take a relative path as relative to the
  design root. `nvc` maps `(name, path)` with `--map=name:path`, passes `(None, path)` as `-L path`
  (a directory that holds library directories) and refuses an entry that has a name and no path.
- A flow uses every other setting it lists, and refuses a setting of a related flow:
  - The message names the flow that has it: `vivado_project` refuses `elab_debug`, `stop_time` and
    the other `vivado_sim` settings ("use `flows.vivado_sim.<name>`"); `ghdl_synth` refuses
    `compiler_flags`, `assembler_flags` and `linker_flags` ("use `flows.ghdl_sim.<name>`");
    `vivado_alt_synth` refuses `set_synth_properties`, `set_impl_properties` and
    `show_available_strategies` ("it is a setting of vivado_synth": non-project mode has no runs).
  - The message says to delete it: `yosys_sim` refuses the synthesis settings of `yosys`
    (`splitnets` was removed: "CXXRTL simulation does not synthesize; delete it").
  - A removed setting fails with "`<name>` was removed: ...". If nothing replaces it, the message
    says to delete it. `timeout_seconds` is now `timeout`, and `no_console` is gone.
- A setting accepts its declared type only. `true` and `false` are the only booleans, and a list
  setting given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`).

## Flows that feed each other

Run the flow you want. Its default producers run first:

| Flow | Needs (default producer) |
| --- | --- |
| `openfpgaloader` | `fpga_pack` (`bitstream`) |
| `fpga_pack` | `nextpnr` (`config`) |
| `nextpnr` | `yosys_fpga` (`netlist`) |
| `vivado_impl` | `yosys_fpga` (`netlist_edif`) |
| `vivado_power` | `vivado_postsynth_sim`, `vivado_synth` |
| `vivado_postsynth_sim` | `vivado_synth` |

A source of the input's type replaces the producer: a `JsonNetlist` source skips `yosys_fpga` for
`nextpnr`. Set a producer's settings under `flows.<producer>`.

A chain runs flows in order and binds each output to the next flow's required input; the last flow
is the one requested (its `results`, `--help-settings` and exit status):

```bash
xeda run yosys_fpga+nextpnr+fpga_pack design.yaml
xeda run nextpnr.config+fpga_pack design.yaml
xeda run yosys_fpga+fpga_pack design.yaml   # same plan: nextpnr runs between the two
```

- `+` is the only separator; a flow appears once; `FLOW.OUTPUT` picks an output. A flow that
  programs a device (`openfpgaloader`) can only end a chain.
- A link `a+b` may pass through flows: when the default producers of `b` reach `a`, they are planned
  as for `b` alone. It binds the inputs `a` feeds (`a.output` must fit each). A flow it passes
  through cannot appear elsewhere in the chain; a source or binding that takes it out of the plan is
  refused.
- Only flows with declared I/O chain (`can_follow`, `can_precede` in `xeda list-flows --json`; an
  entry with `through` lists the flows its link passes through). `bsc`, `bsc_sim`,
  `vivado_project` and `vivado_sim` run alone.
- `--remote` and `xeda dse` refuse chains and bindings.
- A refused chain ends with `Did you mean ...?` when one valid request is near. Planning can still
  refuse a request the chain check passed (related inputs, below).

An input comes from, in this order: an explicit binding (a chain, or `flows.<consumer>.inputs.<input>:
<producer>[.<output>]` in a design or project file, `-s`, or the API); a typed source in
`rtl.sources`; the default producer. A binding names a flow's output, never a file. `inputs` is
not a setting: `list-settings` and `settings.json` omit it. A chain replaces a saved binding of the
same input (the plan lists it in `overridden`) and conflicts with a command-line or API binding of
it.

```yaml
# prebuilt_demo.yaml: `xeda run nextpnr prebuilt_demo.yaml` skips synthesis;
# `xeda run yosys_fpga+nextpnr prebuilt_demo.yaml` synthesizes anyway.
name: prebuilt_demo
rtl:
  top: top
  sources:
    - top.v
    - file: top.json
      type: JsonNetlist
flows:
  nextpnr:
    fpga:
      part: LFE5U-85F-6BG381C
```

A project file holds the same section (`--xedaproject FILE`, or the one `xedaproject.yaml`, `.yml`
or `.toml` in the start directory):

```yaml
# project.yaml
flows:
  nextpnr:
    inputs:
      netlist: yosys_fpga
```

`vivado_power` takes timing activity (`vivado_postsynth_sim.timing_saif`) by default. For the
faster, less accurate activity of a functional simulation, bind it with
`-s flows.vivado_power.inputs.activity=vivado_postsynth_sim.saif`; `timing_sim` is no setting of
`vivado_power`.

Related inputs of one flow must come from one producer (`same_producer_as`, `via` in `list-flows`):
`vivado_postsynth_sim`'s `netlist`, `netlist_timing` and `sdf`, and `vivado_power`'s `checkpoint`
with the synthesis behind its `activity`. A binding that splits them is refused; the message lists
the `-s flows.<node>.inputs.<input>=<producer>.<output>` bindings that fix it.

Shared settings on declared edges (`fpga`, `board`, `custom_boards_file`, `clocks`, `prjxray_db`,
`platform`, `corner`, `dont_use_cells`, `platform_file`, `pdk_provider`, `pdk_version`,
`pdk_root`, where both nodes declare them) must agree. `platform` is a bundled name;
`platform_file` and `pdk_root` compare by resolved path; an unset `pdk_provider` or `pdk_version`
is filled in from the platform before hashing. Disjoint leaves combine; two values for one leaf
fail and name both origins. A command-line leaf
(`-s fpga.part=...` or `-s flows.yosys_fpga.fpga.part=...`) wins for the connected group. A shared
setting written under a flow that is not in the run reaches no node.

Do not suggest a chain through `bsc` (`bsc+yosys_fpga`) or an `inputs.design` binding: `bsc`
declares no I/O, and Xeda refuses both. `vivado_synth+openfpgaloader` is a valid chain that
programs hardware: plan it with `--dry-run` only, and end other build checks at `fpga_pack`.

Open `references/fpga-flows.md` for the open-source FPGA flow (yosys, nextpnr, openXC7, boards, pin
constraints, programming) and for `vivado_impl`.

## Results

`results.json` in the run directory is authoritative. Every flow reports `success`, `runtime`,
`tools`, `artifacts`, `design`, `flow`, `run_path`, `timestamp`, `design_hash`, `flow_hash` and
`settings_hash`; the rest is flow-specific (`xeda list-results <flow> --json`). The runner adds
canonical keys `Fmax`, `lut` and `ff` beside the flow's own (`f_max`, `maximum_frequency`, `LUT`,
`FF`); read the canonical ones.

- `Fmax` is the frequency achieved; `clock_frequency` is the one constrained. Compare `Fmax` and
  `lut` only between runs of the same tool. See "Reading numbers" in
  `references/troubleshooting.md`.
- `outputs` (declared output records `{path, sha}`) and `artifacts` are bookkeeping, not metrics.
  Keys that start with `_` are internal.

## Run directories

Runs go to `./xeda_run/<design>/<flow>/` (`<design>/<target>/<flow>/` with a target); `--run-root`
or `XEDA_RUN_ROOT` moves the root. A dependency's directory is a sibling. `--hashed-run-dirs`
appends the run hash (`<flow>_<hash>`) so settings variants coexist. A launch holds each finished
producer for reading until it ends; another Xeda process that would rebuild, clean or scrub that
directory waits (POSIX).

Xeda creates and marks the run root (`.xeda-run-root`, `.gitignore`, `CACHEDIR.TAG`). Keep nothing
of yours in it. A directory you name with `--run-root` that holds files and has no marker is
refused (`RunRootError`); the default `./xeda_run` is adopted.

Each run directory has the tool scripts and logs, `reports/`, `outputs/` and `checkpoints/` when
the flow writes them, and:

| File | Content |
| --- | --- |
| `settings.json` | `flow_settings` (the merged input) and `effective_flow_settings` (what the flow made of it; read it first when a run surprises you) |
| `results.json` | the parsed results; a failed run writes `success: false` and `error.type`/`error.message`; a run that its checks rejected keeps `trace.json`; an unchanged launch reports it again (state `rejected again`); `--rebuild-all` runs it |
| `trace.json` | what the run consumed and produced; present after a run that passed or that its checks rejected; a failure that no check rejected leaves none |

## Rebuilds

A run is make-like: a flow runs only when its settings, sources, tools, Xeda code or outputs
changed since its last success or rejection; otherwise its recorded results are reused. Inputs outside the
trace (indirect programs, environment variables, files a tool finds on its own) are not tracked.

| Need | Option |
| --- | --- |
| Run every flow | `--rebuild-all` |
| Empty each run directory, then run every flow | `--clean` |
| Keep settings variants side by side | `--hashed-run-dirs` |
| Remove the flow's other run directories first (prompts for `yes`) | `--scrub` |
| Keep only `settings.json`, `results.json` and artifacts afterward | `--post-cleanup` |
| Remove each run directory afterward | `--post-cleanup-purge` |

A flow that programs a device or draws a new random seed (`random_seed: random`) always runs
(`always_runs`): it runs every time and keeps no trace. A
removed option (`--cwd`, `--xeda-run-dir`, `--incremental`, `--cached-dependencies`) fails with
`<option> was removed: use ...`.

## Deliverables

A setting with `"writes": "deliverable"` in `list-settings` (`bitstream`, `vcd`, ...) takes a
location: `$PWD/...`, `$DESIGN_ROOT/...`, `$DESIGN_DIR/...` or an absolute path. The run writes the
file in its run directory, and Xeda copies it to the location when the whole launch ends.
`--outputs-to DIR` does the same for the requested flow's artifacts. A `"working"` setting is a
bare name in the run directory.

```bash
xeda run vivado_synth blinky.yaml -s bitstream=$PWD/blinky.bit
xeda run vivado_synth blinky.yaml --outputs-to ./out
```

- A setting that names a file refuses `true` and `false`. A waveform setting (`vcd`, `fst`, ...)
  takes `true` and writes `dump.<format>`.
- Moving the destination later never runs the flow again, and neither does giving a location to
  an output the flow always writes (`fpga_pack`'s `bitstream`). Setting `vivado_synth`'s
  `bitstream` for the first time does, because it switches the output on.
- A delivery never replaces a directory, a design source or a file the run reads. Two outputs for
  one destination are refused before any tool runs. An existing file is replaced without asking
  only if it is Xeda's own unchanged earlier delivery; otherwise pass `--overwrite-outputs`.
- `--post-cleanup-purge` moves the output instead of copying it, when it can.

## Remote runs

`--remote HOST` needs a remote Xeda of the 0.4.4 release line or newer that defines the remote
protocol marker (`xeda.REMOTE_PROTOCOL_VERSION >= 1`). Xeda probes the remote's installed package
and refuses one without it, with an upgrade error, before it ships anything. A remote run
always runs fresh and refuses `--clean`, `--hashed-run-dirs`, `--dry-run`, chains and bindings.

## When a run fails

On failure `run` writes a document with `success: false` and `error`, and exits non-zero. Branch on
`error.type`; `references/troubleshooting.md` has the messages and fixes.

| `error.type` | Do |
| --- | --- |
| `FlowSettingsError` (unknown, mistyped or removed setting), `FlowSettingsException` (the request needs what the settings or design lack: `fpga`, a source the flow reads, agreeing shared settings), `NoReadableSource` | Read the message; it names the setting and the fix. `xeda list-settings <flow> --json` lists valid names. |
| `DesignValidationError` | The message names the key (`rtl.sources:`). Check the file against `xeda design-schema`. |
| `BadParameter`, `UsageError` (exit 2) | Unknown flow, missing file, malformed chain, removed option. The message suggests close matches. |
| `DesignFileParseError` | The file does not parse; the message gives the line and column. |
| `DesignNotSpecified` | Pass the design file as the last argument, or `--design-name` for a project. |
| `ProjectFileError` | Keep one project file with the keys `designs` and `flows`. |
| `ExecutableNotFound` | Install the tool, or add `-s dockerized=true`. |
| `FlowFailed` | The requested flow ran and did not succeed. `results.error` has the cause: `NonZeroExitCode` (the tool exited non-zero; read its log in `run_path`), `ReportedFailure` (the tool exited 0 and its reports or checks say it failed), `MissingOutput` (a declared output is missing or was not written by this run). |
| `FlowDependencyFailure` | A producer failed or its output could not be verified. Read the producer's `results.json`, named in the message. |
| `RunRootError`, `RunDirectoryError`, `DeliveryError`, `OutputExistsError` | The message names the directory or setting and the fix. |
| `NoSuccessfulRun` | `xeda dse` found no successful run. Inspect the attempted runs and adjust the search. |

Add `--debug` for verbose logs and a traceback. Flow names accept `vivado_synth`, `vivado-synth`
and `VivadoSynth`; setting names are exact.

## References

| Open | When |
| --- | --- |
| `references/design-file.md` | Writing or fixing a design or project file; source types; clocks; targets; dotted keys |
| `references/fpga-flows.md` | yosys_fpga, nextpnr, fpga_pack, openfpgaloader, openXC7, boards, pin constraints, `vivado_impl` |
| `references/troubleshooting.md` | A failure, a simulation verdict, a waveform setting, a number that looks wrong |
| `references/flows.md` | A flow's category, aliases, producers and result keys (generated by `xeda skill install`) |

## Library

```python
from xeda import Design, DefaultRunner
from xeda.introspect import flows_info, settings_info, results_info, design_schema

design = Design.from_file("sqrt.yaml")
flow = DefaultRunner("xeda_run").run("vivado_synth", design, flow_settings=["clock.period=5.0"])
if flow and flow.results.success:
    print(flow.results.Fmax)
```

`xeda.introspect` returns the data the CLI's `--json` renders.
