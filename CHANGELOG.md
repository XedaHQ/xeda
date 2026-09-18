# Changelog
All notable changes to this project will be documented in this file.


## [Unreleased]

### Fixed
- Packaging: the declared minimum Python version is now 3.11, matching the CI matrix and current
  dependencies. Advertising Python 3.10 caused dependency resolution to fail because Pint now
  requires Python 3.11 or newer.
- Packaging metadata now uses the SPDX license format required by current setuptools releases.
- Command line interface:
    - `list-settings`: improved display of types and default values
    - `list-settings`, `list-flows`: setting and flow names are no longer truncated to the
      terminal width (a truncated name could not be typed back into `-s KEY=VALUE`)
    - `list-settings` no longer hides the settings shared by every flow (`ncpus`, `dockerized`,
      `docker`, `lib_paths`, `redirect_stdout`, `clean`, `quiet`, ...)
    - `list-flows` reports each flow's own description instead of an inherited base-class
      docstring, and folds aliases into their flow instead of listing them as separate flows
    - `scrub`: non-incremental run directories were never matched
- Flows: `open_xc7` and `yosys_sim` were listed by `list-flows` but could not be run or
  inspected. Flows are now registered under their canonical (snake_case) name, and flow lookup
  is dash- and case-insensitive. An unknown flow name now suggests close matches.
- `xeda.flows.__all__` exported `CxxRtl` (a settings model) as if it were a flow, and omitted
  the `YosysSim` flow.
- Clocks: every command-line way of setting a clock period (`-s clock_period=5.5`,
  `-s clocks.main_clock.period=5.5`, `-s clocks.main_clock.freq=200MHz`) failed with
  `unsupported operand type(s) for /: 'float' and 'str'`. Non-positive periods and frequencies
  now report a clear error, and `clock_period` is kept in step with `clocks`.
- `nextpnr`: the flow asked nextpnr for a JSON report (`--report`) and never read it, so it
  produced no timing or utilization results. It now reports `Fmax`, `wns`, `timing_met`,
  `clock_frequency`, `clock_period`, `clock_domains`, canonical `lut`/`ff`/`bram`/`dsp`/`io` for
  ECP5, the raw nextpnr bel-type counts for any family, and per-domain/per-cell/critical-path
  detail; the report file is recorded as the `report` artifact. Results are reported even when
  timing fails, which is when they matter most. `wns` is derived from the constrained and achieved
  clock periods, since nextpnr reports frequencies rather than slack.
- `nextpnr`: the LPF pin-constraint file was passed to nextpnr twice, so constraints were read
  twice.
- `nextpnr`: slack was rounded before its sign was tested, so a violation as small as
  -0.00025 ns was reported as `timing_met: true`. Pass/fail now uses the unrounded value.
- `xeda run --remote`: the remote flow's result was discarded and the run always reported
  success. Status now comes from the remote results, which are included in the JSON document
  along with the local run path.
- `--json`: a missing design, an unknown flow, an unknown command-line option, and design-space
  exploration setup failures all exited without writing a JSON document. Every failure now emits
  one, and an unknown optimizer names the available ones.
- `--json`: output from design generators and from a remote flow went to stdout, corrupting the
  JSON document. Both now follow the same redirection as local tool output.
- Flow names: the command line rejected names the resolver accepts. `VivadoSynth`, `ghdl` and
  `OpenXC7` now work wherever a flow name is taken, and an unknown name suggests close matches.
- `xeda design-schema` now describes the *input* syntax of a design file: the flat top-level
  form (`sources`/`top`/`clock` at the root), `test` as an alias for `tb`, field names as well as
  aliases (`language`/`hdl`), and the shorthands validators accept (`tb.top = "tb"`,
  `tb.cocotb = true`). A source object must name a `file` or a `path`. Every example design is
  tested against both the schema and the loader, so the two cannot drift.
