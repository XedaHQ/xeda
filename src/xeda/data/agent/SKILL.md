---
name: xeda
description: Use when running, configuring, or debugging Xeda EDA flows - simulation (GHDL, NVC, Verilator, ModelSim, VCS, xsim, Bluesim), Bluespec (bsc), FPGA synthesis and implementation (Vivado, Quartus, Diamond, ISE, yosys+nextpnr, openXC7), ASIC synthesis and place and route (yosys, Design Compiler, OpenROAD) - and when writing or fixing a Xeda design file (`*.yaml`; TOML and JSON also load) or project file (`xedaproject.yaml`). Also use for reading a run's `results.json`, interpreting Fmax/timing/utilization numbers, and fixing a failed run (`unknown_setting`, `invalid_design`, `executable_not_found`, `nonzero_exit`, `rejected`).
---

# Driving Xeda

Xeda runs one flow (simulation, synthesis, implementation) on one design file. Query the CLI for
names, settings and result keys; do not guess them. An unknown setting is an error.

## Machine-readable route

`--json` always means "one parseable document on stdout".

| Need | Command |
| --- | --- |
| Flows, with default producers, declared I/O and chain relations | `xeda list-flows --json` |
| Settings of a flow (name, type, default, enum, `writes`, `common`, `tier`, `group`) | `xeda list-settings <flow> --json` (`--no-common` omits the settings every flow has; `--tier common` keeps the most used) |
| Result keys of a flow | `xeda list-results <flow> --json` |
| Design file schema | `xeda design-schema` |
| Boards, ASIC platforms, DSE optimizers | `xeda list-boards --json`, `xeda list-platforms --json`, `xeda list-optimizers --json` |
| Which PDK files of a platform are installed, and where | `xeda pdk list --json`, `xeda pdk path <platform> --json` (fails with the install command when they are missing) |
| Install the PDK files of a platform (downloads and writes the PDK store; propose it to the user first) | `xeda pdk install <platform> --json` (installs without asking; reports `already_installed` when the files are there; `--provider lambdapdk` installs `sky130hd` from lambdapdk, which `-s <flow>.pdk_provider=lambdapdk` then selects) |
| What a run would do | `xeda run <flow> <design> --dry-run --json` |
| Run | `xeda run <flow> <design> -s <flow>.KEY=VALUE --json` |
| Search for the highest clock frequency | `xeda dse <flow> <design> --json` |
| Remove a flow's old run directories | `xeda scrub <flow> <design> [--target NAME] --json` |

Query commands take `--format {table,json,jsonl,yaml}` (`design-schema`: `json`, `yaml`); `--json` is
`--format json`. On `run`, `dse`
and `scrub`, `--json` moves tool output and logs to stderr. Table output wraps at the terminal
width, so parse JSON.

`xeda scrub` and `run --scrub` prompt on stdin and remove nothing unless you type `yes`:
`echo yes | xeda scrub vivado_synth sqrt --json`. `scrub --json` reports `scrubbed`, `kept`, `gone`
and `skipped` paths. The Python API never prompts: `scrub_design` and a launch with
`prune_variants` remove nothing unless the caller passes `confirm` / sets `confirm_scrub`.

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s vivado_synth.clock.period=5ns --json | jq '.nodes[-1].record.metrics.fmax'
```

The exit status of `run` is 0 for the outcome `passed`, 1 for any other outcome and 2 for a request
that started no launch (an invalid setting, design, project or chain, or a usage error). A Ctrl-C
before the launch started gives `outcome: null` too, with the code `canceled` and exit status 1.
Other commands exit 0 or 1, and 2 for a usage error. The `run` document has exactly these keys:

| Key | Value |
| --- | --- |
| `schema_version` | 1 |
| `outcome` | `passed`, `rejected`, `failed`, `canceled`, `skipped` or `detached`; `null` when no launch started |
| `error` | `null`, or the cause: `{code, message, node, exit_status, log}`; see "When a run fails" |
| `launch_id` | the id of the launch; `null` when no launch started |
| `spec` | `{flow, design, design_file, target, settings}`: the request text, the design's name, the design file you named (absolute, or `null`), the selected target (or `null`), and `-s` as a table of settings by flow name; `null` for a usage error |
| `roots`, `nodes` | the requested node (the last flow of a chain); every planned node, producers first |

A node has `name`, `flow`, `outcome`, `disposition`, `reason`, `blocked_by`, `elapsed`, `run_dir`,
`deliveries` (`{key, destination, delivered}`), `error` (why the node did not pass, or `null`) and
`record`, the `results.json` object (`null` for a node that did not run or whose record could not be
stored). The metrics are in `record.metrics`; metric keys: `xeda list-results`.

Node `outcome`: `passed`, `rejected` (the checks rejected the results), `failed`, `canceled`,
`blocked` (a producer did not pass, so the flow did not run; `blocked_by` names it), `skipped` (the
launch stopped before its turn) and `detached` (a remote launch that keeps running). `disposition`:
`ran`, `reused` (an up to date `passed` or `rejected` run, reported without running the tool),
`shared` or `not_run`. The `outcome` of the document is `detached` if a node is detached, else
`canceled` after a cancel, else `failed` for an error of the whole launch (`delivery_failed`,
`cleanup_failed`, a remote failure). Otherwise it is that of the cause: the root, or the producer
that failed or was rejected.

`--dry-run --json` prints a document of its own, not the `run` document. Its keys are
`schema_version`, `spec` (`flow`, `design`, `design_file`, `target`, `settings`), `roots`, `options`
(the seven launch options by name), `dependencies` (the entries of `dependencies` in a run record)
and `nodes`. A plan node has `name`, `flow`, `run_dir`,
`identity`, `state`, `reason`, `action`, `settings`, `inputs` (`name`, `origin`: `producer`, `source`
or `none`; `producer`, `output`, `sources`, `reason`), `outputs_enabled`, `input_types`,
`output_types` and `deliveries` (`key`, `destination`, `replaces`). `state` is `fresh` (reused), `stale`
(runs; `reason` is the one the log gives), `unknown` (`reason` says why, for example `depends on
yosys_fpga`), `rejected` (the checks rejected the last run and nothing it consumes changed;
`reason` is the recorded message, and the flow does not run) or `blocked` (a producer is `rejected`
or blocked; `reason` names it).

## Plan before you run

```bash
xeda run nextpnr blinky.yaml --dry-run --json
```

A dry run runs no tool or generator. Planning resolves Git dependencies and fetches an uncached
checkout into `<run root>/.dependencies/<key>/`; on POSIX, it also leaves the lock file
`<key>.lock` beside it. Resolving a branch, tag or `HEAD` can also use the network. It changes
nothing else in the run root: no run directory, trace or delivery. A node that reads generated
sources is `unknown`, "the generator has not run". The plan refuses what a run would refuse at planning:
unknown or conflicting settings, a source type the flow cannot read, a missing required setting,
or a chain that does not fit. With `--remote HOST`, the host makes the plan and judges freshness;
the dry run may install Xeda on the host and sync the inputs, and it logs what it uploaded.

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
    clock.period: 5 ns
```

