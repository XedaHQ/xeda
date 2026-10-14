# Design file reference

A design file is YAML (preferred), TOML or JSON. It says what the design is; per-flow settings are
optional. The schema is `xeda design-schema`; loading the file is the authoritative check, because
the schema leaves property sets open (dotted keys expand before validation).

## YAML rules

- YAML 1.2 core schema: `yes`, `no`, `on`, `off`, `y`, `n` are strings; `010` is decimal 10;
  `0o17` is 15, `0x1F` is 31, `1e3` is a float. Dates, `1:30` and `1_000` are strings.
  `true`/`false` are booleans and `null`/`~` are null. Quote HDL text, bit vectors, leading-zero
  text and source paths.
- A boolean setting accepts only `true` and `false` (also `-s key=true`). `debug: yes` fails with
  "`yes` is text, not a boolean: write `true`".
- Mapping keys are strings. A duplicate key fails and names the file, the key and both lines.
  Merge keys (`<<`), recursive aliases and non-core tags are refused; ordinary aliases work.
- Two-space indentation. Keep a source and its metadata in one list entry.

## Dotted keys

`a.b: v` is the table `a: {b: v}` in a design file, a project file, a target and `-s`.

| Write | As |
| --- | --- |
| A table of at most two settings | dotted keys: `clock.port: clk`, `fpga.part: xc7a100tftg256-2L` |
| A file section: the file's root, `rtl`, `tb`, `flows`, each `flows.<flow>`, `targets`, each target, each entry of `designs` | a block |
| `inputs`, and tables keyed by tool names (`set_synth_properties`, `set_impl_properties`, ISE `*_options`, `set_attribute`, `set_mod_attribute`, `rtl.attributes`) | a nested block |
| A source with a type | one line: `- {file: "top.json", type: JsonNetlist}` |

Give one table one spelling per mapping: `clock.port` and a `clock:` block together are a
`ConflictingKeys` error that names both, in either order. So is a key that is a value and a table
(`timing: true` with `timing.x: 1`). Two dotted keys under one table (`fpga.part`, `fpga.speed`)
are fine. Origins merge by precedence and are not one mapping (`-s fpga.part=P` over a design's
`fpga:` block). Do not mix the flat form (`sources`, `top`, `clock` at the root) with
`rtl.*` keys; write `rtl:`.

## Top level

| Key | Meaning |
| --- | --- |
| `name` | Design name. Defaults to the file's stem; names the run directory. |
| `rtl` | The design (required unless the flat form is used). |
| `tb` | The testbench. Simulation flows need it. |
| `description`, `authors` (`author`), `license`, `version`, `url` | Metadata; no flow reads them. `authors` is a `"Name <email>"` string or a list. |
| `language` (`hdl`) | Language standards. |
| `flows` | Per-flow settings. |
| `targets` | One overlay per board. |
| `dependencies` | Other designs: a design file path, or `git+<url>#<design file in the repository>`. A URL without `git+` is refused. |
| `design_root` | Base for relative paths. Defaults to the design file's directory. |

The flat form (`sources`, `top`, `clock`, `clocks`, `parameters`, `generics`, `defines`,
`generator` at the root) is folded into `rtl`.

## `rtl`: the design

| Key | Meaning |
| --- | --- |
| `sources` | Required. Sources in compilation order, relative to the design file's directory. |
| `top` | Top module or entity. Synthesis flows need it. |
| `parameters` (`generics`) | Verilog parameters or VHDL generics of the top module. Use one spelling. |
| `defines` | Verilog preprocessor macros. |
| `clock` | One clock: `clock.port: clk`, or `clock: clk`. |
| `clocks` | A list of clocks for several clocks. |
| `attributes` | HDL attributes, `attribute -> (object -> value)`. |
| `generator` | A command or class that writes the sources before the flow runs. |

A generator runs again when its executable, its `sources` (a directory counts as every file in
it) or its produced sources change. `generated_sources` names the files it writes when it writes
only some; `always_runs` says its inputs cannot be judged. The record lives under
`<run root>/.cache/generators/`, so a design loaded outside `xeda run` generates on every load.

## `tb`: the testbench

`test` and `tests` are aliases. `tb` has `sources`, `parameters` (`generics`) and `defines` like
`rtl`, and:

