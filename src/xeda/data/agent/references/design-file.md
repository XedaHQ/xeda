# Design file reference

A design file is YAML (preferred), TOML or JSON. It says what the design is; per-flow settings are
optional. The schema is `xeda design-schema`; loading the file is the authoritative check, because
the schema leaves property sets open (dotted keys expand before validation).

## YAML rules

- YAML 1.2 core schema: `yes`, `no`, `on`, `off`, `y`, `n` are strings; `010` is decimal 10;
  `0o17` is 15, `0x1F` is 31, `1e3` is a float. Dates, `1:30` and `1_000` are strings.
  `true`/`false` are booleans and `null`/`~` are null. Quote HDL text, bit vectors, leading-zero
  text and source paths.
- A boolean setting accepts only `true` and `false` (also `-s <flow>.key=true`). `docker: yes` fails with
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

Give one table one spelling per mapping: `fpga.part` and an `fpga:` block together are a
`ConflictingKeys` error that names both, in either order. So is a key that is a value and a table
(`timing: true` with `timing.x: 1`). Two dotted keys under one table (`fpga.part`, `fpga.speed`)
are fine. Origins merge by precedence and are not one mapping (`-s nextpnr.fpga.part=P` over a design's
`fpga:` block). The keys of `rtl` (`sources`, `top`, `clocks`, `parameters`, `defines`,
`generator`) go in `rtl:`, never at the root.

## Top level

| Key | Meaning |
| --- | --- |
| `name` | Design name. Defaults to the file's stem; names the run directory. |
| `rtl` | The design. |
| `tb` | The testbench. Simulation flows need it. |
| `description`, `authors`, `license`, `version`, `url` | Metadata; no flow reads them. `authors` is a `"Name <email>"` string or a list. |
| `language` | Language standards. |
| `flows` | Per-flow settings. |
| `targets` | One overlay per board. |
| `dependencies` | Other designs: a design file path, or `git+<url>#<design file in the repository>`. A URL without `git+` is refused. Planning reads them; see below. |

Relative paths resolve against the design file's directory (the design root). `design_root` is refused in a design file and in a design entry of a project file (`removed_key`); a design with another root is a `file` entry of the project. In Python: `Design.load(path, target=None)` or `Design(name=..., root=..., rtl={...})`.
A design is immutable: `d.with_updates({"tb.parameters.W": 8})` and `d.with_target("t")` return new
designs; `d.to_dict()` is the effective design in the shape of a file.

## `rtl`: the design

| Key | Meaning |
| --- | --- |
| `sources` | Required. Sources in compilation order, relative to the design file's directory. |
| `top` | Top module or entity. Synthesis flows need it. |
| `parameters` | Verilog parameters or VHDL generics of the top module. |
| `defines` | Verilog preprocessor macros. |
| `clock` | One clock: `clock.port: clk`, or `clock: clk`. |
| `clocks` | A list of clocks for several clocks. |
| `attributes` | HDL attributes, `attribute -> (object -> value)`. |
| `generator` | What writes the sources, in the launch before the first flow: a shell command line (`generator: "python soc.py"`, kept as `{shell: ...}`), a list of arguments (`[python3, soc.py]`, kept as `{executable: python3, args: [soc.py]}`), or a table with one of `shell`, `command`, `executable` (two of them, or `shell` with `args`, is an error) and `sources`, `generated_sources`, `always_runs`, `outputs_to`. |