- Relative paths resolve against the design file's directory.
- `rtl.sources` order is compilation order: VHDL packages come before their users.
- Every source has a type. A suffix gives it; `.json`, `.bin`, `.cfg`, `.config`, `.dcp` need
  `{file: "top.json", type: JsonNetlist}`. A source a generator writes is a tree entry (a path below the design root that the generator writes, `gen_rtl/top.v`, relative to the root, not `cwd`) or a storage entry (`$XEDA_GENERATED/gen/top.v`, a file in the generator's storage); a pattern with no
  definite type needs `{path: "gen/*", type: Verilog}`.
- A non-cocotb HDL testbench needs `tb.top`; simulation flows refuse the design without it
  (`ghdl_sim` finds a VHDL testbench's top itself).
- Give the clock port in `rtl.clock` and the period in the flow: `clock.period` (`5 ns`) or
  `clock.freq` (`200 MHz`). For several clocks, use `rtl.clocks` and the flow's `clocks.<name>`.
- A physical setting needs a unit: `clock.period: 5 ns`, `-s vivado_synth.clock.period=5ns` (no space, so no
  quotes), `abc.load: 3.9 fF`. A bare number fails with `` `period` needs a unit, such as `5 ns` ``:
  the message names the field and writes the example in its unit (`timeout: 180` suggests `180 s`).
  `list-settings` shows the kind (`time`, `frequency`, `capacitance`, ...) as the `type` of a
  setting that takes a unit. Units are case-sensitive (`MHz`, `fF`). See "Units" in
  `references/design-file.md`.
- Write a table of at most two settings as dotted keys, a file section (`rtl`, `tb`, `flows`,
  `flows.<flow>`, `targets`) as a block. Keep `inputs` and tool-named keys nested.
- YAML is 1.2 core: `yes`, `on` are text; a boolean is `true` or `false` and nothing else
  (`debug: yes` is an error). Quote text that reads as a number (`"010"`).
- `targets.<name>` overlays the design for one board. Select it with `--target NAME` on `run` and
  `dse`. Without the flag, Xeda uses the design itself, with no target.

## Settings

Layers, lowest to highest: flow defaults, user file `flows`, (`--remote`: host profile `flows`),
project `flows.<flow>`, design `flows.<flow>`, selected target's `flows.<flow>`, project-local file
`flows`, `-s`, API. The user file is `$XDG_CONFIG_HOME/xeda/config.yaml` (`~/.config/xeda/`); the
project-local file is `xedaproject.local.yaml` beside the project file. Both are private (keep
the second out of Git), have the keys `flows`, `env` and `run_root`, and are read by `xeda run` and
`xeda dse` only. A `designs` key or any unknown key is `invalid_local_settings`; a flow name that is no flow is `flow_not_found`. `--config FILE` (must exist)
replaces the user file; `--no-config` reads neither file. A design file given as `DESIGN` gets the
user file and no layer from the project or the project-local file, and its run root follows the
same order. Run root, highest first: `--run-root`, `XEDA_RUN_ROOT`,
project-local `run_root`, user `run_root`, project `run_root`, `./xeda_run`. They merge key by key, so `-s vivado_synth.clock.freq=100MHz` refines a
nested section. Write a flow's settings under its own `flows.<flow>` only.

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s vivado_synth.clock.period=4.5ns vivado_synth.synth.strategy=Flow_PerfOptimized_high
xeda run nextpnr blinky.yaml -s yosys_fpga.flatten=true
```

- `-s` takes `KEY=VALUE` items up to the next option or the first token that is not `KEY=VALUE`.
  Put the design before `-s`, or end the options with `--`.
- A dotted key reaches a nested setting. Every `-s` key starts with the flow it addresses:
  `-s <flow>.<key>=<value>` sets any flow of the run (a producer too). A key without a flow
  (`-s seed=2`, `-s flows.nextpnr.seed=2`) fails with `unscoped_setting` and the address to write.
  The flows of the run follow the design: the plan's nodes and the default producers that a source
  or binding displaced. A converter (`bsc`) counts only if the design has a source it converts.
- Sections for flows outside the run differ by layer. A file (design, target, project) may keep a
  `flows.<flow>` for any known flow (a removed flow is an error): it is ignored, with the INFO line
  `<flow> is not part of this run: its settings are unused` for a default producer that a source or
  binding displaced and no line for other flows. `-s <flow>.*` must name a flow of the run,
  else `unknown_node`: `-s bsc.<key>` on a design without Bluespec sources is refused.
- One setting, one value per command line: two `-s` items that set one leaf to different values
  (`-s nextpnr.k=A -s nextpnr.k=B`) fail with `conflicting_keys`, listing every pair.
  An exact repeat is one item; compare text (`1` and `01` differ).
- A setting of a settings group has a dotted name, such as `abc.strategy` or
  `sequential.clock_gating.enabled` for `yosys`. Its `tier` (`common`, `advanced` or
  `expert`) says how often a user needs it; it affects the listing, never what Xeda
  accepts. The tier `common` is not the `"common": true` key. For the most used
  settings of `yosys`, run `xeda list-settings yosys --tier common --json`.
- A setting with `"opaque_keys": true` (`set_synth_properties` and `set_impl_properties` of
  `vivado_synth` and `vivado_project`, ISE `*_options`, and the dictionaries of `openroad` keyed by
  net or instance name: `ir_drop.nets`, `ir_drop.vsrc_files`, `ir_drop.instance_power`,
  `ndr.assignments`) takes a dotted name as one key:
  `-s vivado_synth.set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0` or
  `-s openroad.ir_drop.instance_power.u_core.reg_1=1mW`. Quote a name with a space.
- Settings every flow accepts (`"common": true`) are `nthreads`, `docker` (run the tools from
  their Docker image), `docker_image` (the image to use), `verbosity` (`quiet`, `normal`,
  `verbose` or `debug`; each level adds only its own tool options, so `debug` does not add those
  of `verbose`) and `lib_paths`. A flow whose tool has no matching option accepts
  `nthreads` or `lib_paths` and ignores it: only GHDL, ModelSim, NVC and the Vivado simulation
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
    says to delete it. On the simulator flows, `timeout` replaces `timeout_seconds`; no other flow
    limits a tool's run time. `no_console` has no replacement.
- A setting accepts its declared type only, from Python and from files: an integer setting takes
  `4`, not `"4"`, `4.0` or `true`. `true` and `false` are the only booleans. On the command line
  every value is text and Xeda reads it as the type of the setting (`-s nextpnr.seed=4`,
  `-s vivado_synth.timing_allow_fail=false`); a list setting is comma-separated
  (`-s vivado_synth.xdc_files=a.xdc,b.xdc`).

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
| `openroad` | `openroad_grt` (`odb`, `sdc`) |
| `openroad_grt` | `openroad_cts` (`odb`, `sdc`) |
| `openroad_cts` | `openroad_place` (`odb`, `sdc`) |
| `openroad_place` | `yosys` (`netlist`) |

A source of the input's type replaces the producer: a `JsonNetlist` source skips `yosys_fpga` for
`nextpnr`. Set a producer's settings under `flows.<producer>`.

`yosys` writes `netlist` (`VerilogNetlist`) when it maps to a Liberty library (`liberty`, `platform`
or `platform_file`, and no `gates` or `lut`), else `netlist_generic` (`GenericVerilogNetlist`, which
no flow takes). A listed netlist is your claim: Xeda does not parse it or compare it with
`platform`, `liberty` or `fpga`, and the tool that reads it reports unknown cells.

A chain runs flows in order and binds each output to the next flow's required input; the last flow
is the one requested (its record in the run document and exit status):

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
  entry with `through` lists the flows its link passes through). `bsc_sim` runs alone.
  `vivado_project` and `vivado_sim` have no output, so they end a chain; `bsc` is the only flow
  before them (`bsc+vivado_sim`).
- `xeda dse` refuses chains and bindings. `--remote` takes them. The conversion of a Bluespec
  design (below) is no chain or binding: both accept it.
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
`-s vivado_power.inputs.activity=vivado_postsynth_sim.saif`; `timing_sim` is no setting of
`vivado_power`.

Related inputs of one flow must come from one producer (`same_producer_as`, `via` in `list-flows`):
`vivado_postsynth_sim`'s `netlist`, `netlist_timing` and `sdf`, and `vivado_power`'s `checkpoint`
with the synthesis behind its `activity`. A binding that splits them is refused; the message lists
the `-s <node>.inputs.<input>=<producer>.<output>` bindings that fix it.

Shared settings on declared edges (`fpga`, `board`, `custom_boards_file`, `clocks`, `prjxray_db`,
`platform`, `corner`, `dont_use_cells`, `platform_file`, `liberty`, `pdk_provider`, `pdk_version`,
`pdk_root`, `hierarchical`, `wire_rc`, `out_of_context`, where both nodes declare them) must
agree. `platform` is a bundled name; `platform_file` and `pdk_root` compare by resolved path; `liberty` compares as the
list of resolved paths in order; an unset `pdk_provider` or `pdk_version` is filled in from the
platform before hashing. `hierarchical` and `wire_rc` are the
OpenROAD flows' (the database stores neither). `out_of_context` must be the same in `yosys_fpga`
and `nextpnr`, and only ECP5 takes it in `nextpnr`. Disjoint leaves combine; two values for one leaf
fail and name both origins. A command-line leaf (`-s yosys_fpga.fpga.part=...`) wins for the connected group. A shared setting written under
a flow that is not in the run reaches no node.

A Bluespec design runs on every flow that reads Verilog sources (`yosys`, `yosys_fpga`,
`vivado_synth`, `verilator` and others). When `rtl.sources` holds a language the flow does not read
and one flow converts it, that flow (today `bsc`) is the default producer of the `design` input:
`xeda run yosys examples/bluespec/gcd/gcd.yaml` plans `design <- bsc.design (the design has
Bluespec sources, which yosys does not read)`. The OpenROAD flows read no design sources: `xeda run
openroad examples/bluespec/gcd/gcd.yaml` gets its netlist from `yosys`, so `bsc` converts the design
for `yosys`. `bsc+openroad` and
`flows.yosys.inputs.design: bsc` give the same plan. Two fitting
converters are an error that names the chain and the binding to choose one. `bsc` passes the other
sources of `rtl` through in order and carries `tb`: a Bluespec testbench needs `bsc_sim`. A comment
edit of a `.bsv` reruns `bsc` only; an edit that changes the Verilog reruns the later flows too.
`examples/bluespec/gcd/gcd.yaml` holds every configuration of one design: `xeda run bsc_sim`,
`openroad`, `vivado_synth`, `yosys_fpga+nextpnr --target ecp5` and `yosys_fpga --target artix7` (the
FPGA ones are estimates without pins; a board build needs an I/O wrapper and constraints).
`vivado_synth+openfpgaloader` is a valid chain that programs hardware: plan it with `--dry-run`
only, and end other build checks at `fpga_pack`.

Open `references/fpga-flows.md` for the open-source FPGA flow (yosys, nextpnr, openXC7, boards, pin
constraints, programming) and for `vivado_impl`.

ASIC place and route: `xeda run openroad design.yaml` plans (`bsc`, for a Bluespec design) `yosys`,
`openroad_place`, `openroad_cts`, `openroad_grt` and `openroad`. The first three run one OpenROAD
process each; `openroad` runs two (detailed route, final outputs), and a third with `save_images`.
Every flow needs `platform`, and the PDK files must be installed (`xeda pdk path <platform>
--json`). A changed setting of a late flow reruns only that flow and the ones after it; a shared
setting (`wire_rc`, `platform`) reruns every flow. The database carries the routing layers, the
layer adjustments, `resize.opt_config`, `resize.global_sizing` and `design.dont_touch`: set them
under `flows.openroad_place` only. Physical settings take units (`clock.period: 0.65 ns`,
`floorplan.core_margin: 2 um`). `openroad` rejects a negative slack (`fail_timing`), design rule
violations (`fail_drc`) and antenna violations left after the repair (`fail_antenna`), all on by
default: outcome `rejected`, reported again without a tool run until something
changes. A failed stage leaves `<flow>-failed.odb` in its run directory.
`examples/bluespec/gcd/gcd.yaml` is a working design file for nangate45: its `flows.yosys` section
sets `platform` and `clock.period`, and `bsc` converts the design first.

## Results

`results.json` in the run directory is authoritative. It is one record (`schema_version` 1):
`outcome` (`passed`, `rejected`, `failed` or `canceled`; it replaces the old `success`), `error`
(`{code, message, node, exit_status, log}` or null), `metrics` nested by dotted path (`sim.ended_by`)
with `metric_info` (description, type, unit) for each, `outputs` and `artifacts` (names relative to
the run directory), `settings`, `configuration`, `inputs`, `run_id`, `identity`, `started`,
`finished` and `runtime`. It names no file of the run by an absolute path. Which metrics a flow writes:
`xeda list-results <flow> --json`. Every metric name is lower snake case, with dots between the
parts of a group (`fmax`, `lut`, `ff`, `lut_use.logic`); a quantity has one name in every flow. A
file without `schema_version` was written by an older Xeda: a launch runs that flow again.

- `fmax` is the frequency achieved; `clock_frequency` is the one constrained. Compare `fmax` and
  `lut` only between runs of the same tool. See "Reading numbers" in
  `references/troubleshooting.md`.
- `outputs` (declared output records `{path, sha}`) and `artifacts` are bookkeeping, not metrics.
  Keys that start with `_` are internal.
- An artifact is one file of the run directory, under a documented lower snake case key
  (`xeda list-results <flow> --json`, `artifacts`). A key is present only when the run wrote its
  file. `log` is the output of the flow's main tool (`logs/<program>.log`), `<program>_log` that
  of another program, `waveform` a VCD or FST, `synth_1_log` and `impl_1_log` the logs of the
  child runs of `vivado_synth` (the error of a failed run is there). `error.log` is the key of
  the log to read for a failure. A declared output is never an artifact.

## Run directories

Runs go to `./xeda_run/<design>/<flow>/` (`<design>/<target>/<flow>/` with a target); `--run-root`
or `XEDA_RUN_ROOT` moves the root. A dependency's directory is a sibling. `--hashed-run-dirs`
appends the run hash (`<flow>_<hash>`) so variants of the settings or of the design (its top,
parameters, defines, source places) coexist. A launch holds each finished
producer for reading until it ends; another Xeda process that would rebuild, clean or scrub that
directory waits (POSIX).

Xeda creates and marks the run root (`.xeda-run-root`, `.gitignore`, `CACHEDIR.TAG`). Keep nothing
of yours in it. A directory you name with `--run-root` that holds files and has no marker is
refused (`RunRootError`); the default `./xeda_run` is adopted.

Each run directory has the tool scripts and logs, `logs/<program>.log` (the output and error
output of every program the flow started; a Python caller sees them there and not on its streams),
`reports/`, `outputs/` and `checkpoints/` when the flow writes them, and:

| File | Content |
| --- | --- |
| `settings.json` | `flow_settings` (the merged input) and `effective_flow_settings` (what the flow made of it; read it first when a run surprises you) |
| `results.json` | the record of the run; a failed run writes `outcome: failed` and `error.code`/`error.message`; a run that its checks rejected keeps `trace.json`; an unchanged launch reports it again (outcome `rejected`, disposition `reused`); `--rebuild-all` runs it |
| `trace.json` | what the run consumed and produced; present after a run that passed or that its checks rejected; a failure that no check rejected leaves none |

## Rebuilds

A run is make-like: a flow runs only when its settings, sources, tools, Xeda code or outputs
changed since its last success or rejection; otherwise its recorded results are reused. Inputs outside the
trace (indirect programs, files a tool finds on its own) are not tracked. The inherited
environment is tracked (see Environment).

| Need | Option |
| --- | --- |
| Run every flow | `--rebuild-all` |
| Empty each run directory, then run every flow | `--clean` |
| Keep variants of the settings or of the design side by side | `--hashed-run-dirs` |
| Remove the flow's other run directories first (prompts for `yes`) | `--scrub` |
| Keep only `settings.json`, `results.json` and artifacts afterward | `--post-cleanup` |
| Remove each run directory afterward | `--post-cleanup-purge` |

A flow that programs a device or draws a new random seed (`random_seed: random`) always runs
(`always_runs`): it runs every time and keeps no trace. A
removed option (`--cwd`, `--xeda-run-dir`, `--incremental`, `--cached-dependencies`) fails with
`<option> was removed: use ...`.


## Environment

Every flow has `env` (a table of text variables, part of the identity) and `inherit_env` (default
`true`). A process gets: with `inherit_env`, the caller's environment; without it, from the caller
only the essential variables (`PATH`, `HOME`, `LANG`, `LC_*`, ...) and the flow's tool and license
variables (`locator_variables`, plus the `DOCKER_*` client variables with `docker`). Then come the
`env` of the private settings files, then `env`, then the variables the flow sets. A Docker
container gets only `DISPLAY` (`host.docker.internal:0`, replaceable), those last three, and `PWD`
(its working directory). Neither mode is hermetic.

The inherited values a node uses are freshness evidence, not identity: a changed value runs the
node again, and the stale reason names a variable, never a value. Shell, terminal, login and
`XEDA_*` session variables (`PWD`, `SHLVL`, `SSH_*`, `TMUX*`, `XEDA_RUN_ROOT`, ...) never count.
`results.json` lists the names in `inherited_variables`. With `--remote`, the environment is the
host agent's, and the private `env` of the local files is not sent; the `env` of the host profile
in `remotes.yaml` applies.

## Deliverables

A setting with `"writes": "deliverable"` in `list-settings` (`bitstream`, `vcd`, ...) takes a
location: `$PWD/...`, `$DESIGN_ROOT/...`, `$DESIGN_DIR/...` or an absolute path. The run writes the
file in its run directory, and Xeda copies it to the location when the whole launch ends.
`--outputs-to DIR` does the same for the requested flow's artifacts. A `"working"` setting is a
bare name in the run directory.

```bash
xeda run vivado_synth blinky.yaml -s vivado_synth.bitstream=$PWD/blinky.bit
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
- `rtl.generator.outputs_to` (a location) is a delivery of the same kind: the generated sources are copied
  there under their names in the generator's output, key `generator`. A delivery is also refused where an
  entry of `rtl.sources`, `tb.sources` or the generator's `sources` would read the file.
- `--post-cleanup-purge` moves the output instead of copying it, when it can.

## Remote runs

`--remote HOST` (env `XEDA_REMOTE`) runs the whole request on a Linux host over SSH and fetches the
results. It takes everything a local run takes: chain, bindings, target, `--outputs-to`,
`--dry-run`. Use it when the tools, licenses or cores are on a server. The host needs no Xeda: the
first run installs `uv`, the local Python version and the local package versions under the remote
root `${XDG_CACHE_HOME:-~/.cache}/xeda/v1`, without administrator rights.

Access:

- `HOST` is an OpenSSH destination: an alias of `~/.ssh/config`, `user@host` or
  `ssh://user@host:port`. Text that starts with `-` or has a space is `RemoteConfigError`.
- An unset or empty `XEDA_REMOTE` runs locally. A program that must run locally, whatever the
  environment, runs `XEDA_REMOTE= xeda run ...` or `env -u XEDA_REMOTE xeda run ...`. An empty
  `--remote ''` is a usage error (exit 2), and an empty `HOST` of `xeda remote check`, `attach` or
  `cancel` is `RemoteConfigError`.
- The local client must be OpenSSH `ssh` (Xeda uses `ssh -G`, ControlMaster, `ssh -O check|exit`)
  on Linux or macOS. Native Windows is refused (use WSL). The local Python must be CPython.
- Authentication is the user's `ssh` configuration: keys, agent, `ProxyJump`, certificates. Xeda
  opens one master connection per launch and OpenSSH asks on the terminal, once, for a password,
  passphrase, second factor or new host key. Xeda stores no secret and adds no host key option.
  Never handle or type a secret yourself. With no terminal a prompt cannot be answered: ask the
  user to load a key (`ssh-add`) or to run `! ssh HOST true` once, then run again.
- Host: Linux x86_64 or aarch64, glibc or musl; `sh`, `bash`, `tar`, `curl` or `wget`, `sha256sum`
  or `shasum`; internet access at the first run (GitHub for `uv` and Python, the package index for
  packages); the EDA tools on the login `PATH` or made available by `setup` lines; Docker for
  dockerized flows.
- First diagnostic: `xeda remote check HOST [--json]`. It installs what is missing and reports
  `platform`, `xeda`, `python`, `environment` (`key`, `code`, `prefix`, `lock_file`, `lock`),
  `disk` (`path`, `free`, `total`), `kill_user_processes` (`yes`, `no`, `unknown`), `executables`
  (`found`, `missing`; the login `PATH`, `setup` is not run), `docker` and `warnings`. A missing
  tool is no error. Exit 0 when the connection and the environment work. It builds the environment
  of Xeda alone, without `tb.python_packages`.

Profile `${XDG_CONFIG_HOME:-~/.config}/xeda/remotes.yaml` (optional; one invalid entry fails the
load for every host with `RemoteConfigError`). The `hosts` table is keyed by the exact text of
`--remote`. Fields, all optional:

- `root`: the remote root; `~/<path>` or an absolute path of `[A-Za-z0-9._/+-]`. Default
  `$XDG_CACHE_HOME/xeda`, else `~/.cache/xeda`.
- `setup`: shell lines that run in the `bash -lc` of every launch, in order (`module load ...`,
  `source settings64.sh`). Not used by the installation or `xeda remote check`.
- `env`: variables for every remote command (proxy, `UV_INDEX_URL`, `SSL_CERT_FILE`, license
  servers). Refused names: `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `UV_CACHE_DIR`,
  `UV_PYTHON_INSTALL_DIR`, `UV_NO_CONFIG`, `XEDA_BOOT_*`.
- `python`: `managed` (default; `uv` installs the local Python version) or the absolute path of a
  Python of the host. Nothing checks that its version matches the local one.
- `flows`: flow settings for this host, shaped like the `flows` section of a design file. It is the
  layer just above the user file. A locator setting (`pdk_root`) reaches the host from here
  only; a local value is never sent.

A cocotb testbench names its Python packages in `tb.python_packages`. The host installs them at the
local versions, and the key of the environment covers them. A package that is not installed locally
is `RemoteDependencyError`, before any connection.

Behavior:

- The host keeps a workspace (`v1/ws/<key>/`) per machine, resolved run root and `ssh -G` user,
  host name and port. Another `--run-root` is another workspace. Runs there are make-like: only
  changed inputs are sent, and a fresh flow is not run again. `--hashed-run-dirs`, `--clean` and
  `--rebuild-all` act on the host. A command waits for the workspace lock and logs the launch
  that holds it.
- Refused before any connection: `--post-cleanup`, `--post-cleanup-purge` (use `xeda remote clean`),
  `--scrub` (a remote launch cannot ask for the confirmation),
  a deliverable setting that names a location (use `--outputs-to`), `rtl.generator.outputs_to` (drop it for the
  remote run, or run locally to get the copies), a chain with a programmer
  flow (`openfpgaloader`; run the part before it with `--remote`, then program locally with a
  binding to the fetched file), `--fetch-all` or `--keep-running` without `--remote`, and
  `--keep-running` with `--dry-run`.
- The mirror is `<run root>/@<host>/<design>[/<target>]/<flow>[_<hash>]`, `<host>` being the text of
  `--remote` with each character outside `[A-Za-z0-9._@-]` replaced by `_`. Every flow of the plan
  gets `results.json`, `settings.json`, the top-level `*.log` files, `logs/*.log` and `reports/`;
  the requested flow also its declared outputs and artifacts. A failed flow returns only the files its run wrote.
  An artifact outside the run directory never comes back; the results keep its path on the host.
  An artifact that a symbolic link leads out of the run directory does not come back either: the
  results name its place in the mirror, where no file lies, and `--outputs-to` skips it.
  `--fetch-all` fetches every file except `trace.json`. The mirror has no `trace.json`, so a local
  run never reuses it. Paths in `results.json` and `settings.json` name the mirror where Xeda can
  map them. `.launches/<id>.json` in the mirror is the record that `attach` needs.
- `--outputs-to DIR` delivers from the mirror after a success.
- `--json` writes one document: the document of a run. `run_dir` and the paths in the records name
  the mirror, `design_file` is the local file, and `launch_id` is the id of the launch on the host.
  A failed remote step after the launch was sent has the outcome `failed` and the code
  `remote_failed`; before it, the host is unreachable or cannot be set up (`remote_unreachable`,
  `remote_setup_failed`): no launch starts, so the outcome is `null`, there are no nodes and the
  exit status is 2. A launch that ended but could not be fetched or delivered has
  `remote_fetch_failed` or `delivery_failed` and keeps its records; Ctrl-C gives `canceled`;
  `--keep-running` then Ctrl-C gives the outcome `detached` and `remote_detached`. A remote dry run
  prints the dry-run document and no other key; each node of the plan has `run_dir` (in the mirror),
  and the log lists the uploaded files. Tool output goes to stderr.
- `--dry-run --remote HOST` plans and judges freshness on the host. It may install Xeda there and
  sync the inputs; it writes no run directory.
- Ctrl-C asks the host to cancel (SIGINT, after 15 s SIGTERM, after 10 s more SIGKILL, to the
  launch and its descendants). A second Ctrl-C closes the connection, and the host cancels. A lost
  connection cancels too (heartbeat every 5 s, 30 s lease). Recovery is the same command again:
  a finished run is reused and only the fetch repeats; a canceled run starts over.
- `--keep-running` keeps the launch on the host when the connection ends or Ctrl-C stops the follow.
  The document has the outcome `detached`, the code `remote_detached` and the `launch_id`. Then
  `xeda remote attach HOST [LAUNCH_ID] [--run-root DIR] [--json]` shows the log from its start and
  fetches the result into the mirror (exit 0 on success; it delivers nothing, so run the original
  command again to deliver), and `xeda remote cancel HOST [LAUNCH_ID] [--run-root DIR] [--json]`
  stops it (document `success`, `host`, `launch`, `survivors`; exit 1 if a process survived
  SIGKILL). Both need the `--run-root` (env `XEDA_RUN_ROOT`) of the `xeda run` that started the
  launch, and `attach` needs the record in the mirror. Running the same request again follows the
  running launch (same options and input content; log verbosity does not count); Ctrl-C then only
  stops the follow. `KillUserProcesses=yes` on the host (`check` reports it) may kill the launch at
  logout unless `loginctl enable-linger` is set.
- `xeda remote clean HOST [--older-than DAYS] [--all] [--yes] [--json]` removes unused
  environments, code copies and workspaces with their run directories. It lists, asks, and removes;
  `--yes` and `--json` do not ask. The document has `success`, `host`, `removed` and `kept`; an
  entry has `kind` (`env`, `code`, `workspace`) and `path`, and a `kept` entry a `reason`.

## When a run fails

A launch that does not pass exits 1 with `outcome` and `error`. A request that starts no launch has
`outcome: null`, exits 2 and has the `spec` it could read (`null` for a usage error); a Ctrl-C
before the launch started exits 1 with the code `canceled`. Branch on
`error.code`, a stable lower snake case text (the set is open: keep an unknown code as text).
`references/troubleshooting.md` has the messages and fixes. `dse`, `scrub`, `pdk`, `remote check`
and `list-*` keep their own documents: on failure they have `success: false` and
`error.type`/`error.message`.

| `error.code` | Do |
| --- | --- |
| `unknown_setting`, `invalid_value`, `invalid_quantity`, `boolean_text`, `list_text`, `required_setting`, `conflicting_keys`, `unscoped_setting`, `unknown_node` (`error.suggestions` holds the address to write), `removed_setting`, `removed_alias`, `unsupported_setting`, `shared_setting_conflict` (no launch; `error.node` is the flow) | Read the message; it names the setting and the fix. `xeda list-settings <flow> --json` lists valid names. |
| `invalid_local_settings` (a private file `config.yaml` or `*.local.yaml` is invalid) | The message names the file and the key. Fix the file, or pass `--no-config`. |
| `invalid_design`, `design_file_not_found`, `unknown_target`, `target_required` (the project's `default_target` is no target of the design: pass `--target` or `--no-target`), `unknown_design`, `design_name_required`, `use_design_name` (a design file that the project lists: use `--design-name`), `several_project_files`, `invalid_project`, `removed_alias`, `removed_key` (a design or project key that was removed), `fetch_failed` (a Git dependency could not be resolved or fetched: pin its `commit`, or check the URL), `dependency_cycle` (the dependencies lead back to a design on the chain that the message names: remove one entry of the cycle) | The message names the key (`rtl.sources:`) or the file, and the key to write. Check the file against `xeda design-schema`. |
| `flow_not_found`, `design_file_not_found`, `project_file_not_found`, `usage_error` (exit 2, `spec: null`) | Unknown flow, missing file, no design given, unknown option, removed option. The message suggests close matches. |
| `invalid_chain`, `several_roots_unsupported`, `repeated_flow`, `no_edge`, `unknown_output`, `ambiguous_output`, `cardinality_mismatch`, `type_mismatch` | A chain that cannot be planned. Read the message. |
| `invalid_run_root`, `delivery_refused`, `delivery_failed`, `cleanup_failed` | The message names the directory or setting and the fix. |
| `nonzero_exit` | The tool exited non-zero; read its log in `run_dir` of the node `error.node`. |
| `timeout`, `executable_not_found` | Raise the limit. Install the tool, or add `-s <flow>.docker=true`. |
| `reported_failure` | The tool exited 0 and its reports or checks say it failed, or the flow raised its own error. |
| `missing_generated` | A source of `rtl.sources` that the generator should write matches no file in its output. A plain entry must be a file the generator wrote below the design root in this generation (a tool that skips an up-to-date output needs `make -B`); a `$XEDA_GENERATED/<name>` entry must be written into the storage. Or list only the generated entries in `generated_sources` (empty, every entry is the generator's). |
| `generator_failed` (`error.node` is null, every node `skipped`) | The generator failed or could not be judged. The message names it and its exit status; its output is in the log. |
| `missing_report`, `missing_output`, `invalid_metric` | A report the flow reads or a declared output was not written by this run, or a metric is not a finite value of its type. |
| `rejected` | The checks judged the results negative. The metrics are in the record; change the design or settings. |
| `producer_failed` (on a `blocked` node) | A producer did not pass. The top-level `error` is the producer's own; read its `record`. A blocked flow does not run and keeps its earlier `results.json`. |
| `remote_failed`, `remote_fetch_failed`, `remote_detached`, `canceled` | A `--remote` run failed outside the flow, could not fetch its result, keeps running on the host, or was canceled. The message names the step and the cause; `references/troubleshooting.md` has the fix. |
| `remote_unreachable`, `remote_setup_failed`, `unsupported_with_remote` (exit 2, outcome `null`, no nodes) | The host cannot be reached or set up, or the request cannot run on a host: a flow that programs a device here, or (in Python) `prune_variants`, `post_cleanup` or `post_cleanup_purge`. Fix the host (`xeda remote check HOST`), or run that flow here. |
| `storage_error` | A file or directory error of the operating system, or a path that leaves the run directory, ended the node (the record may be missing). Check the disk and permissions. |
| `internal_error` | A bug of Xeda. Report it with the message. |

Add `--log-level 10 --detailed-logs` for debug logs. Flow names and setting names are exact: a
second spelling is refused, and the error names the right one.

## References

| Open | When |
| --- | --- |
| `references/design-file.md` | Writing or fixing a design or project file; source types; clocks; targets; dotted keys |
| `references/fpga-flows.md` | yosys_fpga, nextpnr, fpga_pack, openfpgaloader, openXC7, boards, pin constraints, `vivado_impl` |
| `references/troubleshooting.md` | A failure, a simulation verdict, a waveform setting, a number that looks wrong |
| `references/flows.md` | A flow's category, producers and result keys (generated by `xeda skill install`) |

## Library

```python
from xeda import Design, DefaultRunner
from xeda.introspect import flows_info, settings_info, results_info, design_schema

design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
flow = DefaultRunner("xeda_run").run("vivado_synth", design, flow_settings=["vivado_synth.clock.period=5ns"])
if flow and flow.results.success:
    print(flow.results.fmax)
```

A `Design` and everything in it is read-only (assigning raises an error). Change a copy with
`design.model_copy(update=...)`, then call `invalidate_cached_properties()` on it.

`xeda.introspect` returns the data the CLI's `--json` renders.