| Key | Meaning |
| --- | --- |
| `top` | Testbench top module: one string, or a list of at most two (a primary and a secondary top module). Required when `tb.sources` holds Verilog, SystemVerilog, VHDL, Bluespec or Chisel and the testbench is not cocotb; the simulation flow refuses the design and names `tb.top`. Not needed for a C++ driver of the design's own (`yosys_sim`; `verilator` with `main: false`) or for `ghdl_sim` with a VHDL testbench. |
| `uut` | The instance name of the unit under test in the testbench; some flows need it for waveforms or activity capture. |
| `cocotb` | `true` for the defaults, or a table with `module`, `toplevel`, `testcase`; `false` means no cocotb testbench. A source of type `Cocotb` (a `.py` file) sets it. |

## Clocks

`rtl` names the clock ports. The flow settings give the period or frequency, because they
constrain one build. The settings `clock_port` (design) and `clock_period` (flow) were removed:
use `clock: <port>` and `clock.period`.

```yaml
rtl:
  clock.port: clk
flows:
  vivado_synth:
    clock.period: 5 ns # clock.freq: 200 MHz is the same clock
```

Several clocks:

```yaml
rtl:
  clocks:
    - port: clk
      name: main_clock
    - port: clk_aux
      name: aux
flows:
  vivado_synth:
    clocks.main_clock.period: 5 ns
    clocks.aux.freq: 100 MHz
```

Within one settings layer give `clock` or `clocks`, not both. Across layers the higher layer's
`clock` merges into the canonical `clocks`. Clock names are keys, so they contain no dot.

A clock takes `period` (a time) or `freq` (a frequency) and derives the other; a consistent pair is
accepted, a contradictory pair is an error. Every time and frequency needs a unit (`5.5 ns`,
`200 MHz`, `0.2 GHz`); a bare number is refused. Units are case-sensitive: `200mhz` is an error
that names `MHz`. A clock also takes `rise`, `uncertainty` and `skew` (times), `duty_cycle` (a
fraction) and `port`.

## Units

A setting whose `type` in `xeda list-settings <flow> --json` is a kind (`time`, `frequency`,
`capacitance`, `length`, `voltage`, `resistance`) holds a quantity: one number, an optional space
and one unit, and nothing else (`5 ns * 2` is an error). A bare number is an error. Units are
case-sensitive, and a spelled-out name (`nanoseconds`) is refused.

| Kind | Units | `settings.json` writes |
| --- | --- | --- |
| time | `fs`, `ps`, `ns`, `us`, `µs` (micro sign, U+00B5), `ms`, `s`, `min`, `h` | `ns` |
| frequency | `Hz`, `kHz`, `MHz`, `GHz` | `MHz` |
| capacitance | `fF`, `pF`, `nF` | `fF` |
| length | `nm`, `um`, `mm`, `µm` (micro sign, U+00B5) | `um` |
| voltage | `mV`, `V` | `V` |
| resistance | `ohm`, `kohm`, `Mohm` | `ohm` |

- Write `clock.period: 2.5 ns` in YAML, `"2.5 ns"` as a string in TOML and JSON, and
  `-s clock.period=2.5ns` on the command line (no space, so no quotes; with a space, quote the
  item).
- `10 ns`, `10000 ps` and `0.01 us` are one value and one run identity. `settings.json` writes
  `10 ns` for all three.
- Other numbers stay plain: counts, ratios and fractions, percentages (the name ends in
  `_percent`), sizes of data (the description names the unit) and values "in the units of the
  library" (passed to the tool unconverted).