A generator runs in the launch, never when a design loads, into a storage
`<run root>/.generated/<design>/<key>/` (with the record `generation.yaml`); the design tree is never
written. The key hashes the recipe (its `env` too), the design configuration without content, the
names and digests of the generator's `sources` and its executable. Every spelling (shell, list, table,
`class: chisel`) starts in the design root, or in `cwd` (a directory below the design root; an
absolute one or one that leaves it is refused at load); a relative executable (`./mill`) and a
relative `PATH` entry resolve against that same directory. `DESIGN_ROOT`, `XEDA_GENERATED` (the
storage) and `PWD` (its directory) are in its environment; the words after the executable expand both
(`args: ["gen.py", "$XEDA_GENERATED/out"]`; `shell: ./mill core.runMain top.Main "$XEDA_GENERATED"`).
Generated sources must be written under `$XEDA_GENERATED`: generated names are looked up only in
the storage, and a file written in the working directory is not found. Declare narrow `sources`: a
tool that writes inside a directory named there changes its own inputs, and the launch gives up
after a few attempts. A `shell` recipe's key counts `/bin/sh`, so name `./mill` and the build files
in `sources`. Two variants of a design share the design tree, so a tool's in-tree intermediate files
(mill `out/`) are not separate per variant. It runs again when its executable, its `sources` (a directory counts as
every file in it), a generated file or the digest of its inherited environment changes
(`always_runs`, or no `sources`: every launch; `--rebuild-all` forces it). `generated_sources`
names the entries of `rtl.sources` it writes when it writes only some (an entry is wholly generated
or wholly written; a pattern matching both is refused); empty, every entry is generated. A generated
entry is a name or pattern in its output, typed by its extension or by `type:`; one that matches no
file fails the node that reads it (`missing_generated`). A failing generator is the launch error
`generator_failed` (every node skipped). A plan runs nothing: a node that reads generated sources is
`unknown` ("the generator has not run") until the storage is current. `--remote` sends the recipe and
its declared `sources`; a generator without `sources`, or with an input outside its design root, or with `outputs_to`, is refused. `outputs_to` (a location: absolute, or below `$DESIGN_ROOT`, `$DESIGN_DIR` or `$PWD`; a relative path is refused) names a
directory that gets a copy of each generated file under its name in the output (`gen/top.v` -> `<outputs_to>/gen/top.v`). It is a
delivery like a deliverable setting given a location (at the end of a passed launch, key `generator`; a changed copy needs
`--overwrite-outputs`; never onto an input, over a directory, through a link, or where an entry of `rtl.sources`, `tb.sources` or
the generator's `sources` would read it), and no part of any identity. A dependency with a generator is
generated too, first, into its own storage; the same names may not be declared twice. A generator may write Bluespec:
`bsc` converts the generated files like hand-written ones. Each flow after `bsc` runs again only if
one of its own inputs changed, such as the Verilog that `bsc` writes or a source that `bsc` passes
through.

## `tb`: the testbench

`tb` has `sources`, `parameters` and `defines` like `rtl`, and:

| Key | Meaning |
| --- | --- |
| `top` | Testbench top module: one string, or a list of at most two (a primary and a secondary top module). Required when `tb.sources` holds Verilog, SystemVerilog, VHDL, Bluespec or Chisel and the testbench is not cocotb; the simulation flow refuses the design and names `tb.top`. Not needed for a C++ driver of the design's own (`yosys_sim`; `verilator` with `main: false`) or for `ghdl_sim` with a VHDL testbench. |
| `uut` | The instance name of the unit under test in the testbench; some flows need it for waveforms or activity capture. |
| `cocotb` | `true` for the defaults, or a table with `module`, `toplevel`, `testcase`; `false` means no cocotb testbench. A source of type `Cocotb` (a `.py` file) sets it. |

## Clocks

`rtl` names the clock ports. The flow settings give the period or frequency, because they
constrain one build. Xeda refuses the settings `clock_port` (design) and `clock_period` (flow):
use `clock: <port>` in the design and `clock.period` in a flow.

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
    clock.period: 5 ns
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
`capacitance`, `length`, `voltage`, `resistance`, `power`) holds a quantity: one number, an optional space
and one unit, and nothing else (`5 ns * 2` is an error). A bare number is an error. Units are
case-sensitive, and a spelled-out name (`nanoseconds`) is refused.

| Kind | Units | Unit of its kind |
| --- | --- | --- |
| time | `fs`, `ps`, `ns`, `us`, `µs` (micro sign, U+00B5), `ms`, `s`, `min`, `h` | `ns` |
| frequency | `Hz`, `kHz`, `MHz`, `GHz` | `MHz` |
| capacitance | `fF`, `pF`, `nF` | `fF` |
| length | `nm`, `um`, `mm`, `µm` (micro sign, U+00B5) | `um` |
| voltage | `mV`, `V` | `V` |
| resistance | `ohm`, `kohm`, `Mohm` | `ohm` |
| power | `nW`, `uW`, `µW` (micro sign, U+00B5), `mW`, `W` | `W` |

- Write `clock.period: 2.5 ns` in YAML, `"2.5 ns"` as a string in TOML and JSON, and
  `-s vivado_synth.clock.period=2.5ns` on the command line (no space, so no quotes; with a space, quote the
  item).
- `10 ns`, `10000 ps` and `0.01 us` are one value and one run identity. Xeda writes a value in
  the unit of its setting (the `unit` column of `list-settings`), as a plain decimal with no
  exponent: a setting without a unit of its own uses the unit of its kind (the table), so all three
  are `10 ns`. The time limits use `s` (`timeout: 2 min` is `120 s` in `settings.json` and in
  `xeda run --json`; a default shows in the unit too, as `6000 s` for `lic_wait` of `vcs`), and the
  simulated times and delays use `ns`.
- Other numbers stay plain: counts, ratios and fractions, percentages (the name ends in
  `_percent`), sizes of data (the description names the unit) and values "in the units of the
  library" (passed to the tool unconverted).
