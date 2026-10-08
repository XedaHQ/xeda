---
name: xeda
description: Use when running, configuring, or debugging Xeda EDA flows - simulation (GHDL, NVC, Verilator, ModelSim, VCS, xsim, Bluesim), Bluespec compilation (bsc), FPGA synthesis (Vivado, Quartus, Diamond, ISE, yosys+nextpnr, OpenXC7), or ASIC synthesis (OpenROAD, Design Compiler) - and when writing or fixing a Xeda YAML design file (`*.yaml`; TOML and JSON are accepted) or project file (`xedaproject.yaml`; `.yml` and `.toml` are accepted too). Also use for reading a run's `results.json`, interpreting Fmax/timing/utilization numbers, or diagnosing `FlowSettingsError`, `FlowNotFoundError`, or `ExecutableNotFound`.
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
xeda run vivado_synth sqrt.yaml -s clock.period=5.0 --json
```

`xeda run --json` emits one object: `design` (the design's name) and `design_file` (the design
file you named, absolute, or `null`), `success`, `results`, `run_path`, `results_json`,
`settings_json` - or `success: false` plus an `error` object. Exit status is non-zero on failure.

```bash
xeda run vivado_synth sqrt.yaml --json | jq '.results.Fmax'
```

## Writing a design file

`xeda design-schema` is the authoritative JSON Schema. The shape:

```yaml
name: sqrt # optional (defaults to the file's stem); names the run directory
description: ...
language:
  vhdl:
    standard: '2008' # or language.verilog.standard
rtl:
  sources: ['pkg.vhdl', 'sqrt.vhdl'] # required, IN COMPILATION ORDER
  top: sqrt # required by synthesis flows
  clock: {port: clk} # names the clock PORT, not its period
  parameters: {G_IN_WIDTH: 32} # or `generics`; use one spelling, not both
tb:
  sources: ['tb_sqrt.py'] # a .py source is detected as cocotb automatically
  top: tb_sqrt # required by simulation flows for non-cocotb testbenches
flows:
  vivado_synth: # per-flow settings, applied only for that flow
    fpga:
      part: xc7a100tftg256-2L
    clock:
      period: 5.0
