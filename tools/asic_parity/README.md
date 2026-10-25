# ASIC parity with OpenROAD-flow-scripts

`tools/asic_parity.py` checks that xeda's ASIC scripts behave like the scripts of
OpenROAD-flow-scripts (ORFS). For one ORFS design, it runs ORFS and xeda with equivalent settings,
then compares the results in a Markdown table.

The tool is a faithfulness and regression check. It is not a tuning tool. It does not choose
xeda's defaults, and it does not search for settings that give a better result.

## What it needs

- An ORFS checkout. Give it with `--orfs` or `$ORFS_DIR`. The tool only reads the checkout. All
  output goes under `--root`.
- OpenROAD. Give it with `--openroad` or `$OPENROAD_EXE`, or put `openroad` on `PATH`.
- A yosys with Tcl, for ORFS. Give it with `--yosys` or `$YOSYS_EXE`, or put `yosys` on `PATH`.
  The yosys from Homebrew has Tcl.
- A yosys for xeda. Give it with `--xeda-yosys`, or put `yosys` on `PATH`. xeda writes a Tcl
  script when the settings need one, so the designs with `SWAP_ARITH_OPERATORS = 1` need Tcl in
  this yosys too.
- A yosys with `read_slang`, for the canonical RTLIL of a design with the slang front end. Give it
  with `--yosys-frontend`. The default is the yosys for xeda. The `ibex` designs use the slang
  front end, so the yosys for xeda must have `read_slang` for them.
- `make`, from `$MAKE` or from `PATH`.
- A virtual environment with xeda installed. The tool starts `python -m xeda` with the Python that
  runs the tool.

## Commands

Run the tool from the root of the xeda checkout. The global options go before the command:

```console
.venv/bin/python tools/asic_parity.py --orfs /path/to/OpenROAD-flow-scripts --root runs run nangate45/gcd --half synthesis
.venv/bin/python tools/asic_parity.py --root runs table
.venv/bin/python tools/asic_parity.py --orfs /path/to/OpenROAD-flow-scripts --root runs script nangate45/gcd
```