- Times, frequencies and delays of every flow take a unit, among them `timeout` (`2 min`),
  `stop_time` (`100 us`), `prerun_time`, `time_unit`, `time_resolution` and `lic_wait` of `vcs`,
  the Vivado `default_{max,min}_{input,output}_delay`, the two `max` delays of `dc`,
  `hold_detour_max` of `nextpnr` and `freq` of `openfpgaloader` (`6 MHz`). A bare number is refused
  (`invalid_quantity`). Only the numbers of `xeda dse` stay plain.

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
that way: `JsonNetlist`, `EcpConfig`, `VerilogNetlist`, `GenericVerilogNetlist`, `VhdlNetlist`, `FpgaNetlist`,
`FpgaTimingNetlist`, `SynthCheckpoint`, `RoutedCheckpoint`, `OpenroadDbPlaced`, `OpenroadDbCts`,
`OpenroadDbGlobalRouted`, `OpenroadDbRouted`, `OpenroadSdcPlaced`, `OpenroadSdcCts`,
`OpenroadSdcGlobalRouted`, `OpenroadSdcRouted`, `Chipdb`, `Data`. `.odb` is one of the four
`OpenroadDb` types, by stage. `Data` has no
automatic HDL frontend; a flow or the design can still read it. An invalid `type` is an error
that names the closest types; type names are case-tolerant, suffixes are not.

A source table has `file` (must exist at load time) or `path` (not checked), and `type`; a source a
generator writes is a name in its output (see `generator`). Any other key is an error: a source has no `standard` or
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
| `VerilogNetlist` | a Verilog netlist mapped to the cells of a Liberty library. Give a gate-level `.v` this type, or it is `Verilog`. |
| `GenericVerilogNetlist` | a Verilog netlist of the cells of `yosys` (gates, LUTs, coarse cells); no flow takes it, and alone it gives the error that no flow reads the design's sources |
| `FpgaNetlist` | a Verilog netlist of FPGA primitives as Vivado writes it; `vivado_postsynth_sim` simulates it |
| `FpgaTimingNetlist` | its timing netlist, simulated with an SDF; Verilog has no suffix for it, so give `type` |
| `SynthCheckpoint`, `RoutedCheckpoint` | a Vivado checkpoint after synthesis, after routing (`vivado_power` takes only the routed one) |
| `Checkpoint` | no stage; no flow takes it |

A source of the wrong one of these types is not taken in its place: a `JsonNetlist` source does
not replace the `Edif` netlist that `vivado_impl` reads. A listed netlist is your claim: Xeda does
not parse it or compare it with `platform`, `liberty` or `fpga`. The tool that reads it reports
cells it does not know. `yosys` writes `netlist` when it maps to a Liberty library, else
`netlist_generic`.

A source of a later stage replaces the flows that would build it: a `JsonNetlist` skips synthesis
for `nextpnr`, an `Edif` netlist skips it for `vivado_impl`, a `Fasm`, `EcpConfig` or `IceAsc`
configuration is packed by `fpga_pack` without placing, and a `Bitstream` is what
`openfpgaloader` programs.

Each flow declares the source types it reads. It skips other non-language types (an `Lpf` in a
Vivado design). At planning it refuses a source in a language it cannot read (VHDL for `verilator`,
Bluespec or SystemVerilog for `ghdl_sim`) and a design with no source it reads; the error lists the
sources with their types and the types the flow reads. A Bluespec source is no refusal for a flow
that reads Verilog, because `bsc` converts it first. One source of a type the flow reads is enough,
a constraint file included. Simulators, `bsc` and `vivado_project` check `tb.sources` too. C and
C++ files are never refused. `nextpnr`, `fpga_pack`, `openfpgaloader`, `vivado_impl` and
`vivado_power` read only their declared inputs.
`bsc` compiles Bluespec Classic only from `.bs` files and refuses a `.bh` source.

## `language`: standards

```yaml
language.vhdl.standard: 2008 # 93, 2008, 2019, ...
language.verilog.standard: 2005
```

Write the four-digit year; a two-digit year needs quotes
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
    stop_time: 100 us
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
the flat form), not `name`, `targets` or `target`. `--target NAME` on `xeda run` and `xeda dse` selects it.

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

- A target is optional. Without `--target`, Xeda uses the design itself, whatever the number of
  targets. An unknown name is an error that lists the targets. `--target` on a design without
  targets is an error. API: `Design.load(path, target=...)`.