```

YAML uses the 1.2 core schema. Quote strings such as `"010"`, `"0x1F"` or `"1e3"`
when they are text; `yes/no/on/off` stay strings, and booleans are `true`/`false` only
(`debug: yes` and `debug: 1` are errors, never `true`).
Duplicate keys and non-string mapping keys are errors. Ordinary aliases and core explicit
tags are accepted; merge keys, recursive aliases and non-core tags are rejected.
TOML and JSON designs/projects remain accepted.

Key points that are easy to get wrong:

- **Paths resolve against the design file's directory**, not the working directory.
- **`sources` order is compilation order.** VHDL packages must precede their users.
- **Clocks split in two.** `rtl` names the clock *port*; the *period or frequency* is a flow
  setting, because it constrains a particular build. Single clock: `clock: {port: clk}` in
  `rtl` plus `clock.period` (ns) or `clock.freq` per flow. The legacy `clock_port` spelling is
  accepted as compatibility input. The legacy `clock_period` spelling is also accepted for flow
  settings. Within one settings layer, do not combine a compatibility spelling with its canonical
  counterpart; across layers the higher-precedence spelling wins and is merged into canonical
  `clocks`.
  Prefer `clock.period` or `clock.freq` in new files. Multiple: a list under `rtl.clocks` plus a
  `clocks` mapping in the flow settings.
- Every source needs a type: suffix inference is case-sensitive; unknown or ambiguous suffixes
  (`.json`, `.bin`, `.cfg`, `.config`) need an explicit `type`. Use `Data` for files with no
  automatic HDL frontend (a tool or the design can still read them). Invalid explicit types fail
  with suggestions; type names themselves are case-tolerant.
- Give a source a table instead of a string when inference is not enough:
  `{file: "legacy.v", type: SystemVerilog}`. Use `path:` instead of `file:` for a source a
  generator will produce (it is not checked for existence).

- **One file, several boards:** `targets.<name>` is an overlay on the design (its own keys,
  merged over it; `sources` appended), selected with `--target NAME` on `run` and `dse`. A design
  with one target needs no flag; with several, the error lists them. `--json` documents carry
  `target`. A selected target has run directories of its own, `<design>/<target>/<flow>`;
  `xeda scrub FLOW DESIGN --target NAME` removes only that target's.

See `references/design-file.md` for the full reference.

## Settings

Precedence, lowest to highest: flow defaults -> `xedaproject.yaml`'s `flows.<flow>` -> the
design file's `flows.<flow>` section -> the selected `--target`'s `flows.<flow>` (it overrides the
design, key by key) -> command-line `-s` -> the API. Layers merge key by key, so
`-s clock.freq=100MHz` refines a nested section instead of replacing it. A flow's settings are
written under its own `flows.<flow>`, never nested in another flow's (`nextpnr.yosys`, `vivado_postsynth_sim.synth` and `vivado_power.postsynthsim` were removed, each naming its replacement). `-s flows.<flow>.key=value` sets a setting of any flow in the run (the
requested flow or a declared dependency; a typo is an error with suggestions), and `-s key` and
`-s flows.<requested>.key` are one setting. `-s` takes space-separated KEY=VALUE items and stops
at the next option or the first token that is not KEY=VALUE, so put the design before it or end
the options with `--`.

```bash
# dotted keys reach nested settings; several can be given at once
xeda run vivado_synth sqrt.yaml -s clock.period=4.5 synth.strategy=Flow_PerfOptimized_high
```

Every flow also accepts `ncpus` (alias `nthreads`), `dockerized`/`docker`,
`quiet`/`verbose`/`debug`, `redirect_stdout` and `lib_paths`. `xeda list-settings <flow> --json`
marks these with `"common": true`; `--no-common` omits them.

A setting may have an `alias`; both names work (`vcd`/`waveform`, `nthreads`/`ncpus`).

## Flow dependencies

Run the flow you want, not the chain leading to it - dependencies run automatically:

```
openfpgaloader -> fpga_pack -> nextpnr -> yosys_fpga
vivado_power   -> vivado_postsynth_sim -> vivado_synth
openroad       -> yosys
```

Set a dependency's settings in its own section, `-s flows.<flow>.<setting>`:

```bash
xeda run openfpgaloader blinky.yaml -s flows.yosys_fpga.flatten=true
```

`openroad` declares its netlist input from `yosys.netlist`; a typed `VerilogNetlist` source
skips synthesis. Synthesis settings belong in `flows.yosys`: the removed `openroad.blocks`
setting names `flows.yosys.black_box`. `yosys_fpga`, `nextpnr`, `fpga_pack` and
`openfpgaloader` declare file I/O. `fpga_pack` packs
`nextpnr`'s configuration (or a typed `EcpConfig`/`IceAsc`/`Fasm` source) into a bitstream and
programs nothing; `openfpgaloader` programs `fpga_pack`'s bitstream, or a typed `Bitstream`
source, and builds nothing. The `open_xc7` flow was removed: use `fpga_pack` to build,
`openfpgaloader` to program. `nextpnr` handles Xilinx 7-series with openXC7's
`nextpnr-himbaechel`. `nextpnr` takes its `netlist` from
`yosys_fpga`'s checked output record, or from exactly one
`{file: "top.json", type: JsonNetlist}` in `rtl.sources` (which skips synthesis).
`nextpnr.config` records the selected ECP5 `textcfg`, iCE40 `asc` or Nexus/Xilinx `fasm`; a missing or
stale enabled configuration fails the run.

Xilinx 7-series (openXC7 1.0: its `yosys`, `nextpnr-himbaechel` and `fpga-as` on `PATH`):

```yaml
name: blinky
rtl:
  sources: [blinky.v, blinky.xdc]   # pins as a typed Xdc source
  top: blinky
flows:
  yosys_fpga:
    fpga: {part: xc7a100tcsg324-1}  # the full part: device, package, pins, speed grade
  nextpnr:
    clock: {port: clk, freq: 100MHz}
