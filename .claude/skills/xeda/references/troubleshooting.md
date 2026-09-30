# Troubleshooting Xeda runs

## First move: read `settings.json`

Every run directory contains `settings.json`: `flow_settings`, the run's input (the flow's
defaults, the project's and design file's `[flows.<flow>]` sections and the command-line `-s`
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
`nthreads`/`ncpus`). Check nesting too - a dependency's settings live under a nested key, e.g.
`nextpnr.yosys.flatten`, not `flatten`.

### `FlowSettingsError` with a type error

The value does not match the setting's type. `xeda list-settings <flow> --json` gives each
field's `type` and, where the value is constrained, its `enum`. Text that spells a number is read
as one, so `-s clock.period=5.5` is fine but `-s clock.period=fast` is not. The reverse does not
happen: a text setting needs text in a design file (`compile_args = ["-j", "8"]`), not a number,
and `true`/`false` are not text. The declared `Code` fields used for FPGA speed grades and device
generations are the exception: `speed = -1` is accepted and stored as text. A list setting also
accepts comma-separated text (`-s xdc_files=a.xdc,b.xdc`).

### A simulation fails with "no evidence" or an all-skipped cocotb run

A simulation passes only when it shows how it ended. Read `results.json`: `error.message` and, for
Verilator, `sim.ended_by` and `sim.time`. A cocotb run needs at least one test that ran and none
that failed. Verilator fails at `fail_severity` (default `error`; `warning`, `error`, `failure` or
`fatal`) and stops at `timeout`. Simulators other than Verilator, cocotb and `bsc_sim` are not
converted to this rule yet.

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

The flow ran but reported failure, often because report parsing found a tool error or a timing
violation. The JSON document keeps the parsed `results`; inspect them and the reports under
`run_path`.

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
dependency's, or the design's own), a directory where a file was expected, or a path inside a run
root. The role -- *working* or *deliverable* -- belongs to the setting itself, not to the value you
gave it (`xeda list-settings <flow> --json` shows each setting's `"writes"`): a *working* setting
always stays a bare name inside the run directory, whatever it is given, and is never delivered
anywhere; only a *deliverable* setting given a location -- an absolute path, or one anchored with
`$PWD`/`$DESIGN_ROOT` -- is copied out.

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
`wns`/`whs` in `results.json` and whether `fail_timing` (Vivado) or `timing_allow_fail` (nextpnr,
OpenXC7) was turned off.

### Simulation passes but nothing ran

Check `results.json`'s cocotb counts: `cocotb.tests` of 0 means no test was collected. Verify the
testbench source is present in `[tb].sources` and that `tb.top` is set for non-cocotb testbenches.

## Reading numbers correctly

- **`clock_frequency` is not `Fmax`.** `clock_frequency` is the frequency that was *constrained*;
  `Fmax` is the maximum that was *achieved*.
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
  ECP5-specific; for another `fpga.family` you get the raw nextpnr bel-type counts
  (`ICESTORM_LC`, `OXIDE_COMB`, ...) instead, plus timing, which is family-independent.
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

By default a flow reuses one directory per design (`xeda_run/<design>/<flow>/`), which keeps
incremental tool state between runs - good while iterating, occasionally the cause of a confusing
result; `--clean` empties it first and reruns everything ("make clean, then make"). A dependency
gets its own directory, a sibling of the flow that launched it, never nested under it.
