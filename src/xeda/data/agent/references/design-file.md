# Xeda design file reference

A design description is one YAML, TOML or JSON file. YAML is the recommended format. It says
*what* the design is, not how to build it, apart from optional per-flow settings.

The authoritative machine-readable definition: `xeda design-schema`.

## YAML parsing

YAML follows the 1.2 core schema: `yes/no/on/off/y/n` are strings, `010` is decimal 10,
`0o17` is 15, `0x1F` is 31, and `1e3` is a float. Dates, sexagesimal values (`1:30`)
and underscore-separated numbers (`1_000`) are strings. `true`/`false` are booleans;
`null`/`~` are null. Quote HDL text, bit vectors, leading-zero text and source paths.

A boolean setting accepts only `true` and `false` (no `yes`, no `1`; also `-s key=true`); `debug: yes` is an
error saying "`yes` is text, not a boolean: write `true`".

All mapping keys must be strings. Duplicate keys fail naming the file, key and both lines.
Ordinary aliases and core explicit tags are accepted; merge keys, recursive aliases and
non-core tags are rejected. Use two-space indentation and keep each source with its metadata
in one list entry. TOML and JSON files remain supported.

## Top level

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | no | Unique design name. Defaults to the design file's stem and names the run directory. |
| `rtl` | yes* | The design. See below. |
| `tb` | no | The testbench. Required by simulation flows. |
| `description` | no | One-line description. |
| `authors` (`author`) | no | One `"Name <email>"` string or a list of them. |
| `language` (`hdl`) | no | Language standards. |
| `flows` | no | Per-flow settings. |
| `dependencies` | no | Other designs this one depends on: the path of a design file, or a Git repository written `git+<url>#<design file in the repository>`. A URL without `git+` is refused. |
| `license`, `version`, `url` | no | Metadata; no flow reads these. |
| `design_root` | no | Base for relative paths. Defaults to the design file's directory - almost always right. |

The loader also accepts `sources`, `top`, `clock`, `clocks`, `parameters`, `generics`, `defines` and
`generator` at the top level. It folds them into `rtl`, so an explicit `rtl` section is not
required when this flat form is used.

## `rtl` - the design

| Key | Required | Meaning |
| --- | --- | --- |
| `sources` | yes | Source files **in compilation order**. Relative to the design file's directory. |
| `top` | no | Top-level module/entity. Required by synthesis flows. |
| `parameters` / `generics` | no | Verilog parameters or VHDL generics for the top level. Use either interchangeable name; giving both is an error. |
| `defines` | no | Verilog preprocessor macros. |
| `clock` | no | Canonical single-clock description, e.g. `{port: clk}`. |
| `clock_port` | no | Compatibility shorthand for a single-clock design; prefer `clock`. |
| `clocks` | no | A list of clocks, for multi-clock designs. |
| `attributes` | no | HDL attributes, as `attribute -> (object -> value)`. |
| `generator` | no | Command or generator class producing the sources before the flow runs. Its direct executable and its declared `sources` (a directory counts as every file in it, outside the design root too) identify its inputs; it runs again when those or its produced sources change. `generated_sources` names the sources it writes when it writes only some, and `always_runs` says its inputs cannot be judged. A POSIX lease serializes loads for the same design-root directory. The record lives under `<run root>/.cache/generators/`, so a design loaded outside `xeda run` generates on every load. |

## `tb` - the testbench

The aliases `test` and `tests` are also accepted. The section has the same `sources`,
`parameters`/`generics` and `defines` as `rtl`, plus:

| Key | Meaning |
| --- | --- |
| `top` | Toplevel testbench module. Up to two, as a list, when a secondary toplevel is needed. Required when `tb.sources` holds a Verilog, SystemVerilog, VHDL, Bluespec or Chisel source and the testbench is not cocotb: a simulation flow refuses the design and names `tb.top`. Not needed with a C++ driver of the design's own (`verilator`, `yosys_sim`), nor by `ghdl_sim` for a VHDL testbench (it finds the top). |
| `uut` | Instance name of the unit under test inside the testbench. Some flows need it for waveform dumping or activity capture. |
| `cocotb` | `true`, or a table with `module`, `toplevel`, `testcase`. Detected automatically from a `.py` source. |