Global options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--orfs DIR` | `$ORFS_DIR` | The ORFS checkout. Read only. |
| `--root DIR` | `asic_parity_runs` | The output directory. A relative path is relative to the working directory. |
| `--openroad EXE` | `$OPENROAD_EXE`, else `openroad` | OpenROAD. |
| `--yosys EXE` | `$YOSYS_EXE`, else `yosys` | A yosys with Tcl, for ORFS. |
| `--xeda-yosys EXE` | `yosys` on `PATH` | The yosys for the xeda flows. |
| `--yosys-frontend EXE` | the yosys for xeda | A yosys with `read_slang`. |
| `--cores N` | `4` | The `NUM_CORES` of ORFS. |
| `--timeout S` | `3300` | The seconds that one `make` or xeda call may run. |

Commands:

- `run <platform>/<design> --half {synthesis,pnr,both}` runs ORFS when `--root` has no ORFS row,
  then runs each named half, then prints the table. Give `--half` more than once for several
  halves. A half whose saved row is current is skipped. A row is current when it passed and the
  inputs that it records are unchanged: the tools, the source files, the ORFS variables and the
  core count, and a digest of this tool. A half row also records the platform and override files,
  the design file and a digest of the code of xeda, and the `pnr` row depends on the ORFS row. The
  ORFS row records none of the inputs that only xeda reads, so a change of xeda does not run ORFS
  again. A failed or stale row runs again, and the tool prints why. With `--force`, every row of
  the command runs again, the ORFS row too.
- `table [<platform>/<design> ...]` prints the table of the recorded rows. With no design, it prints
  every design under `--root`. It needs no ORFS checkout and no tool.
- `script <platform>/<design>` runs the xeda `yosys` flow of the design. It prints the yosys script
  that xeda rendered. The run directory is `<root>/<platform>/<design>/script/`.

The exit status of `run` is 0 when every stage of every requested row ran to the end and every
row meets the bar. It is 1 when a stage failed, or when a row that ran to the end misses the bar.
The output names each row that misses the bar and the conditions that it misses
(`<platform>/<design> <half>: misses the bar: ...`); the `bar` column of the table shows the same
verdict.
The status 2 means a problem that the message names, such as a design file that disagrees with
ORFS. The status 3 means that xeda lacks flows that a half needs.

## The halves

The reference row is ORFS's own flow. It runs `make do-yosys-canonicalize do-yosys` (for a slang
design, the tool writes the canonical RTLIL with the front-end yosys, then runs `make do-yosys`).
Then it runs the place and route targets `do-1_synth`, `do-floorplan`, `do-place`, `do-cts`,
`do-route` and `do-finish`.

Each half changes one part of the flow to xeda:

- `synthesis`: xeda's `yosys` flow synthesizes the design sources. ORFS places and routes the
  netlist that xeda writes. The row shows what the xeda synthesis script costs or gains.
- `pnr`: ORFS's netlist (`1_2_yosys.v` of the reference row) is the input. xeda's `openroad` flow
  places and routes it, and its default producers run before it: `openroad_place`, `openroad_cts`
  and `openroad_grt`. The row shows what the xeda place and route flows cost or gain.
- `both`: xeda's whole chain runs from the design sources: `yosys`, `openroad_place`,
  `openroad_cts`, `openroad_grt` and `openroad`.

In `pnr` and `both`, the constraint file of ORFS (`SDC_FILE`) is a source of the design. The
OpenROAD flows read it as it is, so xeda uses the clock, the input and output delays, the clock
latencies and the virtual clock of ORFS. The tool gives no flow a `clock`: a second clock on the
same port would replace the clock of the constraint file and drop its latency. The `yosys` flow of
ORFS uses no clock period either, so its xeda counterpart needs none.

The tool switches off the three verdicts of the last flow (`fail_timing`, `fail_drc` and
`fail_antenna`). A run that misses timing then still gives its numbers, and the `bar` column says
which condition it misses. The tool judges the numbers itself.

The results of the last xeda flow become the ORFS metric names of the table:

| xeda result | ORFS metric |
| --- | --- |
| `worst_slack` | `finish__timing__setup__ws` |
| `tns` | `finish__timing__setup__tns` |
| `whs` | `finish__timing__hold__ws` |
| `hold_tns` | `finish__timing__hold__tns` |
| `area` | `finish__design__instance__area__stdcell` |
| `power` | `finish__power__total` |
| `drc_errors` | `detailedroute__route__drc_errors` |
| `antenna_violating_nets` | `detailedroute__antenna__violating__nets` |

The results `fmax` (MHz in xeda, Hz in ORFS), `setup_violations`, `instances` and `core_area` map
to optional metrics. A required result that is missing stops the row with an error that names it.
The area and the cell count of the `both` row come from the `yosys` flow of the chain.

The halves `pnr` and `both` need the OpenROAD flows of xeda. Before any ORFS run starts, the tool
checks the flows of the half. When one is missing, for example in an older xeda, it stops with
status 3 and a message that names the missing flows. The half `synthesis` needs only `yosys`.

## The bar

A row meets the bar when all of these hold, compared with the ORFS reference row of the same design:

- The setup WNS (`finish__timing__setup__ws`) is not more than 15 ps below the ORFS WNS. A better
  WNS also meets the bar.
- The final standard-cell area (`finish__design__instance__area__stdcell`) is not more than 2%
  above the ORFS area. A smaller area also meets the bar.
- No DRC error (`detailedroute__route__drc_errors` is 0).
- No antenna violation (`detailedroute__antenna__violating__nets` is 0).
- The hold WNS (`finish__timing__hold__ws`) is positive.

The table has one row per recorded row. Its columns are the setup and hold WNS and TNS, the final
area, the power, the DRC and antenna counts, the synthesis area and cell count, the yosys area, the
run times and the `bar` verdict. The verdict names each condition that a row misses.

## Design files

Each design has a file in `tools/asic_parity/designs/<platform>/<design>.yaml`. It holds the xeda
settings that match the `config.mk` of the ORFS design. The shipped files cover `nangate45` (`aes`,
`gcd`, `ibex`, `jpeg`) and `sky130hd` (`aes`, `gcd`, `ibex`).

```yaml
orfs: nangate45/gcd
clock:
  port: clk
  period: 0.25 ns  # constraint.sdc: set clk_period 0.25