- Tool version detection now retries with stderr folded into stdout for both native and Docker
  tools. Tools such as nextpnr that print their version banner to stderr no longer report an empty
  version. ISE's container wrapper accepted `merge_stderr` and then dropped it on the way to
  `Docker.run`, so any caller asking for stderr on that path silently did not get it.
- cocotb 2.1 support: from 2.1 the GPI library no longer discovers its Python entry point on its
  own and exits with "No GPI_USERS specified", so every cocotb simulation failed. Xeda now sets
  `GPI_USERS` (libpython followed by the PYGPI entry point), mirroring `cocotb_tools.runner`. The
  entry point is probed via `cocotb-config --pygpi-entry-point`, which older releases reject, so
  cocotb 2.0 keeps working unchanged. An existing `GPI_USERS` is respected.
- cocotb results: every simulation reported `cocotb.sim_time_ns` as 0. The attribute branch of
  the results parser was unreachable (it tested a local it had just set to `None` rather than the
  parsed attribute), and from cocotb 2.0 the per-test metadata moved out of `<testcase>`
  attributes into a JUnit `<properties>` block, which the parser did not read at all. Simulated
  time, the random seed and the testbench source location are now reported for both layouts, and
  a passing test no longer has its status read from the `<properties>` element as "PROPERTIES".
  Pass/fail detection was unaffected.
- Units: `ns`, `us`, `ms` and `ps` are spelled out before parsing. `pint` resolves `ns` to both
  *nanosecond* and *nanosiemens* and picked between them nondeterministically, so a value such
  as `"5.5ns"` was sometimes rejected.
- `xeda run --design-file` / `--design` were silently ignored: the option shared its destination
  with the positional `DESIGN` argument, and the argument always won. Both spellings now work,
  with the positional argument taking precedence.
- `xeda design-schema` accepted a source object naming both `file` and `path`, which
  `Design.from_file` always rejects as mutually exclusive. The schema's object branch is now a
  `oneOf`, so schema validation and the loader agree.
- A zero clock frequency (`-s clock_period=... freq=0`, `PhysicalClock(freq=0)`) reported
  "Neither freq or period were specified" instead of "Clock frequency must be positive". Zero is
  a supplied value, not an omitted one.
- `xeda list-results yosys` advertised `cells` and `sequential_cells`, neither of which
  `parse_reports()` ever wrote. `cells` is now read from the report's `num_cells`;
  `sequential_cells` has no counterpart in yosys' `stat` output and is no longer claimed.
- `xeda skill install` resolved the packaged skill directory inside an `importlib.resources`
  context and copied from it afterwards. On a filesystem install the path outlives the context, but
  from a zip (or any non-filesystem loader) the extracted directory is deleted on exit and the
  install would fail. The copy now happens while the context is open.
- A design source given as an object (`sources = [{ file = "a.vhdl" }]`) -- the form
  `xeda design-schema` documents -- failed to load with `unhashable type: 'dict'`: the sources
  validator deduplicated its input by hashing every element. `utils.unique()` now falls back to
  equality comparison for unhashable items.
- A non-positive `clock_period` flow setting was accepted. An explicit `0` was indistinguishable
  from an omitted one and was silently replaced by the main clock's period, and a negative value
  was never checked at all; both now report "Clock period must be positive".
- `--json` left the rich console and tool output pointed at stderr after the command finished.
  In a one-shot `xeda` process this was invisible, but when the CLI is driven repeatedly in one
  process (`click.testing.CliRunner`, or use as a library) every later human-facing command wrote
  to the finished command's stream and appeared to produce nothing. Both are now restored when the
  invocation ends.
- `Design.schema()` raised `ValueError: Value not declarable with JSON Schema`.
- Design sources recorded their type as an integer ordinal (`"5"`) in `settings.json` instead
  of a name (`"Vhdl"`). Old files are still read correctly.