```

`xeda run fpga_pack blinky.yaml` builds `outputs/blinky.bit` and programs nothing. The first
build under a run root generates the die's chip database into `<run root>/.cache/xilinx-chipdb/`
(about a minute and 3.5 GB of memory for an `xc7a100t`); every later launch, of any design
there, reuses it. `chipdb` names an existing database file instead; `prjxray_db` another
Project X-Ray root. A bundled board has a lower-case name and is found by it in any letter case
(`ARTY_A7_100T` is `arty_a7_100t`); `xeda list-boards` lists them with their name in openFPGALoader.
`board: arty_a7_100t` (or `arty_a7_35t`) gives the part and, with no `Xdc`
source, Digilent's pin names (`CLK100MHZ`, `led[0]`, ...), which the design's ports must then use.
`basys_3` (`xc7a35tcpg236-1`) and `stlv7325_v2` (`xc7k325tffg676-2`) do the same, for the clock,
every user LED, the user buttons and the UART only. `basys_3`: `clk`, `led[15:0]`, `btnC` (the reset
button), `btnU`, `btnL`, `btnR`, `btnD`, `RsRx`, `RsTx`; LEDs and buttons are active high.
`stlv7325_v2`: `clk_p` and `clk_n` (a differential clock: the design needs an `IBUFDS` and a
`BUFG`), `led[7:0]`, `btn[1:0]` (`btn[0]` is the reset button), `uart_rx`, `uart_tx`; LEDs and
buttons are active low. Neither file has a `create_clock`: give the clock in the flow settings.
`openfpgaloader` loads SRAM by default; `write_flash` programs flash and `verify` needs it.
It needs openFPGALoader 0.13.0 or newer (it asks `-V` before it programs and refuses an older
one). It fails on a nonzero exit, and also when `openfpgaloader.log` shows the device was not
programmed although the loader exited 0 (v1.1.1 does that): DONE low after a Xilinx load
(`ir: ... done 0`, with the ID or CRC error the FPGA reports), a step that printed `FAIL`, an
`Error:` message. The error is `ReportedFailure`; it quotes the lines. A load that fails in
another way passes: read the log.
A `flows.open_xc7` section fails as the removed flow's own name does, naming the replacement.

Put pin constraints in `rtl.sources` as typed `Lpf`, `Pcf`, `Pdc` or `Xdc` files. nextpnr
merges the selected family's files in source order, falling back to its board's file when
none are supplied. The former `lpf_cfg`, `pcf_cfg` and `pdc_cfg` settings are removed.
Typed `Sdc` sources are merged in source order, followed by the optional `sdc` setting's
file; the combined timing file reaches `--sdc` once. A clock must have one timing authority
across pin files, SDC files and settings: duplicates name the original files/lines and
clock setting/period. File-only clocks suppress generated frequency hints. A clock port
without a period is a declaration; it does not supply a physical timing constraint.

`custom_boards_file` is your own board database, used instead of the bundled one (which stays
TOML): TOML or YAML by suffix (`.toml`, `.yaml`, `.yml`; any other suffix is an error), read as
YAML 1.2 like every YAML file xeda reads. A relative path resolves against the design directory,
and a board's local `lpf` against the database file's directory. Its board names are
case-sensitive, as written. A board's entry has `fpga`, `lpf` or `xdc`, and optionally
`openfpgaloader_board`, the board's name in openFPGALoader (the former `name` key is an error that
says so). `openfpgaloader` passes it as `--board`, and no `--fpga-part`, when `board` is set and
no `cable` is. A `cable` takes precedence: the flow passes `--cable` and no board name. A board
without an `openfpgaloader_board` adds no `--board`. Without `--board`, a known part is
`--fpga-part`, a Xilinx part without its speed grade (`xc7a35tcpg236`); with no part known,
openFPGALoader detects the device.
`write_flash` needs the device (`fpga`, or a `board` that gives one) and is refused without it.

Shared settings on declared edges (`fpga`, `board`, `custom_boards_file`, `clocks`, `prjxray_db`,
`platform`, `corner`, `dont_use_cells`, where both nodes declare them) must agree: disjoint leaves combine; different values for one leaf fail,
naming both origins. An explicit command-line leaf (`-s fpga.part=...` or
`-s flows.yosys_fpga.fpga.part=...`) wins for the connected group, preserving other leaves.
API overrides remain the highest-precedence origin.
A consumer holds each completed producer for reading until its launch ends (POSIX only),
so another Xeda process that would rebuild, clean or scrub that directory waits.

`xeda list-flows --json` reports `dependencies`, `inputs` and `outputs`.

## Flow chains and input bindings

`xeda run` takes a chain: `xeda run yosys_fpga+nextpnr+fpga_pack design.yaml`. The last flow is the
one requested (`flow`, `results`, `--help-settings` and the exit status are its); each flow before
it supplies the next one's compatible **required** inputs. A chain is a path, not waypoints: `+`
is the only separator, each flow appears once, `FLOW.OUTPUT` picks one output of a producer
(`nextpnr.config+fpga_pack`), and nothing searches for a missing stage. A refused chain suggests a
valid one only when another output of the producer, or the flows that the required default
producers name between the pair, fix it (`nextpnr+openfpgaloader` ->
`nextpnr+fpga_pack+openfpgaloader`); otherwise the refusal carries no suggestion. A flow that
programs a device (`openfpgaloader`) can only end a chain, and only flows with declared I/O chain
(`xeda list-flows --json`: `can_follow`, `can_precede`); `bsc`, `bsc_sim`, `vivado_project` and
`vivado_sim` run alone, and a chain through one is refused (no compatible output, or the next flow takes no required input). A chain is local: `--remote` and
`xeda dse` refuse it.

`-s` sets the last flow; `-s flows.<flow>.key=value` sets any other flow of the chain. With
`--json`, `request` lists the elements and every node has its `node` name and `inputs` (each with
`origin`, `producer`, `output`, `binding_origin`); nodes planned but never entered after a failure
show `"state": "not run"`. A build-only chain (`yosys_fpga+nextpnr+fpga_pack`) programs nothing.

Each input is supplied, in this order, by an **explicit binding** (a chain adjacency, or
`flows.<consumer>.inputs.<input>: <producer>[.<output>]` saved in a design or project file, given
as `-s flows.<consumer>.inputs.<input>=...`, or through the API), else a **typed source** in
`rtl.sources` (a `JsonNetlist` skips synthesis for `nextpnr`), else the declared **default
producer**. A binding names a flow's output, never a file: give a file as a typed source.
`inputs` is wiring for the resolver, not a setting: it is not in `xeda list-settings` or
`settings.json`.

```yaml
# prebuilt_demo.yaml: `xeda run nextpnr prebuilt_demo.yaml` skips synthesis;
# `xeda run yosys_fpga+nextpnr prebuilt_demo.yaml` synthesizes anyway.
name: prebuilt_demo
rtl:
  top: top
  sources:
    - file: top.json
      type: JsonNetlist