- Where design and target both write a key, the target wins with no error (its
  `flows.nextpnr.board` replaces the design's); other keys stay. Mappings merge at every depth,
  `sources` append, any other list replaces. A null or empty `flows` table or section adds
  nothing. Layers, lowest first: flow defaults, user file, host profile, project, design, target,
  project-local file, `-s`, API.
- A short form means its table in every merge (`clock: CLK` is `clock.port: CLK`; `fpga: <part>`
  is `fpga.part: <part>`). A target's `clock` refines the design's first clock, its `clocks`
  replaces all, `clock: null` means none. A value
  that is no table where one is expected (`tb: 3`) is an error.
- `--design-updates KEY=VALUE...` merge by the same rules, except that only a target appends
  `sources`: an override's `rtl.sources` or `tb.sources` replaces the whole list.
- A target name matches `[A-Za-z][A-Za-z0-9_-]*` and is not a flow's name or hashed run
  directory name; two names that differ only in case are refused. A design file has no `target`
  key. `--json` documents report `target`.
- A selected target has its own run directories, `<run root>/<design>/<target>/<flow>`; runs made
  without a selected target are left alone. `xeda scrub FLOW DESIGN --target NAME` removes only
  that target's runs; without `--target` it removes every target's runs and the runs without a
  target too.
- `board`, `fpga` and `custom_boards_file` go under the target's `flows.<flow>`; at the target's
  top level they are refused, naming that.

## `dependencies`: other designs

A dependency adds its `rtl.sources` and `tb.sources` to the design's, and fills the `rtl.top`, `rtl.parameters`, `rtl.clocks` and `tb.top` that the design leaves out, but only with `rtl: {pos: -1}` in the reference (the default `pos: 0` adds sources only). Loading reads none of them. Planning (`xeda run`, `--dry-run` too, `xeda dse`) does.

- Local: the path of a design file, relative to the design. It must exist when planning starts: a generator cannot write it (`design_file_not_found`).
- Git: `git+<url>[?branch=<name>|commit=<hash>]#<design file>`, or a table with `repo_url`, `design_file`, `branch`, `commit`. `commit` is a full hash; an abbreviated one is refused. Planning fetches it into `<run root>/.dependencies/<key>/checkout/`, which is never changed or removed by Xeda (delete it by hand while no launch runs). `clone_dir` and `local_cache` were removed (`removed_key`).
- A pinned `commit` already fetched needs no network. A branch, a tag or no reference is resolved by `git ls-remote` at every planning; a failure is `fetch_failed` (field `dependencies[<n>]`). Pin a commit to work offline. With `--remote` the client plans first, so it needs access to the Git server; the host then fetches the commit that the client resolved (the record keeps the branch as written).
- The commit is no part of the identity, but another commit makes the run stale ("dependency <repository> now at <commit>"). `results.json` has `dependencies`, one per entry in the listed order (`repository`, `reference`, `commit`, `design_file`; a local entry has `design_file` only). Entries merge in the order listed, local and Git alike.

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

- Top-level keys: `designs`, `flows`, `run_root` (relative to the project file; default
  `xeda_run` beside it; the CLI uses the selected project's run root unless `--run-root`,
  `XEDA_RUN_ROOT` or a `run_root` of the project-local or user file is set, else `./xeda_run` when
  no project is used) and `default_target`. Any other key fails with
  `ProjectError: ... unknown key(s) ...; a project has 'designs', 'flows', 'run_root',
  'default_target'`. The singular `design` and `flow` were removed (`removed_alias`).
- `designs` is a list (a single table is `invalid_project`). An entry is a table with a `name` (each name once; its root is the project's directory), or `{file: hw/fifo.yaml}`, which
  holds nothing else and takes the name of that design file. Each table entry and each flow
  section follows the rules of a design file, dotted keys included. A dotted key cannot reach
  into `designs` from the project's root.
- Xeda finds the file as `--xedaproject FILE`, else the one `xedaproject.yaml`, `.yml` or `.toml`
  in the start directory. Two of these names in one directory is a `ProjectError`
  (`several_project_files`).
- A project design is reached by its name, `--design-name sqrt`; with no design given, a project
  with one design is selected (`design_name_required` if there are several). `DESIGN` is always a file
  path; a name there that is a design of the project is refused (`use_design_name`). A design file
  given by path: (1) a `file` entry lists it, by a relative path, an absolute path or a symbolic
  link: refused, `use_design_name`, "use --design-name <name>"; (2) the project does not list it:
  it gets no layer from the project (not its `flows`, no settings file of the project), and the
  user's layers still apply; (3) the project, given or found, still supplies the run root
  (`--run-root`, `XEDA_RUN_ROOT`, the project's, then `./xeda_run` with no project). Both `DESIGN` and `--design-name` together
  are a usage error.
- `--target T` selects a target, `--no-target` selects none, and neither selects the project's
  `default_target` for a design of the project (`target_required` if the design lacks it).