- These keep their current form and are not quantities: `stop_time` (ns, or text such as `100us`),
  `timeout` (s), `prerun_time` (text such as `10ns`), `time_unit`, `time_resolution` and
  `lic_wait` of `vcs`, the Vivado `default_{max,min}_{input,output}_delay` (ns), `hold_detour_max`
  of `nextpnr` (ns), `freq` of `openfpgaloader` (Hz), and the numbers of `dc` and `xeda dse`. A
  unit on one that takes a plain number fails with a type error ("unable to parse string as a
  number" or "... as an integer").

## Source files

A suffix gives the type when it is in this table:

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
| `.lib` / `.def` / `.gds` / `.cdl` / `.vlt` | `Liberty` / `Def` / `Gds` / `Cdl` / `Vlt` |

Suffixes match as written (`TOP.VHD` is an error naming `.vhd`). `.json`, `.bin`, `.cfg`,
`.config`, `.dcp`, `.odb` and any suffix not in the table need `type`. These types are given only
that way: `JsonNetlist`, `EcpConfig`, `VerilogNetlist`, `VhdlNetlist`, `FpgaNetlist`,
`FpgaTimingNetlist`, `SynthCheckpoint`, `RoutedCheckpoint`, `OpenroadDbPlaced`, `OpenroadDbCts`,
`OpenroadDbGlobalRouted`, `OpenroadDbRouted`, `OpenroadSdcPlaced`, `OpenroadSdcCts`,
`OpenroadSdcGlobalRouted`, `OpenroadSdcRouted`, `Chipdb`, `Data`. `.odb` is one of the four
`OpenroadDb` types, by stage. `Data` has no
automatic HDL frontend; a flow or the design can still read it. An invalid `type` is an error
that names the closest types; type names are case-tolerant, suffixes are not.

A source table has `file` (must exist at load time) or `path` (not checked: for a file a
generator writes), and `type`. Any other key is an error: a source has no `standard` or
`variant`, and `language.vhdl.standard` sets the standard of the whole design.

```yaml
rtl:
  sources:
    - "pkg.vhdl"
    - {file: "legacy.v", type: SystemVerilog}
    - {path: "generated/top.v"} # not checked for existence
```

A string containing `*` is a pattern (`"src/*.vhd"`): its matches are inserted in sorted order, and
a pattern that matches no file is an error. `*` is the only pattern character; `?`, `[` and `]`
are part of a file name (`"rtl/fifo[1].v"` is exactly that file). Never escape them. Vivado's
`add_files` refuses a name with `[`, `]` or `$` (every file in `vivado_project`; memory files,
`xdc_files` and `tcl_files` in `vivado_synth`): rename those.

Netlists and checkpoints are typed by stage:

| Type | Is |
| --- | --- |
| `VerilogNetlist` | a standard-cell gate-level netlist. Give a gate-level `.v` this type, or it is `Verilog`. |
| `FpgaNetlist` | a Verilog netlist of FPGA primitives as Vivado writes it; `vivado_postsynth_sim` simulates it |
| `FpgaTimingNetlist` | its timing netlist, simulated with an SDF; Verilog has no suffix for it, so give `type` |
| `SynthCheckpoint`, `RoutedCheckpoint` | a Vivado checkpoint after synthesis, after routing (`vivado_power` takes only the routed one) |
| `Checkpoint` | no stage; no flow takes it |

A source of the wrong one of these types is not taken in its place: a `JsonNetlist` source does
not replace the `Edif` netlist that `vivado_impl` reads.

A source of a later stage replaces the flows that would build it: a `JsonNetlist` skips synthesis
for `nextpnr`, an `Edif` netlist skips it for `vivado_impl`, a `Fasm`, `EcpConfig` or `IceAsc`
configuration is packed by `fpga_pack` without placing, and a `Bitstream` is what
`openfpgaloader` programs.

Each flow declares the source types it reads. It skips other non-language types (an `Lpf` in a
Vivado design). At planning it refuses a source in a language it cannot read (Bluespec for
`vivado_synth`, VHDL for `verilator`, SystemVerilog for `ghdl_sim`) and a design with no source
it reads; the error lists the sources with their types and the types the flow reads. One source of
a type the flow reads is enough, a constraint file included. Simulators, `bsc` and
`vivado_project` check `tb.sources` too. C and C++ files are never refused. `nextpnr`,
`fpga_pack`, `openfpgaloader`, `vivado_impl` and `vivado_power` read only their declared inputs.
`bsc` compiles Bluespec Classic only from `.bs` files and refuses a `.bh` source.

## `language`: standards

```yaml
language.vhdl.standard: 2008 # 93, 2008, 2019, ...
language.verilog.standard: 2005
```

`version` is an alias of `standard`. Write the four-digit year; a two-digit year needs quotes
(`"08"`), because an unquoted `08` is the number 8. The standard applies to every source of that
language (GHDL and NVC compile all VHDL of a run under one standard).

## `flows.<flow>`: per-flow settings

A section applies only when that flow is in the run.

```yaml
flows:
  vivado_synth:
    fpga.part: xc7a100tftg256-2L
    clock.period: 5 ns
  yosys:
    platform: nangate45
    clock.period: 10 ns
  ghdl_sim:
    stop_time: 100us
```

`xeda list-settings <flow> --json` lists the settings. An unknown key is an error. A section
holds `inputs` as well: where a flow with declared file inputs reads each from, as
`<producer>` or `<producer>.<output>` (a list for an input that takes several). `inputs` is
saved wiring, not a setting (`list-settings` and `settings.json` omit it). It names a flow's
output, never a file; give a file as a typed source. A saved binding beats a typed source, which
beats the input's default producer:

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
(`xeda run yosys_fpga+nextpnr design.yaml`) replaces a saved binding of the same input.

## `targets`: one design, several boards

A target is an overlay on the design. It takes the design's own keys (`rtl`, `tb`, `flows`,
the flat form), not `name`, `targets`, `target` or `design_root`. `--target NAME` on `xeda run` and `xeda dse` selects it.

