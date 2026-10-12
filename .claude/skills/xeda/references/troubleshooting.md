# Troubleshooting Xeda runs

## Triage order

1. `error.type` and `error.message` of the `--json` document. Usage errors (exit 2) are
   `BadParameter` and `UsageError`.
2. `FlowFailed`: the cause is `results.error` (the same as `error` in `results_json`).
   `FlowDependencyFailure`: open the failed producer's `results.json` (the message names it) and
   start there. `nodes` lists every run directory; a node planned but never entered shows
   `"state": "not run"`.
3. `settings.json` in `run_path`: `flow_settings` is what you asked for, `effective_flow_settings`
   is what the flow made of it.
4. The tool log and the generated scripts in `run_path`. Add `--debug` for verbose logs and a
   traceback.

## Messages and fixes

### Settings

| Message | Fix |
| --- | --- |
| `Extra inputs are not permitted: <name> (extra_forbidden)` | No such setting on this flow. `xeda list-settings <flow> --json \| jq -r '.fields[].name'`. A producer's setting goes under `-s flows.<producer>.<name>=...`. |
| `` `-s flows.<name>.*` names no flow of this run (<flow>) `` | Name a flow of the run (the requested flow or a producer). The error suggests the right one. |
| `` `<name>` was removed: use ... `` | Any removed flow, setting, option or key fails this way. Do what the message says. For `open_xc7`, use `fpga_pack` to build and `openfpgaloader` to program: move its placement settings to `flows.nextpnr`, its synthesis settings to `flows.yosys_fpga` and its pin files to `rtl.sources`. |
| `` `yes` is text, not a boolean: write `true` `` | A boolean takes `true` or `false` only. |
| `cannot interpret 'fast' as a quantity: expected a number, optionally followed by a unit` | Write `5.5`, `5.5ns` or `200MHz`. Units are case-sensitive. |
| a type error on a list or text setting | A setting takes its declared type only. `list-settings` shows `type` and `enum`. A list given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`); `[a, b]` is refused. |
| `` `true` is a switch, and `bitstream` names a file `` | Give a name or location, or leave it unset. |

### Design

| Message | Fix |
| --- | --- |
| `rtl.sources: Value error, source file does not exist: <path>` | Relative paths resolve against the design file's directory. A file a generator writes is `{path: "gen/top.v"}`. |
| `rtl.<key>: Extra inputs are not permitted` | Unknown design key. |
| `<key>: Extra inputs are not permitted` at the root, often with `rtl.sources: Field required` | The flat form (`sources`, `top`, `clock` at the root) beside an `rtl:` section. Write everything under `rtl:`. |
| `` `.json` names several kinds of file ... give its type `` | Add `type` (`{file: "top.json", type: JsonNetlist}`). |
| `` `standard` was removed from a source `` | Use `language.vhdl.standard`. |
| `<flow> reads none of the design's sources: ...` (`NoReadableSource`) | The design lists no source the flow reads. Pick a flow for the design's languages, or give a source its `type`. |
| `<flow> cannot read the design's Vhdl source(s) ...; it reads ...` | Same: `verilator` reads no VHDL; `ghdl_sim`, `nvc` and `ghdl_synth` read only VHDL; `bsc` reads Bluespec and Verilog. A simulator checks `tb.sources` too. |
| `` <flow> needs `fpga`, the target FPGA device ... `` | Give `flows.<flow>.fpga.part` or `-s fpga.part=<part>` (or a `board`). A shared setting travels only along the edges of the run's graph. In `vivado_synth+openfpgaloader` the part under `flows.yosys_fpga` reaches neither node (`yosys_fpga is not part of this run`). Only `nextpnr`, `fpga_pack` and `openfpgaloader` take `board`. |
| `<flow> cannot run cocotb tests; use one of: ghdl_sim, nvc, verilator` | Pick a cocotb simulator, or drop the `.py` testbench. |

### Chains

A malformed chain is `BadParameter` (exit 2) before anything runs.