## Clocks

Clocks are split deliberately: `rtl` names the design's clock **ports**; the **period or
frequency** is a *flow setting*, because it constrains a particular build.

Single clock:

```yaml
rtl:
  clock: {port: clk}
flows:
  vivado_synth:
    clock:
      period: 5.0 # nanoseconds
```

or, equivalently, `clock.freq: "200MHz"` in the flow section. The legacy `clock_port` and
`clock_period` inputs are accepted for compatibility. Within one settings layer, use only one
spelling for a concept; across layers the higher-precedence spelling wins and is merged into the
canonical `clocks` mapping. Prefer `clock` in the design and `clock.period` or `clock.freq` in
flow settings.

Multiple clocks:

```yaml
rtl:
  clocks:
    - {port: clk, name: main_clock}
    - {port: clk_aux, name: aux}
flows:
  vivado_synth:
    clocks:
      main_clock: {period: 5.0}
      aux: {freq: 100MHz}
```

A `PhysicalClock` takes `period` (ns) or `freq` (MHz), and derives the other. A consistent pair is
accepted for compatibility; a contradictory pair is an error. Both accept unit strings
(`"5.5ns"`, `"200MHz"`, `"0.2GHz"`). Units are case-sensitive, as in SI: `"200mhz"` is an error
that names `MHz`. It also takes `rise`, `duty_cycle`, `uncertainty`, `skew` and `port`.

## Source files

A path string suffices when its extension identifies a type:

| Extension | Type |
| --- | --- |
| `.vhd`, `.vhdl` | `Vhdl` |
| `.v` / `.sv` | `Verilog` / `SystemVerilog` |
| `.vh` / `.svh` | `VerilogHeader` / `SVHeader` |
| `.bsv`, `.bs`, `.bh` | `Bluespec` |
| `.py` | `Cocotb` |
| `.cc`, `.cpp`, `.cxx` / `.c` / `.h`, `.hpp` / `.o`, `.a` | `Cpp` / `C` / `CHeader` / `ObjectFile` |
| `.sc` | `Chisel` |
| `.xdc` / `.sdc` / `.lpf` / `.pcf` / `.pdc` | `Xdc` / `Sdc` / `Lpf` / `Pcf` / `Pdc` |
| `.ucf` / `.xcf` / `.qsf` / `.ldc` / `.fdc` | `Ucf` / `Xcf` / `Qsf` / `Ldc` / `Fdc` |
| `.tcl` | `Tcl` |
| `.mem`, `.init`, `.hex` | `MemoryFile` |
| `.asc` / `.fasm` / `.bit`, `.sof` | `IceAsc` / `Fasm` / `Bitstream` |
| `.blif` / `.edf`, `.edif` | `Blif` / `Edif` |
| `.sdf` / `.spef` / `.saif` | `Sdf` / `Spef` / `Saif` |
| `.vcd` / `.fst` / `.ghw` / `.vpd` / `.fsdb` | `Vcd` / `Fst` / `Ghw` / `Vpd` / `Fsdb` |
| `.dcp` / `.lib` / `.def` / `.odb` / `.gds` / `.cdl` / `.vlt` | `Checkpoint` / `Liberty` / `Def` / `Odb` / `Gds` / `Cdl` / `Vlt` |

Every source has a type. Suffixes match as written (`TOP.VHD` is an error naming `.vhd`).
`.json`, `.bin`, `.cfg`, `.config` and any suffix not in the table need `type: "..."`;
`JsonNetlist`, `EcpConfig`, `VerilogNetlist`, `VhdlNetlist`, `Chipdb` and `Data` are only given
that way. `Data` has no automatic HDL frontend; a flow or the design can still read it.
An invalid explicit `type` is an error naming the closest types. Explicit type names are
case-tolerant; suffixes are not. Give a gate-level `.v` netlist `type: VerilogNetlist`;
otherwise its inferred type is `Verilog`.

A source of a later stage's type stands in for the flows that would build it: a `JsonNetlist`
skips synthesis for `nextpnr`, an `Edif` netlist skips it for `vivado_impl`, a `Fasm`, `EcpConfig`
or `IceAsc` configuration is packed by `fpga_pack` without placing, and a `Bitstream`
(`{file: build/top.bit, type: Bitstream}`, or just the `.bit` file) is what `openfpgaloader`
programs, with nothing built.

