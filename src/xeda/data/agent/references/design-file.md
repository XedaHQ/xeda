# Xeda design file reference

A design description is one TOML, YAML or JSON file. It says *what* the design is, not how to
build it - apart from the optional per-flow settings at the end.

The authoritative machine-readable definition: `xeda design-schema`.

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
| `dependencies` | no | Other designs this one depends on. |
| `license`, `version`, `url` | no | Metadata; no flow reads these. |
| `design_root` | no | Base for relative paths. Defaults to the design file's directory - almost always right. |

The loader also accepts `sources`, `top`, `clock`, `clocks`, `parameters`, `generics`, `defines` and
`generator` at the top level. It folds them into `rtl`, so an explicit `[rtl]` section is not
required when this flat form is used.

## `[rtl]` - the design

| Key | Required | Meaning |
| --- | --- | --- |
| `sources` | yes | Source files **in compilation order**. Relative to the design file's directory. |
| `top` | no | Top-level module/entity. Required by synthesis flows. |
| `parameters` / `generics` | no | Verilog parameters or VHDL generics for the top level. Use either interchangeable name; giving both is an error. |
| `defines` | no | Verilog preprocessor macros. |
| `clock` | no | Canonical single-clock description, e.g. `{ port = "clk" }`. |
| `clock_port` | no | Compatibility shorthand for a single-clock design; prefer `clock`. |
| `clocks` | no | A list of clocks, for multi-clock designs. |
| `attributes` | no | HDL attributes, as `attribute -> (object -> value)`. |
| `generator` | no | Command or generator class producing the sources before the flow runs. |

## `[tb]` - the testbench

The aliases `test` and `tests` are also accepted. The section has the same `sources`,
`parameters`/`generics` and `defines` as `[rtl]`, plus:

| Key | Meaning |
| --- | --- |
| `top` | Toplevel testbench module. Up to two, as a list, when a secondary toplevel is needed. |
| `uut` | Instance name of the unit under test inside the testbench. Some flows need it for waveform dumping or activity capture. |
| `cocotb` | `true`, or a table with `module`, `toplevel`, `testcase`. Detected automatically from a `.py` source. |

## Clocks

Clocks are split deliberately: `[rtl]` names the design's clock **ports**; the **period or
frequency** is a *flow setting*, because it constrains a particular build.

Single clock:

```toml
[rtl]
clock = { port = "clk" }

[flows.vivado_synth]
clock.period = 5.0          # nanoseconds
```

or, equivalently, `clock.freq = "200MHz"` in the flow section. The legacy `clock_port` and
`clock_period` inputs are accepted for compatibility. Within one settings layer, use only one
spelling for a concept; across layers the higher-precedence spelling wins and is merged into the
canonical `clocks` mapping. Prefer `clock` in the design and `clock.period` or `clock.freq` in
flow settings.

Multiple clocks:

```toml
[[rtl.clocks]]
port = "clk"
name = "main_clock"

[[rtl.clocks]]
port = "clk_aux"
name = "aux"

[flows.vivado_synth.clocks.main_clock]
period = 5.0

[flows.vivado_synth.clocks.aux]
freq = "100MHz"
```

A `PhysicalClock` takes `period` (ns) or `freq` (MHz), and derives the other. A consistent pair is
accepted for compatibility; a contradictory pair is an error. Both accept unit strings
(`"5.5ns"`, `"200MHz"`, `"0.2GHz"`). Units are case-sensitive, as in SI: `"200mhz"` is an error
that names `MHz`. It also takes `rise`, `duty_cycle`, `uncertainty`, `skew` and `port`.

## Source files

A path string is enough; the type comes from the extension:

| Extension | Type |
| --- | --- |
| `.vhd`, `.vhdl` | `Vhdl` |
| `.v` | `Verilog` |
| `.sv` | `SystemVerilog` |
| `.vh` / `.svh` | `VerilogHeader` / `SVHeader` |
| `.bsv`, `.bs`, `.bh` | `Bluespec` |
| `.py` | `Cocotb` |
| `.cc`, `.cpp`, `.cxx` | `Cpp` |
| `.sc` | `Chisel` |
| `.xdc` / `.sdc` | `Xdc` / `Sdc` |
| `.tcl` | `Tcl` |
| `.mem` | `MemoryFile` |

A source containing `*` is a pattern (`"src/*.vhd"`); its matches are inserted in sorted order,
and a pattern matching no file is an error. `*` is the only pattern character: `?`, `[` and `]` are
part of a file name, so `"rtl/fifo[1].v"` names exactly that file (never `fifo1.v`), and
`"rtl/fifo[1]_*.v"` matches `fifo[1]_a.v`. Never escape them. Xeda passes such names to yosys and
to TCL-scripted tools intact, but Vivado's `add_files` (every file in `vivado_project`; memory
files, sources of an unknown type, `xdc_files` and `tcl_files` in `vivado_synth`) refuses a name
containing `[`, `]` or `$`: rename those for Vivado.

When inference is not enough, use a table:

```toml
[rtl]
sources = [
  "pkg.vhdl",
  { file = "legacy.v", type = "SystemVerilog" },
  { file = "old.vhdl", standard = "93" },
  { path = "generated/top.v" },          # not checked for existence
]
```

`file` must exist and is checked at load time. `path` is not checked - use it for sources a
generator will produce. Every source carries a content hash, which is how Xeda tells runs apart
and reuses cached dependency runs.

## `[language]` - standards

```toml
language.vhdl.standard = "2008"      # "93", "2008", "2019", ...
language.verilog.standard = "2005"
```

`version` is an accepted alias of `standard`. Two-digit (`08`) and four-digit (`2008`) both work.

## `[flows.<flow_name>]` - per-flow settings

Applied only when that flow runs, so one file can carry constraints for several targets:

```toml
[flows.vivado_synth]
fpga.part = "xc7a100tftg256-2L"
clock.period = 5.0

[flows.openroad]
platform = "sky130hd"
clock.period = 10.0

[flows.ghdl_sim]
stop_time = "100us"
```

`xeda list-settings <flow> --json` lists what a flow accepts. Unknown keys are rejected.

## Environment variables in paths

Path-typed settings expand `$PWD`, `$DESIGN_ROOT` and `$DESIGN_DIR`:

```toml
[flows.dc]
target_libraries = ["$DESIGN_ROOT/lib/SAED90/saed90nm_typ_ht.db"]
```

Design sources expand environment variables too, except `$PWD`; `$DESIGN_ROOT`/`$DESIGN_DIR` are
the design root, so `"$DESIGN_ROOT/src/*.vhd"` and `"src/*.vhd"` are the same sources.

## Multiple designs

A `xedaproject.toml` holds several designs plus top-level `flows` settings merged into each. Select
one with `--design-name`.