| Message | Fix |
| --- | --- |
| `` Flow `a` has no compatible output for a required input of `b` `` / `` ... takes no required input `` | The pair does not fit. `Did you mean ...?` names a valid chain. `bsc`, `bsc_sim`, `vivado_project` and `vivado_sim` declare no I/O: run each alone. |
| `` Flow `openfpgaloader` programs a device and can only end a chain `` | Put it last. |
| `` Flow `a` appears more than once `` | A chain is a path; each flow once. |
| a binding error naming the consumer, input and where it was written | `unknown input`, `unknown producer` or `declares no inputs`. A binding names a flow's output (`yosys_fpga.netlist`), never a file. A chain and a command-line or API binding of one input conflict even when equal: bind it in one place. |
| `` vivado_power: `checkpoint` must come from the producer of the `netlist_timing` ... `` | Related inputs come from one producer. Add the `-s flows.<node>.inputs.<input>=<producer>.<output>` bindings the message lists. |

`--remote` and `xeda dse` refuse chains and bindings; run locally.

### Run root, run directories and deliveries

| Message or type | Fix |
| --- | --- |
| `RunRootError: <dir> holds files and is not a run root xeda created (it has no .xeda-run-root)` | Name a new or empty directory with `--run-root`. If it holds only earlier Xeda runs, delete them or create `<dir>/.xeda-run-root`; everything in a marked run root is Xeda's. A directory named `xeda_run` in the start directory is adopted without the marker. |
| `RunDirectoryError` | A run directory would lie outside the run root, or a write would go through a link Xeda does not control. Read the message. |
| `DeliveryError` | A setting or `--outputs-to` named a location Xeda cannot deliver to: the run wrote no such file; the destination is an input of the launch, a directory, or inside a run root; or two outputs name one destination (the message names both settings). Only a deliverable setting (`"writes": "deliverable"`) given a location is copied out; a working setting stays a bare name in the run directory. |
| `OutputExistsError` | The destination holds a file that is not Xeda's own unchanged earlier delivery. Rerun with `--overwrite-outputs`. At a terminal you can answer the prompt; under `--json` only the flag works. |
| `Invalid value for '--outputs-to': Directory '<x>' is a file.` | `--outputs-to` takes a directory. |

### Tools

- `ExecutableNotFound`: `Executable 'ghdl' was not found in PATH. flow:ghdl_sim, tool:GhdlTool`
  (`--debug` appends `, PATH:...`). Install the tool, or run it from a container with `-s dockerized=true`. `results.json`'s `tools`
  lists the executables Xeda found and their versions.
- `NonZeroExitCode`: `Command '...' exited with code 1!` in the failed node's `results.error`. If
  the error reaches the CLI itself, the message starts with `Flow <flow> failed: `. The generated
  script and the tool's log are in `run_path`.
- `ReportedFailure`: a node's `results.json` says so when its reports or checks failed with no
  exception and no tool exit status, for example a tool that exited 0 and wrote no netlist. The
  top-level document says `FlowFailed`; consumers quote the message
  (`dependency yosys_fpga failed: ...`).
- `MissingOutput`: a declared output is absent, unreadable, outside the run directory or not
  written by this run. `nextpnr` reports its own missing configuration as `FlowFatalError`, naming
  the setting and path.

## Simulation verdicts

A simulation passes only on recognized evidence that it ended, never on exit status alone.
`results.json` holds `sim.evidence` (the normalized record), `sim.ended_by`, `sim.time`,
`sim.time_unit`, `sim.errors` and `sim.warnings`. `error.message` states the cause and the fix.

- A silent exit 0, a missing or malformed end record, an unknown end, or a drained event queue
  fails. A testbench that ends with `wait;` needs `std.env.finish` or `$finish`, or a `stop_time`.
  Fix: end the testbench with `$finish` (VHDL `std.env.finish`).
- `$finish`, VHDL `std.env.finish` and `std.env.stop` are accepted. Verilog `$stop` is an
  error-rank event and fails at the default threshold. A nonzero exit always fails.
- cocotb: at least one test must run and none may fail; an all-skipped run fails. `cocotb.tests: 0`
  in `results.json` means no test was collected: check `tb.sources`.