Every flow that reads the design's sources declares the types it reads (`reads_sources`): other
non-language types are skipped (an `Lpf` in a Vivado design), but an unsupported language fails
before the flow runs (a Bluespec source for `vivado_synth`, VHDL for `verilator`, SystemVerilog
for `ghdl_sim`/`nvc`), and so does a design with no source the flow reads at all
(a design whose only netlist is an `.edf`, for `vivado_synth`, `yosys_fpga` or the `yosys_fpga`
that `nextpnr` plans): the error lists the design's sources with their types and the types the
flow reads; for the `yosys_fpga` that `nextpnr` plans, and only when nothing else in the plan needs
it, the error adds that a `JsonNetlist` source would replace it. One source of a type the flow reads
is enough, a constraint file included, so an `.edf` with an `.xdc` still plans for `vivado_synth`.
Flows that read the testbench (simulators, `bsc`, `vivado_project`) also check `tb.sources`.
C/C++ files are never refused. `nextpnr`, `openroad`, `fpga_pack`, `openfpgaloader`, `vivado_impl`
and `vivado_power` read no design source directly, only their declared inputs (a producer's output
or a typed source, such as `Sdc` or `Xdc` files). Headers reach include/search paths; no source
type name becomes a tool command.

The `bsc` flow compiles BH (Bluespec Classic) only from `.bs` files; it rejects a `.bh` source
with a settings error naming the file to rename.

A source containing `*` is a pattern (`"src/*.vhd"`); its matches are inserted in sorted order,
and a pattern matching no file is an error. `*` is the only pattern character: `?`, `[` and `]` are
part of a file name, so `"rtl/fifo[1].v"` names exactly that file (never `fifo1.v`), and
`"rtl/fifo[1]_*.v"` matches `fifo[1]_a.v`. Never escape them. Xeda passes such names to yosys and
to TCL-scripted tools intact, but Vivado's `add_files` (every file in `vivado_project`; memory
files, `xdc_files` and `tcl_files` in `vivado_synth`) refuses a name
containing `[`, `]` or `$`: rename those for Vivado.

When inference is not enough, use a table:

```yaml
rtl:
  sources:
    - pkg.vhdl
    - {file: legacy.v, type: SystemVerilog}
    - {file: old.vhdl, standard: '93'}
    - {path: generated/top.v} # not checked for existence
```

`file` must exist and is checked at load time. `path` is not checked - use it for sources a
generator will produce. Every source carries a content hash, which is how Xeda tells runs apart
and reuses cached dependency runs.

## `[language]` - standards

```yaml
language:
  vhdl:
    standard: '2008' # '93', '2008', '2019', ...
  verilog:
    standard: '2005'
```

`version` is an accepted alias of `standard`. Two-digit (`08`) and four-digit (`2008`) both work.

## `flows.<flow_name>` - per-flow settings

Applied only when that flow runs, so one file can carry constraints for several targets:

```yaml
flows:
  vivado_synth:
    fpga.part: xc7a100tftg256-2L
    clock.period: 5.0
  openroad:
    platform: sky130hd
    clock.period: 10.0
  ghdl_sim:
    stop_time: 100us
```

`xeda list-settings <flow> --json` lists what a flow accepts. Unknown keys are rejected.

A section may also hold `inputs`: where a flow that declares file inputs reads each from, as
`<producer flow>` or `<producer flow>.<output>` (a list for an input that takes several). It is
saved wiring for the resolver, not a setting -- absent from `xeda list-settings` and from
`settings.json` -- and it names a flow's output, never a file; give an external file as a typed
source. A saved binding is chosen before a typed source and before the input's default producer:

```yaml
# bound_demo.yaml
name: bound_demo
rtl:
  top: top
  sources:
    - top.v
    - file: top.json       # a prebuilt netlist ...
      type: JsonNetlist
flows:
  nextpnr:
    fpga:
      part: LFE5U-85F-6BG381C
    inputs:
      netlist: yosys_fpga.netlist   # ... that this binding chooses not to use
```

