# Troubleshooting Xeda runs

## First move: read `settings.json`

Every run directory contains `settings.json`: `flow_settings`, the run's input (the flow's
defaults, the project's and design file's `flows.<flow>` sections and the command-line `-s`
overrides, merged key by key), and `effective_flow_settings`, what the flow made of it -- plus the
design as Xeda resolved it.

When a run did something unexpected, this is the difference between what you think you asked for
and what was actually asked of the tool. `xeda run --json` reports its path.

## Failure modes

### `FlowSettingsError: Extra inputs are not permitted: <name>`

The setting does not exist on that flow. Settings are validated strictly - a typo fails rather
than being ignored, on purpose.

```bash
xeda list-settings <flow> --json | jq -r '.fields[].name'
```

Check for an `alias`: some settings have two accepted names (`vcd`/`waveform`,
`nthreads`/`ncpus`). Check the flow too - a dependency's settings belong to its own flow, e.g.
`-s flows.yosys_fpga.flatten=true`, not `flatten`.

### `FlowSettingsError` with a type error

The value does not match the setting's type. `xeda list-settings <flow> --json` gives each
field's `type` and, where the value is constrained, its `enum`. Text that spells a number is read
as one, so `-s clock.period=5.5` is fine but `-s clock.period=fast` is not. The reverse does not
happen: a text setting needs text in a design file (`compile_args: ["-j", "8"]`), not a number,
and `true`/`false` are not text. The declared `Code` fields used for FPGA speed grades and device
generations are the exception: `speed = -1` is accepted and stored as text. A list setting also
accepts comma-separated text (`-s xdc_files=a.xdc,b.xdc`).

### A simulation fails with "no evidence" or an all-skipped cocotb run

A simulation passes only on recognized evidence that it ended, not on exit status alone. Read
`results.json`: `sim.evidence` is the normalized record; `sim.ended_by`, `sim.time`,
`sim.time_unit`, `sim.errors` and `sim.warnings` summarize it. A silent exit 0, missing or
malformed record, unknown end, or event queue that drained without a finish fails. `$finish`
(including VHDL `std.env.finish` and `std.env.stop`) is accepted; Verilog `$stop` is an error-rank
event and fails at the default threshold.
A requested `stop_time` needs a matching observed time. Bluesim `max_cycles` needs the exact
measured cycle count and final time. A supported user-owned C++ driver may pass on observed exit
0. Nonzero execution always fails, even after finish.

Every simulator flow uses shared `timeout` and `fail_severity` settings. The default severity is
`error`; choices are `warning`, `error`, `failure` and `fatal` (`failure` and `fatal` have the
same rank). Timeout bounds each subprocess invocation containing simulation, including analysis
and elaboration when combined; it is not a dependency-wide deadline. For GHDL and nvc, sparse
clock stops that miss the requested time fail at their actual reported time. NVC builds a
passive VHPI monitor and needs a C++ compiler for non-cocotb runs; its removed `exit_severity`
setting must be replaced with `fail_severity`. CXXRTL accepts an observed exit 0 from its linked
user-owned driver even if simulated time is unknown; it rejects `stop_time` because that driver
controls scheduling.

ModelSim batch evidence requires a matching runtime checkpoint, native stop reason/time,
TESTSTATUS and the bounded runtime section of its owned logfile. VHDL `std.env.stop` is accepted
with source-qualified native evidence; Verilog `$stop` fails. ModelSim-Intel Starter 2020.1 and
Vivado 2024.2 are the real releases verified for these contracts. Vivado xsim requires
source-preserving `elab_debug`; setting it to `off` is rejected so VHDL `std.env.stop` can be
distinguished from `$stop`.

VCS and Questa adapters are documentation-only and have not been verified against licensed real
tools; unknown native evidence fails closed. VCS quiet `$finish(0)` and VHDL completion fail
unless a native finish diagnostic is visible. UCLI time checkpoints alone do not prove HDL
completion. `bsc_sim` rejects legacy `cvc`, `cver`, `isim`, `ncverilog` and `veriwell` before
compilation. Real Icarus evidence and builtin-task capability checks remain mandatory Linux CI
gates; macOS skips them.

### `-s` took the design file, or a setting names the wrong flow

`-s` ends at the next option or the first token that is not KEY=VALUE; put the design first, or end
the options with `--`. To set a dependency's setting, use `-s flows.<flow>.key=value`; an unknown
flow or a misdirected key is an error that suggests the right flow.

### `FlowNotFoundError`