flows:
  nextpnr:
    fpga:
      part: LFE5U-85F-6BG381C
```

A chain is command-line data: it replaces a binding saved in a design or project (the plan's
`overridden` lists them), and it is an error, even when equal, against a command-line or API
binding of the same input, naming the chain position and where the other was given. A project file
is read with `--xedaproject`:

```yaml
# project.yaml
flows:
  nextpnr:
    inputs:
      netlist: yosys_fpga
```

Do not suggest Bluespec chains (`bsc+yosys_fpga+...`) or an `inputs.design` binding: they are
refused until those flows declare their inputs and outputs. `vivado_synth+openfpgaloader` is a
valid chain (`vivado_synth` declares its `bitstream`), but it programs hardware: plan it with
`--dry-run` only.
To check a chain, use `--dry-run` or end it at `fpga_pack`: `openfpgaloader` programs hardware.

## Planning without running

```bash
xeda run nextpnr blinky.yaml --dry-run --json
```

This prints `success: true`, `dry_run: true`, and a `plan` with `requested` and ordered `nodes`:
producers first, run directories, hashes, declared input origins (`source`, `producer`, `none`)
and `switched_on` optional outputs. It runs no tools and changes no run root, marker, lock or
delivery. Conflicting settings and impossible targets produce the usual failure document.

A design that would run a generator or fetch a Git dependency while loading is refused before
side effects. Materialize it first; the library can plan an already materialized `Design`.
Freshness is not evaluated in a plan.
`--dry-run --remote` is refused.

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

Declared outputs appear under the CLI's `results.outputs` and `results.json`'s top-level `outputs`:
each name maps to `{"path": ..., "sha": ...}`
(or an ordered list of those). Only enabled outputs verified as readable files written by that
run inside its directory are recorded. Consumers verify records and content under a read lease;
a fresh producer uses its matching trace, not current-run write evidence. Output-record validation
failures use `MissingOutput` with the normal `results.json` error and identity. `nextpnr` checks its
enabled configuration earlier (`FlowFatalError` naming the setting/path if the tool did not
write it). These records are bookkeeping, not result metrics. `trace.json` records ordered input
names, source/producer origins and producer identities;
changing a binding invalidates reuse even if the file set stays the same.

Keys beginning with `_` are internal and may change.

## Where output lands

Default `./xeda_run/<design>/<flow>/` (`<design>/<target>/<flow>/` for a design selected as a
target), under the run root `./xeda_run` (`--run-root` moves it).
The first time xeda uses a run root it creates and marks it (`.xeda-run-root`, `.gitignore`,
`CACHEDIR.TAG`); **everything under a marked run root is xeda's -- never keep files of your own in
`xeda_run/`.** A directory named as the run root that already holds files and carries no marker is
refused before anything runs, naming it and the fix. `--hashed-run-dirs` instead appends
the run hash, `<flow>_<hash>` (the flow's settings and where its declared inputs come from), so
variants coexist. A dependency gets its own
directory, a sibling of the flow that launched it. The directory holds the generated tool
scripts, the tool logs, `reports/`, `outputs/`, `checkpoints/`, plus:

- `settings.json` - `flow_settings`, the run's input (every layer merged; re-runnable), and
  `effective_flow_settings`, what the flow made of it. Read the latter first when a run did
  something unexpected.
- `results.json` - what was parsed back out. A run that fails after it starts still writes one
  with `success: false` and `error.type`/`error.message`; check `success` before reading numbers.
- `trace.json` - what the run consumed and produced, present only after a successful run; it is
  what makes rebuilds make-like (see below).

`xeda run --json` reports all three paths, so there is no need to guess.

## Getting a named output out of the run directory

A setting that names an output (`bitstream`, `vcd`, ...) can be given a location -- a path
under `$PWD`, `$DESIGN_ROOT` or `$DESIGN_DIR`, or an absolute path: the run still writes its own copy
inside the run directory, and once the whole run has finished xeda copies it to the location you
named. Moving or renaming that destination later never re-runs the flow. `--outputs-to DIR`
delivers the requested flow's artifacts the same way, each at its path inside the run directory:

```bash
xeda run vivado_synth blinky.yaml -s bitstream=$PWD/blinky.bit
xeda run vivado_synth blinky.yaml --outputs-to ./out
```

A delivery never replaces a directory, a design source, or a file the run itself reads. Two
outputs never go to one destination: a request that names one file twice (two settings, or a
setting and `--outputs-to`; in other letters where the file system ignores case; or one inside
the other) is refused, naming both. An existing file at the destination is
replaced without asking only if it is xeda's own earlier, unchanged delivery; otherwise rerun with
`--overwrite-outputs`, or answer the interactive prompt.

## Rebuilds are make-like by default

Re-running `xeda run` only re-runs a flow whose settings, sources, code, tools or outputs changed
since its last successful run (the default); an unchanged flow's recorded results are reused and
shown as if it had just run. `--rebuild-all` forces every flow to run regardless. The `--json`
document's `nodes` list reports, per flow that was touched, whether it was `"fresh"`, `"ran"` or
`"failed"`, and why (`reason`). A directory a setting names has its contents tracked too (every
entry, recursively, a symbolic link followed); what is not tracked: what a symbolic link *in a run
directory* points to when that is a directory (recorded by its target text, never followed),
programs started indirectly (a compiler under `make`, Python packages such as cocotb), environment
variables, and files a tool finds on its own without reporting them -
`--rebuild-all` is the escape if a rebuild looks wrong. A flow that programs a device, or that is
asked for a fresh random seed (`random_seed: random`, `randomize_seed: true`; seeds default to
fixed values), always runs and says so.

`--clean` empties a flow's run directory before running and forces every flow to run ("make clean,
then make"; it implies `--rebuild-all`). `--xeda-run-dir`, `--cached-dependencies`,
`--incremental`/`--no-incremental` and `--cwd` were removed; giving them fails naming their
replacement (`--run-root`, `--rebuild-all`, `--hashed-run-dirs`, `--clean`, `--outputs-to`).

## Remote runs

`--remote HOST` needs Xeda's 0.4.4 release line or newer and remote protocol 1 or newer on the
host: the remote simulation evidence rule, the FPGA build graph (`fpga_pack`, a programming-only
`openfpgaloader`), the node identity of flow chains (a node's hash counts where its declared
inputs come from), the declared Vivado outputs, and `yosys`'s ASIC configuration with a bundled
platform counted relative to xeda's installation. Xeda
probes the package the remote interpreter actually imports and refuses an older build, or one of
an older protocol, before shipping, with an upgrade error. The remote flow always runs fresh;
`--rebuild-all` is accepted to force local generator loading before shipping, while `--clean` and
`--hashed-run-dirs` remain unsupported because remote runs already use a fresh hashed layout.

## When something fails

`error.type` in the `--json` output names the failure type, usually an exception class:

| `error.type` | Meaning | What to do |
| --- | --- | --- |
| `FlowSettingsError` | A setting is unknown or has the wrong type | `xeda list-settings <flow> --json` and match the name exactly |
| `FlowNotFoundError` | Unknown flow name | The message suggests close matches; `xeda list-flows` |
| `ExecutableNotFound` | The tool is not on `PATH` | Install it, or use `-s dockerized=true` |
| `NonZeroExitCode` | The tool itself failed | Read the tool log in `run_path` |
| `DesignValidationError` | The design file is invalid | Validate against `xeda design-schema` |
| `MissingOutput` | A required or enabled declared output is absent, stale or unreadable | Read `results.json` and the tool log; confirm its output setting |
| `FlowDependencyFailure` | A producer failed or its completed output could not be verified | Read the named producer's `results.json`; rebuild after correcting the cause |
| `FlowFailed` | The verdict at the top of the document: the requested flow itself ran without raising and did not succeed (a raised failure names its class, a failed producer is `FlowDependencyFailure`) | Read `results` and the reports under `run_path` |
| `ReportedFailure` | The cause, in a node's `results.json` (and quoted by its consumers): the flow's own reports or checks failed with no tool error (a missing declared output is `MissingOutput`) | Read the log and reports in that node's run directory |
| `NoSuccessfulRun` | A DSE search found no successful run | Inspect the attempted runs and relax or correct the search settings |
| `RunRootError` | The run root holds files but no marker xeda created, or cannot be written | Move it aside, or create `<dir>/.xeda-run-root` to hand it to xeda |
| `DeliveryError` | A named output could not be delivered: the run did not write it, or the destination is an input, a directory, inside a run root, or named by two outputs | Check the setting's value, and that it points at no input or run root and no other output's destination |
| `OutputExistsError` | The destination holds a file that is not xeda's own unchanged earlier delivery | Rerun with `--overwrite-outputs`, or confirm the interactive prompt |

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

design = Design.from_file("sqrt.yaml")
flow = DefaultRunner("xeda_run").run("vivado_synth", design, flow_settings=["clock.period=5.0"])
if flow and flow.results.success:
    print(flow.results.Fmax)
```

`xeda.introspect` returns the same data the CLI's `--json` renders, as plain Python objects.
