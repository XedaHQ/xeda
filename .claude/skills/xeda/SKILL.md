---
name: xeda
description: Use when running, configuring, or debugging Xeda EDA flows - simulation (GHDL, NVC, Verilator, ModelSim, VCS, xsim, Bluesim), Bluespec (bsc), FPGA synthesis and implementation (Vivado, Quartus, Diamond, ISE, yosys+nextpnr, openXC7), ASIC synthesis and place and route (yosys, Design Compiler, OpenROAD) - and when writing or fixing a Xeda design file (`*.yaml`; TOML and JSON also load) or project file (`xedaproject.yaml`). Also use for reading a run's `results.json`, interpreting Fmax/timing/utilization numbers, and fixing a failed run (`FlowSettingsError`, `DesignValidationError`, `ExecutableNotFound`, `NonZeroExitCode`, `FlowFailed`).
---

# Driving Xeda

Xeda runs one flow (simulation, synthesis, implementation) on one design file. Query the CLI for
names, settings and result keys; do not guess them. An unknown setting is an error.

## Machine-readable route

`--json` always means "one parseable document on stdout".

| Need | Command |
| --- | --- |
| Flows, with aliases, default producers, declared I/O and chain relations | `xeda list-flows --json` |
| Settings of a flow (name, alias, type, default, enum, `writes`, `common`, `tier`, `group`) | `xeda list-settings <flow> --json` (`--no-common` omits the settings every flow has; `--tier common` keeps the most used) |
| Result keys of a flow | `xeda list-results <flow> --json` |
| Design file schema | `xeda design-schema` |
| Boards, ASIC platforms, DSE optimizers | `xeda list-boards --json`, `xeda list-platforms --json`, `xeda list-optimizers --json` |
| Which PDK files of a platform are installed, and where | `xeda pdk list --json`, `xeda pdk path <platform> --json` (fails with the install command when they are missing) |
| Install the PDK files of a platform (downloads and writes the PDK store; propose it to the user first) | `xeda pdk install <platform> --json` (installs without asking; reports `already_installed` when the files are there; `--provider lambdapdk` installs `sky130hd` from lambdapdk, which `-s pdk_provider=lambdapdk` then selects) |
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
and `skipped` paths. The Python API never prompts: `scrub_design` and a launch with
`scrub_old_runs` remove nothing unless the caller passes `confirm` / sets `confirm_scrub`.

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=5ns --json | jq '.results.fmax'
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
run or a Git fetch; materialize that design first. With `--remote HOST`, the host makes the plan
and judges freshness; the dry run may install Xeda on the host and sync the inputs, and it reports
what it uploaded (`uploaded`).

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
  `{file: "top.json", type: JsonNetlist}`. A source a generator writes is `{path: "gen/top.v"}`.