- Every simulator has `timeout` (per subprocess invocation that contains simulation) and
  `fail_severity` (`warning`, `error`, `failure`, `fatal`; default `error`; `failure` and `fatal`
  rank alike).
- A requested `stop_time` needs a matching observed time. GHDL and nvc report the actual stop time;
  a sparse clock that misses it fails. Bluesim `max_cycles` needs the exact measured cycle count.
- `verilator` refuses `stop_time` (`verilator cannot stop at a simulated time`): end the
  testbench with `$finish`, or set `timeout`. A C++ source that holds `main` needs `main: false`;
  without it, a C++ source is a DPI helper. `yosys_sim` also refuses `stop_time`.
- `nvc` needs a C++ compiler for non-cocotb runs. Its `exit_severity` was removed: use
  `fail_severity`.
- ModelSim exits 0 whatever the testbench says; Xeda judges its owned batch log and
  `TESTSTATUS`. Vivado `xsim` needs source-preserving `elab_debug`; turning it off is refused.
- VCS and Questa adapters judge native evidence and fail closed on evidence they do not recognize.
  They are unverified on licensed tools. `bsc_sim` refuses `cvc`, `cver`, `isim`, `ncverilog` and `veriwell`
  before compilation.

### Waveform settings

`vcd`, `fst`, `vcdgz`, `saif` and the other waveform settings take `true` (the conventional file,
`dump.<format>`), `false`, a file name, or a location. `-s vcd=true` is `vcd: true`. `yes` and `on`
are refused.

| Message | Fix |
| --- | --- |
| `` `wave` writes GHW data, and its name x.vcd ends in .vcd: give the name to `vcd` `` | A name that ends in another format's extension goes to that format's setting. |
| `` `vcd` writes VCD data, and its name x.vcd.gz ends in .vcd.gz: give the name to `vcdgz` `` | `ghdl_sim` writes compressed VCD with `vcdgz`. `vcd` is plain VCD. |
| `ghdl_sim writes one waveform, and vcd, fst are set: keep one of them` | One format at a time on `ghdl_sim`, `nvc`, `vcs` and `verilator`. |
| `` `vcd` names a waveform file, but without cocotb xeda writes no waveform ... Set `vcd: true` `` | `verilator` without cocotb: `vcd`, `fst` or `saif` set to `true` builds the model with that trace format, and the testbench calls `$dumpfile("dump.<format>")` and `$dumpvars`. A run that wrote no such file fails. |
| `` vcs cannot write a waveform from `vcd`: ... set `vpd`, `fsdb` or `evcd` `` | `to_vcd` converts FSDB or VPD to VCD. |
| `` yosys_sim cannot write a waveform from `vcd` `` | The design's C++ driver writes the waveform. |

`nvc` has `vcd` and `fst` (`wave` and `wave_format` were removed). `ghdl_sim` has `vcd`, `vcdgz`,
`fst` and `wave` (GHW). `modelsim` writes no dump unless `vcd` is set. `vivado_sim`'s `saif` is
`outputs/<design>.saif`.

## Open-source FPGA flows

A Xilinx 7-series build fails before or during `nextpnr`:

- `nextpnr-himbaechel needs the full part in fpga.part`: give `fpga.part` with package, pins and speed grade (`xc7a100tcsg324-1`),
  as the Project X-Ray database lists it.
- A missing Himbaechel share tree, generator, `bbasm` or Project X-Ray database names the path
  searched: put openXC7 1.0's `bin` first on `PATH` (another `nextpnr-himbaechel` without the
  openXC7 share tree cannot serve), or set `chipdb` or `prjxray_db`.
- The first build seems to hang: it generates the chip database (about a minute, gigabytes of
  memory), once per run root.
- `nextpnr: timing constraints are not met: Max frequency ... (FAIL at ...)`: relax the clock, or set
  `timing_allow_fail`.
- `nextpnr constraint error` names the original constraint file and line.
- A `NonZeroExitCode` of `nextpnr` ends with `nextpnr error: ...`, the first error lines of this
  run's log; more are in `nextpnr.log`.