Unknown flow name. The message suggests close matches. Names are forgiving about separators and
case (`vivado_synth`, `vivado-synth`, `VivadoSynth` all work), so this usually means a genuinely
wrong name. `xeda list-flows --json` is the list, and `aliases` are equally valid names.

### `ExecutableNotFound`

The tool is not on `PATH`. Either install it, or run the flow's tools from a container:

```bash
xeda run <flow> <design> -s dockerized=true
```

`results.json`'s `tools` array records which executables Xeda found and their versions.

### `NonZeroExitCode`

The EDA tool itself failed. The generated script and the tool's log are in the run directory
(`run_path` in the `--json` output). Re-run with `--debug` for verbose logging and a traceback.

### `FlowFailed`

The verdict at the top of the `--json` document: the requested flow itself ran to its end without
raising and did not succeed (often report parsing found a tool error or a timing violation). A flow
that raises is named by its exception class instead, and a failed producer is
`FlowDependencyFailure`. The document keeps the parsed `results`; inspect them and the reports under
`run_path`.

### `ReportedFailure`

The cause, not the verdict: a node's `results.json` says `error.type = "ReportedFailure"` when its
own reports or checks failed without an exception or a tool exit status (a tool that exited 0 and
wrote no netlist, for instance). A declared output that xeda finds missing after the flow's own
checks passed is `MissingOutput` instead. The names differ on purpose: `FlowFailed` is the top of the
document, what happened to the request; `ReportedFailure` is inside a node, why it failed. Its
consumers quote the message ("dependency yosys_fpga failed: ..."), and the
top-level `--json` document of the request carries `FlowDependencyFailure` (a producer failed) or
`FlowFailed` (the requested flow itself). Read the log and the reports in the failing node's
run directory: the `nodes` of the document list every run directory, and a flow that was planned
but never entered shows `"state": "not run"`.

### A chain is refused

`xeda run a+b` fails with exit status 2 before anything runs, and says why:

- `no compatible output for a required input` or `takes no required input`: the pair does not
  fit. `bsc`, `bsc_sim`, `vivado_project` and `vivado_sim` declare no inputs and no outputs, so
  they cannot be part of a chain yet; run each alone. When another output of the producer, or the flows between the pair
  on the required default routes, make a pair fit, the message ends with `Did you mean ...?`, a whole valid chain (`nextpnr+openfpgaloader` ->
  `nextpnr+fpga_pack+openfpgaloader`). Otherwise there is no suggestion; `xeda list-flows --json`
  has `can_follow`/`can_precede` for finding a flow that fits.
- `programs a device and can only end a chain`: `openfpgaloader` must be last.
- `appears more than once`: a chain is a path; each flow once.
- `--remote` and `xeda dse` refuse chains and bindings; run locally.

A binding that fails is a settings error naming the consumer, the input and where it was written
(`unknown input`, `unknown producer`, `declares no inputs`), and a chain and an explicit
command-line or API binding of the same input is an error even when they are equal: bind it in
one place. A saved binding is a reference to a flow's output (`yosys_fpga.netlist`), never a file
path; give a file as a typed source.

### "`<flow>` needs `fpga`" although the design gives a part

A flow's settings are its own `flows.<flow>` section, and a shared leaf such as `fpga` travels
only along the edges of the run's own graph. A chain replaces default producers: in
`vivado_synth+openfpgaloader` the bitstream comes from `vivado_synth`, so `fpga_pack`, `nextpnr`
and `yosys_fpga` are not part of the run, and a part written only under `flows.yosys_fpga`
reaches neither node. Planning logs "yosys_fpga is not part of this run: its settings are
unused", and the error names the section that does not count: the `fpga` in `flows.yosys_fpga`
of the design file does not reach vivado_synth, since yosys_fpga is not part of this run (the
programmer needs a device only to program the flash). Write the part for a flow of the run
(`flows.vivado_synth.fpga.part`, or a `board` for `openfpgaloader`; it reaches the other node
along their `fpga` edge), or give `-s fpga.part=<part>`. Only `nextpnr`, `fpga_pack` and
`openfpgaloader` take a `board`; `yosys_fpga` and the Vivado flows take `fpga.part`.

### `NoSuccessfulRun`

A design-space exploration completed without a successful candidate. Inspect the attempted run
directories, then correct the flow settings or adjust the search range.

### `RunRootError`