A project file's section means the same, below the design's. A chain on the command line
(`xeda run yosys_fpga+nextpnr design.yaml`) replaces a saved binding of the same input; see
`SKILL.md`.

## `targets` - one design, several boards

Each entry of `targets` is an overlay on the design: it takes the design file's own keys, merged
over them. `--target NAME` (on `xeda run` and `xeda dse`) selects one.

```yaml
name: knight
rtl:
  top: mkKnight
  sources: [Knight.bsv, por_sync.v]
  clock: {port: CLK}
targets:
  arty:
    sources: [arty.xdc]                 # appended after the design's sources
    defines: {CLK_HZ: 100000000}        # mappings merge key by key
    flows:
      nextpnr: {board: arty_a7_100t}
  ulx3s:
    sources: [ulx3s.lpf]
    defines: {CLK_HZ: 25000000}
    flows:
      nextpnr: {board: ulx3s_85f}
```

- A target takes `rtl`, `tb`, `flows` and every other design key, flat forms included (`sources`,
  `defines`, `top`, `clock`, ...); not `name` or `targets`. Unknown keys are errors.
- Mappings merge at every depth; `sources` are appended after the design's; any other list
  replaces the design's. Paths resolve against the design root.
- A short form means its table where the design and a target meet (`clock: CLK` is `clock:
  {port: CLK}`, `parameters` as a list of `{name, value}`, `vhdl: "08"` is `{version: "08"}`,
  `cocotb: true` is `{}`, `fpga: <part>` is `{part: <part>}`): a target's `clock`/`clock_port`
  refines the design's first clock, its `clocks` replaces them all, `clock: null` or `{}` means none.
  A value that is no table where one is expected (`tb: 3`) is an error whichever target is selected.
  `--design-overrides` meet the design by the same rules, but only a target appends `sources`:
  an override's `rtl.sources`/`tb.sources` replace the whole list (a selected target's too).
- One target: no `--target` needed. Several: `--target` is required and the error lists them.
  `--target` on a design without `targets` is an error. API: `Design.from_file(path, target=...)`.
- **A target overrides the design**, key by key: where both write a key, the target's value
  wins (a target's `flows.nextpnr.board` replaces the design's, with no error); keys the target
  does not write stay the design's. Order, lowest first: flow defaults, project file, design
  file, target, `-s`, API for design settings. The loader records the selected target name;
  a design file or override cannot set or change it. This is not the agreement rule between two flows of one run (which
  errors when `yosys_fpga` and `nextpnr` disagree on a shared setting).
- A target name is a name (`[A-Za-z][A-Za-z0-9_-]*`) and not a flow's name or alias. A design
  file has no `target` key; the loader records it and overrides cannot change it.
- The selected target yields an ordinary design, identical to the file written flat. `--json`
  documents report `target` (`null` without one).
- A selected target has run directories of its own, `<run root>/<design>/<target>/<flow>`
  (`<flow>_<hash>` with `--hashed-run-dirs`), also when it is the design's only one and selected
  by default; without targets a design keeps `<design>/<flow>`. Two targets that yield the same
  design build separately and each stays up to date. A run made before targets (`<design>/<flow>`)
  is left alone by a target's launch. `xeda scrub FLOW DESIGN --target NAME` removes only that
  target's runs; without `--target` it removes every target's and the older ones too.
- Limit: `board`/`fpga`/`custom_boards_file`/clock constraints go under the target's
  `flows.<flow>` (at its top level the three shared leaves are refused, naming that).

## Environment variables in paths

Path-typed settings expand `$PWD`, `$DESIGN_ROOT` and `$DESIGN_DIR`:

```yaml
flows:
  dc:
    target_libraries: [$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db]
```

Design sources expand environment variables too, except `$PWD`; `$DESIGN_ROOT`/`$DESIGN_DIR` are
the design root, so `"$DESIGN_ROOT/src/*.vhd"` and `"src/*.vhd"` are the same sources.

## Multiple designs

A `xedaproject.yaml` holds several designs plus top-level `flows` settings merged into each.
`xedaproject.yml` and `xedaproject.toml` are also accepted. Automatic discovery requires exactly
one of these names in a directory; multiple matches are an error. Select one design with
`--design-name`.
