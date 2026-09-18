# Troubleshooting Xeda runs

## First move: read `settings.json`

Every run directory contains `settings.json` - the **effective** settings after the flow's
defaults, the design file's `[flows.<flow>]` section and the command-line `-s` overrides have all
been merged, plus the design as Xeda resolved it.

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
as one, so `-s clock_period=5.5` is fine but `-s clock_period=fast` is not. The reverse does not
happen: a text setting needs text in a design file (`speed = "2"`, `compile_args = ["-j", "8"]`),
not a number, and `true`/`false` are not text. A list setting also accepts comma-separated text
(`-s xdc_files=a.xdc,b.xdc`).

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

### `DesignValidationError`

The design file is invalid. Validate it against `xeda design-schema`. Common causes: a source file
that does not exist (relative paths resolve against the *design file's* directory, not the working
directory), an unknown field, or a missing `sources` list.

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

| Situation | Option |
| --- | --- |
| Stale tool state is suspected | `--clean` |
| Want each run isolated | `--no-incremental` |
| Want dependencies reused across runs | `--cached-dependencies` |
| Want previous runs of this flow removed first | `--scrub` |

By default a flow reuses one directory per design (`xeda_run/<design>/<flow>/`), which keeps
incremental tool state between runs - good while iterating, occasionally the cause of a confusing
result.