The run root (`--run-root`/`XEDA_RUN_ROOT`, default `./xeda_run`) holds files but carries no marker
xeda created (`.xeda-run-root`), or it lies somewhere xeda cannot write. The message names the
directory. Move it aside, or run `touch <dir>/.xeda-run-root` to hand it to xeda -- but only if you
are sure nothing of yours is in there: everything under a marked run root is xeda's from then on.
The one exception is by name and location, not history: a directory named `xeda_run` directly in
the start directory is adopted automatically, whatever put files there -- xeda does not check who
created it. Keep nothing of your own in a directory called `xeda_run` beside where you run xeda
from.

### `DeliveryError`

A setting or `--outputs-to` named a location xeda could not deliver the output to: the run did not
write the file the setting expects, or the destination is an input the launch reads (a
dependency's, or the design's own), a directory where a file was expected, a path inside a run
root, or a destination that two outputs of the launch name, or one inside another (the message
names both settings: give each its own path). The role -- *working* or *deliverable* -- belongs to the setting itself, not to
the value you gave it (`xeda list-settings <flow> --json` shows each setting's `"writes"`): a
*working* setting always stays a bare name inside the run directory, whatever it is given, and is
never delivered anywhere; only a *deliverable* setting given a location -- a path under `$PWD`,
`$DESIGN_ROOT` or `$DESIGN_DIR`, or an absolute path -- is copied out.

### `OutputExistsError`

A subclass of `DeliveryError`: the destination already holds a file, and it is not xeda's own
earlier, unchanged delivery there, so replacing it needs your say-so. Rerun with
`--overwrite-outputs`, or, at an interactive terminal, answer the prompt; under `--json` or with no
terminal, only the flag works.

### `DesignValidationError`

The design file is invalid. `xeda design-schema` describes its structure, but deliberately leaves
property sets open because the loader accepts dotted-key shorthands before validation; loading the
file is the authoritative unknown-field check. Common causes are a source file that does not exist
(relative paths resolve against the *design file's* directory, not the working directory), an
unknown field, or a missing `sources` list.

### The flow "succeeds" but timing is not met

Flows that enforce timing fail on negative slack by default. If timing results look wrong, check
`wns`/`whs` in `results.json` and whether `fail_timing` (Vivado) or `timing_allow_fail` (nextpnr)
was turned off.

### Simulation passes but nothing ran

Check `results.json`'s cocotb counts: `cocotb.tests` of 0 means no test was collected. Verify the
testbench source is present in `tb.sources` and that `tb.top` is set for non-cocotb testbenches.

### `open_xc7` was removed

"`open_xc7` was removed: use fpga_pack to build, openfpgaloader to program" -- for the flow's
name and for a `flows.open_xc7` section in a design or project file alike. Move its placement
settings to `flows.nextpnr`, synthesis settings to `flows.yosys_fpga`, and pin files into
`rtl.sources`; `xeda scrub open_xc7 <design>` still removes its old run directories.

### A 7-series build fails before or during nextpnr

- "needs the full part": give `fpga.part` with package, pins and speed grade
  (`xc7a100tcsg324-1`), as the Project X-Ray database lists it.
- A missing Himbaechel share tree, generator, `bbasm` or Project X-Ray database names the path
  searched: put openXC7 1.0's `bin` first on `PATH` (another `nextpnr-himbaechel` on `PATH`, without
  the openXC7 share tree, cannot serve), or set `chipdb`/`prjxray_db`.
- The first build seems to hang: it is generating the chip database (about a minute, gigabytes
  of memory), once per run root.
- "timing constraints are not met: Max frequency ... (FAIL at ...)": relax the clock, or set
  `timing_allow_fail` to keep the result.
- "nextpnr constraint error" names the original constraint file and line.
- A `NonZeroExitCode` of nextpnr ends with "nextpnr error: ...", the first error lines this run's
  log holds (an unplaceable cell, for example); more are in `nextpnr.log`.

### A 7-series bitstream builds but computes the wrong result

Some openXC7 `nextpnr-himbaechel` builds write wrong bits with no error and with timing met. The
openXC7 1.0 release and `1.0.0-41-g3e5c2cdd` have none of the fixes. At least these defects are
known (openXC7/nextpnr pull request in brackets): an inferred multiplier (`DSP48E1`) that ignores
its A operand (66, issue 39); an initialized `RAM32M`/`RAM64M`, whose LUTs read 0 (70); a
falling-edge shift register that shifts on the rising edge (70); a block RAM with an initial or
reset value on its output register (69); an `ODDR` on the T input of a tri-state (72); an `MMCME2`
or `PLLE2` (68); a `TMDS_33` input, `LVCMOS33` `DRIVE 16`, an `IDDR` (71); a memory of 64K x 1 or
deeper in cascaded `RAMB36E1` pairs (67); a high-performance bank pad that an `ODDR` or an
`OSERDESE2` drives (78). A timing or utilization report cannot show them; only a test of the
bitstream itself can. Use a build of `main` at `26f5e17a5` (`1.0.0-75-g26f5e17a`, from
`nextpnr-himbaechel --version`) or later: that commit has all of them. Xeda does not check the
build. The authors compared bitstreams with Vivado's and tested none
on a board.

Also, nextpnr ignores `set_property PULLUP true` without a warning. Write `set_property PULLTYPE
PULLUP` for a pull-up. The bundled pin files use neither.

## Reading numbers correctly

- **`clock_frequency` is not `Fmax`.** `clock_frequency` is the frequency that was *constrained*;
  `Fmax` is the maximum that was *achieved*.
- **`Fmax` is per tool.** nextpnr's timing model is not Vivado's. On one design with a block RAM
  and a DSP they reported 429 to 453 MHz and 232 to 241 MHz for the same two netlists; on a counter
  Vivado's timing of nextpnr's own routing differed from nextpnr's report by 0.14 ns. Compare
  `Fmax` only between runs of the same tool.
- Canonical keys `Fmax`, `lut` and `ff` are added by the runner alongside whatever the flow
  reported (`f_max`, `maximum_frequency`, `LUT`, `FF`), so prefer the canonical ones.
- Keys beginning with `_` are internal and may change without notice.
- `xeda list-results <flow> --json` explains every key. When its `documented` field is `false`, the
  keys listed were detected from the flow's source as a best effort - read an actual `results.json`
  instead.
- Some flows report nothing beyond the common keys - `xeda list-results <flow>` says so
  explicitly for those.
- `nextpnr` reports timing and utilization parsed from the JSON report named by its `report`
  setting (kept as the `report` artifact). Its canonical `lut`/`ff`/`bram`/`dsp`/`io` keys are
  given for ECP5 and Xilinx 7-series; for another `fpga.family` you get the raw nextpnr bel-type
  counts (`ICESTORM_LC`, `OXIDE_COMB`, ...) instead, plus timing, which is family-independent.
- **`lut` is per toolchain and per stage**, and `LUT:STAGE`/`LUT:METHOD` say which. For 7-series,
  `nextpnr` counts distinct occupied LUT locations in its placement (not `SLICE_LUTX`, which
  counts both halves of a fractured LUT), and `yosys_fpga` estimates from mapped primitives
  (`LUT:LOGIC`, `LUT:RAM`, `LUT:SRL`). Neither is certified comparable with Vivado's count:
  compare one toolchain with itself. `fabric` names the die whose `available` totals are shown
  (an `xc7a35t` is routed as an `xc7a50t`); `clock_port` appears only when the reported clock
  domain is itself a top-level port.
- `nextpnr`'s `wns` is derived: nextpnr reports achieved *frequencies*, not slack, so slack is the
  difference between the constrained and achieved clock periods. With several constrained clock
  domains, `Fmax` is the lowest achieved frequency across them while `wns` comes from the
  least-slack domain -- these may be different domains -- and the scalar `clock_frequency` and
  `clock_period` keys are omitted as ambiguous. With a single constrained domain both *are*
  reported, taken from that domain's constraint (not its achieved frequency). `clock_domains`
  gives the count either way, and per-domain detail is always in `_fmax`.

## Re-running cleanly

`xeda run` is make-like **by default**: a flow only re-runs when something it consumed or produced
changed since its last successful run. A flow that runs logs why (`Running <flow>: <reason>`);
one left alone logs that it is up to date and shows its recorded results. `--cached-dependencies`
and `--incremental`/`--no-incremental` no longer exist - they fail naming their replacement below.

| Situation | Option |
| --- | --- |
| Stale tool state is suspected, or you want every flow to re-run from nothing | `--clean` |
| Force every flow to run even though nothing changed | `--rebuild-all` |
| Keep each settings variant of a flow in its own directory | `--hashed-run-dirs` |
| Want previous runs of this flow removed first | `--scrub` |

By default a flow reuses one directory per design (`xeda_run/<design>/<flow>/`; per target,
`xeda_run/<design>/<target>/<flow>/`), which keeps
incremental tool state between runs - good while iterating, occasionally the cause of a confusing
result; `--clean` empties it first and reruns everything ("make clean, then make"). A dependency
gets its own directory, a sibling of the flow that launched it, never nested under it.