```yaml
name: knight
rtl:
  top: mkKnight
  sources: ["Knight.bsv", "por_sync.v"]
  clock.port: CLK
targets:
  arty:
    rtl:
      sources: ["arty.xdc"] # appended after the design's sources
      defines.CLK_HZ: 100000000 # mappings merge key by key
    flows:
      nextpnr:
        board: arty_a7_100t
  ulx3s:
    rtl:
      sources: ["ulx3s.lpf"]
      defines.CLK_HZ: 25000000
    flows:
      nextpnr:
        board: ulx3s_85f
```

- One target needs no `--target`; several need it, and the error lists them. `--target` on a
  design without targets is an error. API: `Design.from_file(path, target=...)`.
- Where design and target both write a key, the target wins with no error (its
  `flows.nextpnr.board` replaces the design's); other keys stay. Mappings merge at every depth,
  `sources` append, any other list replaces. A null or empty `flows` table or section adds
  nothing. Layers, lowest first: flow defaults, project, design, target, `-s`, API.
- A short form means its table in every merge (`clock: CLK` is `clock.port: CLK`; `fpga: <part>`
  is `fpga.part: <part>`). A target's `clock` refines the design's first clock, its `clocks`
  replaces all, `clock: null` means none. A value that is no table where one is expected
  (`tb: 3`) is an error.
- `--design-overrides KEY=VALUE...` merge by the same rules, except that only a target appends
  `sources`: an override's `rtl.sources` or `tb.sources` replaces the whole list.
- A target name matches `[A-Za-z][A-Za-z0-9_-]*` and is not a flow's name, alias or hashed run
  directory name; two names that differ only in case are refused. A design file has no `target`
  key. `--json` documents report `target`.
- A selected target has its own run directories, `<run root>/<design>/<target>/<flow>`; runs made
  without targets are left alone. `xeda scrub FLOW DESIGN --target NAME` removes only that
  target's runs; without `--target` it removes every target's and the older ones.
- `board`, `fpga` and `custom_boards_file` go under the target's `flows.<flow>`; at the target's
  top level they are refused, naming that.

## Paths and environment variables

Path-typed settings expand `$PWD`, `$DESIGN_ROOT` and `$DESIGN_DIR`; any other `$VAR` comes from
the environment.

```yaml
flows:
  dc:
    target_libraries: ["$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db"]
```

Design sources expand environment variables too, except `$PWD`; `$DESIGN_ROOT` and `$DESIGN_DIR`
are the design root, so `"$DESIGN_ROOT/src/*.vhd"` and `"src/*.vhd"` are the same sources.

## Project file

A project file holds several designs and `flows` settings merged into each:

```yaml
designs:
  - name: sqrt
    language.vhdl.standard: 2008
    rtl:
      sources: ["sqrt.vhdl"]
      top: sqrt
      clock.port: clk
  - name: fifo
    rtl:
      sources: ["fifo.v"]
      top: fifo
flows:
  vivado_synth:
    fpga.part: xc7a100tftg256-2L
    clock.period: 5 ns
```

- Top-level keys: `designs` (`design`) and `flows` (`flow`). Any other key fails with
  `ProjectFileError: ... unknown key(s) ...; a project has 'designs', 'design', 'flows', 'flow'`.
- Each design entry and each flow section follows the rules of a design file, dotted keys
  included. A dotted key cannot reach into `designs` from the project's root.
- Xeda finds the file as `--xedaproject FILE`, else the one `xedaproject.yaml`, `.yml` or `.toml`
  in the start directory. Two of these names in one directory is a `ProjectFileError`.
- With more than one design, name one: `xeda run vivado_synth --design-name sqrt`. Without a
  name, the error is `DesignNotSpecified`.