- `openfpgaloader`: the loader can exit 0 after a failure. Xeda reads `openfpgaloader.log` and
  fails with `ReportedFailure`, quoting the lines. An ID error means the bitstream is for another
  device than the one on the cable. `Can't program SPI flash: missing device-package information`
  means the loader's list gives the board no part: set `cable` as well. A Xilinx `write_flash`
  whose log lacks the finished write (`Writing: [...] 100.00%` then `Done`, or `Writing: Done`
  with quiet bars) did not finish it: a locked flash can stop the loader before it writes. `verify`
  does not report that.

A 7-series bitstream can compute the wrong result with timing met. Some openXC7 `nextpnr-himbaechel`
builds write wrong bits with no error: the 1.0 release and `1.0.0-41-g3e5c2cdd` have none of the
fixes. Known defects: an inferred `DSP48E1` multiply ignores its A operand; an initialized `RAM32M`
or `RAM64M` reads 0; a falling-edge shift register shifts on the rising edge; a block RAM with an
initial or reset value on its output register; an `ODDR` on the T input of a tri-state; an `MMCME2`
or `PLLE2`; a `TMDS_33` input, `LVCMOS33` `DRIVE 16` or an `IDDR`; a memory of 64K x 1 or deeper in
cascaded `RAMB36E1` pairs; a high-performance bank pad that an `ODDR` or an `OSERDESE2` drives. A
timing or utilization report cannot show them. Use a build of `main` at `26f5e17a5`
(`1.0.0-75-g26f5e17a`, from `nextpnr-himbaechel --version`) or later; Xeda does not check the build.
`nextpnr` also ignores `set_property PULLUP true` without a warning: write `set_property PULLTYPE
PULLUP`.

## Reading numbers

- `clock_frequency` is the frequency constrained. `Fmax` is the frequency achieved.
- `Fmax` is per tool: `nextpnr` and Vivado time the same netlist differently. Compare `Fmax`
  only between runs of one tool.
- Prefer the canonical keys `Fmax`, `lut` and `ff`; keys that start with `_` are internal.
- `xeda list-results <flow> --json` explains every key. When `documented` is `false`, the keys were
  detected from the flow's source: read an actual `results.json`. A flow can report nothing beyond
  the common keys.
- Flows that enforce timing fail on negative slack by default. Check `wns` and `whs`, and whether
  `fail_timing` (Vivado) or `timing_allow_fail` (`nextpnr`) was turned off.
- `lut` is per toolchain and stage; `LUT:STAGE` and `LUT:METHOD` say which. For 7-series, `nextpnr`
  counts distinct occupied LUT locations in its placement, and `yosys_fpga` estimates from mapped
  primitives (`LUT:LOGIC`, `LUT:RAM`, `LUT:SRL`). Neither is certified comparable with Vivado's
  count: compare one toolchain with itself. `fabric` names the die whose `available` totals are
  shown (an `xc7a35t` is routed as an `xc7a50t`).
- `nextpnr` parses timing and utilization from the JSON report named by its `report` setting. Its
  `lut`, `ff`, `bram`, `dsp` and `io` are given for ECP5 and Xilinx 7-series; other families get
  raw bel-type counts (`ICESTORM_LC`, `OXIDE_COMB`) plus timing.
- `nextpnr`'s `wns` is derived: the difference between the constrained and achieved clock periods.
  With several constrained clock domains, `Fmax` is the lowest achieved frequency and `wns` is the
  least-slack domain's (they may differ), and the scalar `clock_frequency` and `clock_period` are
  omitted. `clock_domains` gives the count; per-domain detail is in `_fmax`.

## Stale or surprising reuse

A run reuses `xeda_run/<design>/<flow>/` (`<design>/<target>/<flow>/` with a target), which keeps
incremental tool state. A flow that runs logs `Running <flow>: <reason>`; an up-to-date flow logs
that and shows its recorded results. If a result looks stale or wrong, run `xeda run ... --dry-run
--json` to see each node as `fresh`, `stale` (with the reason) or `unknown`, then use `--rebuild-all`
or `--clean` (see "Rebuilds" in `SKILL.md`).