flows:
  yosys:
    abc.strategy: area  # ABC_AREA = 1
    arithmetic.adder_map: none  # ADDER_MAP_FILE :=  (empty: "Adders degrade GCD")
  openroad_place:
    floorplan.core_utilization_percent: 55  # CORE_UTILIZATION = 55
    global_placement.density.mode: auto  # PLACE_DENSITY_LB_ADDON = 0.20
```

The keys are:

- `orfs`: the ORFS design, as `<platform>/<design>`. It names the directory under `flow/designs`.
- `clock`: `port` is the clock port, and `period` is the clock period with a unit, such as
  `0.25 ns`. The period must equal `set clk_period` in the SDC file of ORFS. The port must equal
  `set clk_port_name` when the SDC file sets it. xeda gets the clock from the SDC file, so the tool
  only compares these two values with it.
- `hierarchical`: `true` when ORFS sets `OPENROAD_HIERARCHICAL = 1`. The tool gives it to the four
  OpenROAD flows, which share it. It is `false` when the key is absent.
- `flows`: the xeda settings, by flow name: `yosys`, `openroad_place`, `openroad_cts`,
  `openroad_grt` and `openroad`. Dotted keys are allowed. Each setting has a comment that names the
  ORFS variable that it mirrors.

The tool refuses a design file that disagrees with ORFS. It reads the ORFS variables with
`make print-<VARIABLE>`, and it checks the settings that each flow ends up with, the defaults of a
flow that the file leaves out included:

- `yosys`: `abc.strategy`, `abc.mapper`, `arithmetic.adder_map`, `arithmetic.adder_architecture`,
  `arithmetic.wrap_operators`, `hierarchy.keep_min_cost` and `systemverilog`,
  from `ABC_AREA`, `SWAP_ARITH_OPERATORS`, `ADDER_MAP_FILE`, `SYNTH_OPERATIONS_ARGS`,
  `SYNTH_HIERARCHICAL` and `SYNTH_HDL_FRONTEND`.
- `openroad_place`: the floorplan (`CORE_UTILIZATION`, `CORE_ASPECT_RATIO`, `CORE_MARGIN`,
  `FLOORPLAN_DEF`), the placement density (`PLACE_DENSITY_LB_ADDON`), the pin distance
  (`PLACE_PINS_ARGS`), the repair (`TNS_END_PERCENT`, `ENABLE_PLACE_REPAIR_TIMING`,
  `REMOVE_ABC_BUFFERS`), and the swap of the arithmetic modules (`SWAP_ARITH_OPERATORS`).
- `openroad_cts`: the clusters of the sinks (`CTS_CLUSTER_SIZE`, `CTS_CLUSTER_DIAMETER`) and the
  repair (`TNS_END_PERCENT`, `SKIP_CTS_REPAIR_TIMING`).
- `openroad_grt`: the repair after the route (`SKIP_INCREMENTAL_REPAIR`, `TNS_END_PERCENT`).

Two Tcl files that a design names itself, `FASTROUTE_TCL` and `PDN_TCL`, have no variable form.
The tool reads them and compares them with the settings: the layer adjustment with
`routing.layer_adjustments`, and the rails, the straps and the pins of the power grid with
`pdn.grid.rails`, `pdn.grid.straps` and `pdn.block_pins`. A line that the tool cannot translate
stops it. A file of the platform needs no setting: the platform of xeda holds its values.

A `config.mk` that sets a variable which the tool neither reads nor lists in `IGNORED_VARIABLES`
stops the tool. A new variable in ORFS must be mapped to a xeda setting, or listed with the reason
why the result does not depend on it. `SYNTH_REPEATABLE_BUILD` is such a variable.

A design file must not set `platform`, `platform_file`, `pdk_provider`, `pdk_version`, `pdk_root`,
`clock`, `clocks`, `hierarchical`, `floorplan.def_file`, `fail_timing`, `fail_drc` or
`fail_antenna` in a flow. The tool sets them. With a floorplan from a DEF file
(`floorplan.sizing: def`), the tool takes the file from `FLOORPLAN_DEF`.

## Override files

Each platform has an override file in `tools/asic_parity/overrides/<platform>.yaml`. It is a user
platform file (`platform_file`) that extends the bundled platform (`extends: <platform>`). It makes
xeda read the platform directory `flow/platforms/<platform>` of the ORFS checkout, with the
`directory` provider and layout `orfs`. The `pdk_root` of the run is `<checkout>/flow/platforms`,
and `pdk_version` is the ORFS commit. The comparison then measures the scripts, not the library
revision or the values of the platform.

The two files differ:

- `nangate45.yaml` changes only the provider. The bundled `nangate45` platform already holds the
  values of ORFS for this PDK.
- `sky130hd.yaml` also takes the upstream configuration, the file names and the values from ORFS.
  It reads `config.mk` of the platform, and it sets the values that ORFS takes from `fastroute.tcl`,
  `tapcell.tcl`, `setRC.tcl` and `pdn.tcl`. Its inherited corners `ss_100C_1v60` and
  `ff_n40C_1v95` have no files in this layout. A run resolves only `tt_025C_1v80`.

These files are not xeda's shipped platforms. The shipped `sky130hd` takes its values from the
upstream configuration of open_pdks, the PDK's own source. ORFS calibrates its values for its own
flow, so they form a different baseline. The override makes xeda use the ORFS values. The
comparison then measures the scripts, not the choice of values.

## Result rows

Each row is a JSON file. The paths are:

- `<root>/<platform>/<design>/orfs/result.json`: the ORFS reference row.
- `<root>/<platform>/<design>/<half>/result.json`: the row of a half.

A row holds:

- `success`, `failed_at` and `error`: whether every stage ran to the end, and where it stopped.
- `tools`: the versions and the paths of OpenROAD, the ORFS yosys and the xeda yosys, the front-end
  yosys when it differs, the SHA-256 digest of the OpenROAD executable, and the xeda version. The
  ORFS row leaves out the xeda yosys and the xeda version.
- `orfs_commit`, and `pdk` with `provider` (`directory`) and `pin` (the ORFS commit).
- `platform_residue_sha256`, `override_sha256` and `design_file_sha256`: the SHA-256 digests of the
  bundled platform file, the override file and the design file (`null` in the ORFS row).
- `xeda_sha256`: the digest of the xeda package (`null` in the ORFS row).
- `tool_sha256`: the digest of `tools/asic_parity.py`, which drives both runs.
- `sources`: the SHA-256 digests of the source files and of the SDC file, by path in the checkout.
- `make_vars`: the ORFS variables that the tool read.
- `metrics`: the ORFS metric names, such as `finish__timing__setup__ws`.
- `times`, `yosys_area`, `yosys_cells` and `machine`.
- `xeda` (for a half): the runtime, the tool versions that xeda reports, the flow, settings and
  design hashes, and the run path.

The working files of each run are in the same directory: the logs of each `make` call, the generated
`design.yaml`, the `xeda.json` document and the `xeda_run/` run directory.

## Tests

The tests of the tool need no ORFS checkout and no tool:

```console
.venv/bin/pytest tests/test_asic_parity.py tests/test_parity_override_sky130hd.py -q
```

The opt-in layer `XEDA_TESTS_ASIC_QOR=1` runs the real comparison for the `synthesis`, `pnr` and
`both` halves of `nangate45` (`gcd`, `aes`, `ibex`, `jpeg`) and `sky130hd` (`gcd`, `ibex`). It
asserts that each row meets the bar. It needs `XEDA_TESTS_ORFS=<ORFS checkout>`, an `openroad`, and
a yosys with Tcl and `read_slang` on `PATH`. The checkout holds the platform files, so the layer
needs no PDK store. One run of ORFS serves all three halves of a design.

Run the layer without `-n`. Each xdist worker has its own run root, so a design would run in ORFS
once per worker, and the OpenROAD jobs would compete for the cores. A case that misses the bar
today is an expected failure with the measured numbers in its reason.