- `openroad` with a non-default ASAP7 corner (`-s corner=FF`) wrote the default corner's supply
  voltage into its scripts (`VDD` 0.7 V instead of 0.77 V). ASAP7 spells `VDD` as `$(VOLTAGE)`,
  and selecting a corner did not re-evaluate it. `AsicsPlatform.select_corner()` now does, and the
  source expression survives the platform being reloaded from `settings.json` or copied.
- `nvc` with a cocotb testbench: when the design also gave the testbench generics of its own, `nvc`
  elaborated with those instead of the RTL generics the flow had just copied over (`ghdl` used the
  right ones). `generics` and `parameters` are two spellings of one setting, and assigning
  `generics` left `parameters` stale. Assigning either one now updates both.
- `ise_synth` quoted its project properties again each time its settings were re-validated -- on
  any assignment, and on reloading `settings.json` -- so `"High"` became `""High""`. Values in
  `translate_options` were never quoted at all. All five option groups are now quoted exactly
  once, when the Tcl script is generated.
- Malformed values in design and settings files escaped as raw `AttributeError` or
  `FileNotFoundError` tracebacks instead of validation errors naming the field: for example
  `parameters = ["W"]`, a `verilog_lib` file that does not exist, or a `set_attribute` that is not
  a mapping. An unknown platform (`-s platform=asap8`) now lists the bundled platforms.
- `fpga = "xc7a100tcsg324-1"`, the shorthand the `fpga` setting documents, was rejected. A bare
  part number is now accepted there.
- Repeating `-s`/`--settings` (`-s clock_period=5 -s impl.strategy=Debug`) kept only the last
  group of overrides and silently dropped the others. Every occurrence now adds to the settings.
- `xeda dse` with a missing or non-numeric `init_freq_low` crashed with `KeyError:
  'init_freq_low'` instead of reporting that setting. An `init_freq_high` that is not above
  `init_freq_low` is now a validation error rather than an `assert`, which `python -O` skips.
- Docker: the environment file of a tool given by absolute path was named `._docker.env`, and a
  `Docker` configuration shared between tools could keep the first tool's name. Both are now named
  after the tool's own executable (`.ghdl_docker.env`).
- `semantic_hash()` of a model included values cached by `cached_property`, so hashing a `Tool`
  changed once its version had been probed, and objects written to `settings.json` through the
  JSON fallback could carry those cached values too. Hashing a whole `Design` recursed without end
  on its source types.

### Changed
- **pydantic 2.** Xeda now requires `pydantic >= 2.13.5, < 3` (previously `>= 1.10.22, < 2`).
  Design, settings, board and platform files that loaded under 1.x load unchanged: a number given
  for a string setting (`speed = 2`, `MAX_BRAM = 0`, `compile_args = ["-j", 8]`) is still accepted
  as text, and a malformed value is still reported as a validation error naming the field. Code
  that uses xeda as a library must move to the pydantic 2 model API (`model_dump()`,
  `model_validate()`, `model_json_schema()`, `model_copy()`).
- The minimum `importlib_resources` version is now 7.1.0.
- `ise_synth` records its project properties in `settings.json` as written (`High`, not
  `"High"`) and quotes them only in the generated script. Its settings therefore hash differently,
  and `--cached-dependencies` re-runs an ISE dependency once.