- A non-cocotb HDL testbench needs `tb.top`; simulation flows refuse the design without it
  (`ghdl_sim` finds a VHDL testbench's top itself).
- Give the clock port in `rtl.clock.port` and the period in the flow: `clock.period` (`5 ns`) or
  `clock.freq` (`200 MHz`). For several clocks, use `rtl.clocks` and the flow's `clocks.<name>`.
- A physical setting needs a unit: `clock.period: 5 ns`, `-s clock.period=5ns` (no space, so no
  quotes), `abc.load: 3.9 fF`. A bare number fails with `` `5` needs a unit, such as `10 ns` ``.
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

Layers, lowest to highest: flow defaults, project `flows.<flow>`, design `flows.<flow>`, selected
target's `flows.<flow>`, `-s`, API. They merge key by key, so `-s clock.freq=100MHz` refines a
nested section. Write a flow's settings under its own `flows.<flow>` only.

```bash
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=4.5ns synth.strategy=Flow_PerfOptimized_high
xeda run nextpnr blinky.yaml -s flows.yosys_fpga.flatten=true
```

- `-s` takes `KEY=VALUE` items up to the next option or the first token that is not `KEY=VALUE`.
  Put the design before `-s`, or end the options with `--`.
- A dotted key reaches a nested setting. `-s flows.<flow>.<key>=<value>` sets any flow of the run (a
  producer too); `-s <key>` and `-s flows.<requested>.<key>` are one setting.
- A setting of a settings group has a dotted name, such as `abc.strategy` or
  `sequential.clock_gating.enabled` for `yosys`. Its `tier` (`common`, `advanced` or
  `expert`) says how often a user needs it; it affects the listing, never what Xeda
  accepts. The tier `common` is not the `"common": true` key. For the most used
  settings of `yosys`, run `xeda list-settings yosys --tier common --json`.
- A setting with `"opaque_keys": true` (`set_synth_properties` and `set_impl_properties` of
  `vivado_synth` and `vivado_project`, ISE `*_options`, and the dictionaries of `openroad` keyed by
  net or instance name: `ir_drop.nets`, `ir_drop.vsrc_files`, `ir_drop.instance_power`,
  `ndr.assignments`) takes a dotted name as one key:
  `-s set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0` or
  `-s ir_drop.instance_power.u_core.reg_1=1mW`. Quote a name with a space.
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
    says to delete it. On the simulator flows, `timeout` replaces `timeout_seconds`; no other flow
    limits a tool's run time. `no_console` has no replacement.
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
`-s flows.vivado_power.inputs.activity=vivado_postsynth_sim.saif`; `timing_sim` is no setting of
`vivado_power`.

Related inputs of one flow must come from one producer (`same_producer_as`, `via` in `list-flows`):
`vivado_postsynth_sim`'s `netlist`, `netlist_timing` and `sdf`, and `vivado_power`'s `checkpoint`
with the synthesis behind its `activity`. A binding that splits them is refused; the message lists
the `-s flows.<node>.inputs.<input>=<producer>.<output>` bindings that fix it.

Shared settings on declared edges (`fpga`, `board`, `custom_boards_file`, `clocks`, `prjxray_db`,
`platform`, `corner`, `dont_use_cells`, `platform_file`, `liberty`, `pdk_provider`, `pdk_version`,
`pdk_root`, `hierarchical`, `wire_rc`, where both nodes declare them) must agree. `platform` is a
bundled name; `platform_file` and `pdk_root` compare by resolved path; `liberty` compares as the
list of resolved paths in order; an unset `pdk_provider` or `pdk_version` is filled in from the
platform before hashing. `hierarchical` and `wire_rc` are the
OpenROAD flows' (the database stores neither). Disjoint leaves combine; two values for one leaf
fail and name both origins. A command-line leaf (`-s fpga.part=...` or
`-s flows.yosys_fpga.fpga.part=...`) wins for the connected group. A shared setting written under
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
default: `FlowFailed` with `rejected`, reported again without a tool run until something
changes. A failed stage leaves `<flow>-failed.odb` in its run directory.
`examples/bluespec/gcd/gcd.yaml` is a working design file for nangate45: its `flows.yosys` section
sets `platform` and `clock.period`, and `bsc` converts the design first.

## Results

`results.json` in the run directory is authoritative. Every flow reports `success`, `runtime`,
`tools`, `artifacts`, `design`, `flow`, `run_path`, `timestamp`, `design_hash`, `flow_hash` and
`settings_hash`; the rest is flow-specific (`xeda list-results <flow> --json`). Every metric
name is lower snake case, with dots between the parts of a group (`fmax`, `lut`, `ff`,
`lut_use.logic`); a quantity has one name in every flow.

- `fmax` is the frequency achieved; `clock_frequency` is the one constrained. Compare `fmax` and
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
| `results.json` | the parsed results; a failed run writes `success: false` and `error.code`/`error.message`; a run that its checks rejected keeps `trace.json`; an unchanged launch reports it again (state `rejected again`); `--rebuild-all` runs it |
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

`--remote HOST` (env `XEDA_REMOTE`) runs the whole request on a Linux host over SSH and fetches the
results. It takes everything a local run takes: chain, bindings, target, `--outputs-to`,
`--dry-run`. Use it when the tools, licenses or cores are on a server. The host needs no Xeda: the
first run installs `uv`, the local Python version and the local package versions under the remote
root `${XDG_CACHE_HOME:-~/.cache}/xeda/v1`, without administrator rights.

Access:

- `HOST` is an OpenSSH destination: an alias of `~/.ssh/config`, `user@host` or
  `ssh://user@host:port`. Text that is empty, starts with `-` or has a space is `RemoteConfigError`.
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
  lowest layer above the flow defaults. A locator setting (`pdk_root`) reaches the host from here
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
  a deliverable setting that names a location (use `--outputs-to`), a chain with a programmer
  flow (`openfpgaloader`; run the part before it with `--remote`, then program locally with a
  binding to the fetched file), `--fetch-all` or `--keep-running` without `--remote`, and
  `--keep-running` with `--dry-run`.
- The mirror is `<run root>/@<host>/<design>[/<target>]/<flow>[_<hash>]`, `<host>` being the text of
  `--remote` with each character outside `[A-Za-z0-9._@-]` replaced by `_`. Every flow of the plan
  gets `results.json`, `settings.json`, the top-level `*.log` files and `reports/`; the requested
  flow also its declared outputs and artifacts. A failed flow returns only the files its run wrote.
  A file outside the run directory comes from a flow that succeeded, under `_external/<digest>/`.
  `--fetch-all` fetches every file except `trace.json`. The mirror has no `trace.json`, so a local
  run never reuses it. Paths in `results.json` and `settings.json` name the mirror where Xeda can
  map them. `.launches/<id>.json` in the mirror is the record that `attach` needs.
- `--outputs-to DIR` delivers from the mirror after a success.
- `--json` writes one document: the document of a run plus `remote` (the text of `--remote`) and
  `mirror`; paths name the mirror and `design_file` is the local file. A failed remote step has
  `remote` and no `mirror`. A remote dry run adds `uploaded` (the input files it sent), and each
  `plan` node has `run_path` (in the mirror) and `remote_run_path`. Tool output goes to stderr.
- `--dry-run --remote HOST` plans and judges freshness on the host. It may install Xeda there and
  sync the inputs; it writes no run directory.
- Ctrl-C asks the host to cancel (SIGINT, after 15 s SIGTERM, after 10 s more SIGKILL, to the
  launch and its descendants). A second Ctrl-C closes the connection, and the host cancels. A lost
  connection cancels too (heartbeat every 5 s, 30 s lease). Recovery is the same command again:
  a finished run is reused and only the fetch repeats; a canceled run starts over.
- `--keep-running` keeps the launch on the host when the connection ends or Ctrl-C stops the
  follow. The failure is `RemoteDetached` and the document has `launch` (`id`, `host`). Then
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
| `FlowFailed` | The requested flow ran and did not succeed. `results.error` has the cause as a stable `error.code` (with `message`, `node`, `exit_status`, `log`): `nonzero_exit` (the tool exited non-zero; read its log in `run_path`), `timeout`, `executable_not_found`, `reported_failure` (the tool exited 0 and its reports or checks say it failed), `storage_error` (a file or directory error of the operating system), `missing_report` (a report the flow reads was not written by this run), `rejected` (the checks judged the results negative), `missing_output` (a declared output is missing or was not written by this run). |
| `FlowDependencyFailure` | A producer failed or its output could not be verified (`producer_failed` in the node's `results.error`). Read the producer's `results.json`, named in the message. |
| `RunRootError`, `RunDirectoryError`, `DeliveryError`, `OutputExistsError` | The message names the directory or setting and the fix. |
| `NoSuccessfulRun` | `xeda dse` found no successful run. Inspect the attempted runs and adjust the search. |
| `RemoteConnectionError`, `RemoteProvisionError`, `RemoteDependencyError`, `RemoteIncompatible`, `RemoteCanceled`, `RemoteInterrupted`, `RemoteDetached`, `RemoteFetchError`, `RemoteConfigError`, `RemoteProtocolError`, `RemoteError` | A `--remote` run failed outside the flow. The message names the step and the cause; `references/troubleshooting.md` has the fix for each type. |

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

design = Design.from_file("examples/vhdl/sqrt/sqrt.yaml")
flow = DefaultRunner("xeda_run").run("vivado_synth", design, flow_settings=["clock.period=5ns"])
if flow and flow.results.success:
    print(flow.results.fmax)
```

`xeda.introspect` returns the data the CLI's `--json` renders.