- Help screens are now rendered by [click-extra](https://github.com/kdeldycke/click-extra) instead
  of `click-help-colors`, and `click` is required at 8.5 or newer. The xeda palette is unchanged
  (yellow headings, green options); options, choices, metavars, environment variables and defaults
  are now highlighted as well, and every group gains a `help` subcommand (`xeda help run`). Colors
  follow the `NO_COLOR` / `FORCE_COLOR` / `CLICOLOR` conventions.
- Examples now require cocotb 2.1 (`examples/requirements.txt`).
- `xeda dse` now exits with a non-zero status when the exploration produced no successful run,
  matching `xeda run` and the other commands. It previously always exited 0.
- A flow's settings for its dependency flows keep following it after construction: assigning
  `fpga`, `board`, `clocks` or `clock_period` on `nextpnr`, `open_xc7` or `openfpgaloader`
  settings updates the `yosys_fpga` dependency (and, for `openfpgaloader`, `nextpnr`) too. Each
  dependency gets its own copy, so editing one flow's settings never alters another's.

### Removed
- Dependency on `click-help-colors`, replaced by `click-extra`.

### Added
- Machine-readable CLI output, for scripts and coding agents:
    - query commands take `--format {table,json,jsonl,yaml}` with `--json` as a shorthand:
      `list-flows`, `list-settings`, `list-results`, `design-schema`, `list-boards`,
      `list-platforms`, `list-optimizers`
    - executional commands take a `--json` flag that writes a JSON summary to stdout and moves
      tool output, logs and result tables to stderr: `run`, `dse`, `scrub`
- New commands: `list-results <flow>`, `design-schema`, `list-boards`, `list-platforms`,
  `list-optimizers`
- `xeda.introspect`: programmatic access to flows, settings, result keys, the design schema,
  boards, platforms and DSE optimizers
- `python -m xeda` now works, and the package ships a `py.typed` marker
- An installable skill that teaches coding agents to drive Xeda: `xeda skill install` writes it to
  `./.claude/skills/xeda/`, regenerating the flow catalog from the installed version so it cannot
  drift. `xeda skill reference` prints the catalog. Also checked into this repository, with an
  `AGENTS.md` pointer for non-Claude agents.
- Documentation: every one of the ~520 flow settings now has a description, every flow has its own
  docstring, and every flow documents the keys it writes to `results.json`
  (`xeda list-results <flow>`). `tests/test_documentation.py` keeps it that way.
- `dc` additionally reports `num_cells_sequential`. The key it has written since v0.2.5 is
  `num_cells_sequentual`, a misspelling baked into the area-report parser; the correct spelling is
  added alongside it and the old one keeps being reported, so existing scripts are unaffected.
- Settings: `$DESIGN_ROOT`, `$DESIGN_DIR` and `$PWD` are now expanded wherever a setting holds a
  path, not only in a single-path setting: in path lists (`sdc_files`, `xdc_files`,
  `target_libraries`, including a comma-separated `-s` value), in mappings (`dc.hooks`), in the
  path half of a `lib_paths` entry, and in settings that take either a string or a path (`vcd`,
  `fst`). Strings that are not paths, such as a `lib_paths` library name, are left as written.
- Results: flows declare `results_description` via `describe_results()`; the runner additively
  records canonical keys (`Fmax` from `f_max`/`maximum_frequency`, `lut` from `LUT`, `ff` from
  `FF`) alongside whatever the flow reported, so scripts need not know which flow produced the
  file. Nothing is renamed or removed.
- Docs: filled the empty pages and rewrote the outdated quickstart; added references for the
  design file, run directories and the machine-readable interface. The README's tool catalog names
  every registered flow, with its aliases and what it does; `tests/test_documentation.py` fails if
  a flow is missing from it or if it names one that does not resolve (`vivado_postsynthsim` was
  listed for a long time, but the flow is `vivado_postsynth_sim`).
- Tool: ecppll (nextpnr's Lattice ECP5 PLL tool)
- Examples: ULX3S adopted DVI test from EMARD
  - change LED chaser speed with buttons `5` and `6`!
- Examples: improved version of `blinky` for the ULX3S board in VHDL
  - change LED chaser speed with buttons `5` and `6`!

## [v0.1.0-alpha.11] - 2022-04-16


[Unreleased]: https://github.com/XedaHQ/xeda/compare/v0.1.0-alpha.1...HEAD
[v0.1.0-alpha.11]: https://github.com/XedaHQ/xeda/releases/tag/v0.1.0-alpha.11
