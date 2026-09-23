# Changelog
All notable changes to this project will be documented in this file.


## [Unreleased]

### Fixed
- An FPGA flow launched without a device says so, once, before anything runs, naming the setting
  and how to give it (`-s fpga.part=<part>`, or a `board` for the flows that take one), as a
  `FlowSettingsException` the CLI reports in one line. Each flow used to fail
  its own way: `yosys_fpga`, `nextpnr` and `open_xc7` with `FlowFatalException FPGA target device
  not specified` from inside `run()`, `quartus` and `ise_synth` with a Jinja traceback (`'None' has
  no attribute 'part'`). A flow declares what it cannot run without (`Flow.required_settings`) and
  the launcher checks it at launch -- also on the local side of a remote run, before anything is
  shipped -- counting a value given in a dependency's section when the flow shares it
  (`nextpnr`'s `yosys.fpga`).
- `xeda run --remote` ships a design's sources laid out as they are under the design root, rather
  than flattened into one directory. A Verilog `include` finds the header beside the including
  file first, so flattening could make the remote build another design from the same files: two
  `defs.vh` were renamed apart, and an includer lost the one beside it. A source outside the root
  still travels among the flat sources.
- A design with a dependency is recorded as built: the dependency's sources, merged into the
  design when it is built, are no longer recorded beside a dependency that merges them again. A
  design reloaded from its `settings.json` had every dependency source twice -- so the recorded
  `design_hash` could not be reproduced from the design beside it -- and a remote run got the
  duplicates and tried to fetch the dependency all over again.
- Files a flow writes one per source (GHDL's Verilog output) get distinct names by construction:
  a name that another source would fold to as well -- `my-fifo.vhd` and `my_fifo.vhd`,
  `fifo.vhd` and `fifo.vhdl`, a dependency's `rtl/fifo.vhd` beside the design's own -- carries a
  short digest of its path; every other name is unchanged.
- `$DESIGN_ROOT` (and any other variable) in a source pattern stands for the place it names: a
  design root called `proj[1]` made `$DESIGN_ROOT/rtl/*.vhd` match a sibling `proj1`'s sources,
  and `proj[v2]` matched nothing. A pattern matches files only, and a directory named as a source
  is a validation error naming it, rather than loading and failing later with
  `IsADirectoryError`.
- A `{ path = ... }` source not written yet can be validated again (an assignment, a reload of a
  dumped design): de-duplicating sources compared their contents, which such a source does not
  have yet, and raised `FileNotFoundError`. One resource is one file, by place.
- Clock and timing values are parsed strictly: a number, optionally followed by one unit (`5.5`,
  `"5.5ns"`, `"1e3 kHz"`). They were evaluated as pint expressions, so `"100.mHz"` and
  `"(100)mHz"` got past the unit case check and became a 0.1 Hz clock, `"5 ns * 2"` was a 10 ns
  clock, and `"5_ns"`, `"("` or `"5/0 ns"` crashed with a traceback; `clock.period = true` was
  1 ns, and `"nan ns"`/`"inf ns"` were accepted. All are now field errors showing the expected
  form, and so is an ambiguous unit (`min`).
- A design file that cannot be loaded is a `DesignFileParseError` naming the file -- and the line
  and column, when the parser knows them -- whatever the cause: malformed TOML (a bare
  `TOMLDecodeError` traceback before), malformed JSON or YAML, an unreadable or non-UTF-8 file, an
  unsupported suffix, or a document that is not a table. JSON positions were one off.
- A design that fails validation names its file again.
- `xeda run` and `xeda dse` report a user error -- a design or project file that does not load, a
  design name the project lacks, no design at all -- as one CRITICAL line, or one `--json`
  document typed by the exception's own class, instead of a traceback or a flow that "did not
  complete successfully". Messages no longer repeat their type
  (`DesignValidationError: DesignValidationError: ...`).
- `xeda dse` without `--init-freq-low`/`--init-freq-high` reports the settings the optimizer
  lacks, as a flow's settings are reported, instead of pydantic's `input_value=None`: the CLI
  handed it `None` for an option not given. A flow setting the search cannot run without (an
  FPGA's device) is reported once, before any run starts -- it failed every run of the search
  separately, and was reported only as `NoSuccessfulRun`.
- Launching a flow with a dependency no longer switches off `--post-cleanup` and
  `--post-cleanup-purge` for it, and no longer leaves a reused launcher incremental and
  non-scrubbing: the launcher wrote the dependency's run-directory policy onto its own settings.
- `--no-incremental`'s help said it "backs up or removes" the previous run directory; it deletes
  it, and says so.
- No mapping key turns a JSON document into an error. `json` accepts only text, numbers, `bool`
  and `None` as keys, so results reported under a tuple or `Path` key failed to write
  `results.json` (and a remote run's results failed to come back at all); a key is now written by
  the rule a value is -- its text, an enum its value -- in `results.json`, `settings.json`, the
  results table, a remote run's results, and every `--json` document. A printed document keeps
  the keys the file has (`true`, `null`, a string enum's value), so the two cannot differ.
- A dependency's own settings section reaches the flow that launches it: an `fpga` given only in
  `[flows.yosys_fpga]` is `nextpnr`'s (and `open_xc7`'s, and `openfpgaloader`'s) device as well.
  That section was applied only when the dependency launched -- after the depending flow's
  `init()` had resolved the settings they share -- so `nextpnr` ran yosys and then failed on a
  missing device. It is now the base of the depending flow's own `yosys` settings, which
  `[flows.nextpnr] yosys.*` and `-s yosys.*` refine, locally and for a remote run alike.
- A flow can no longer change the design anyone else sees. Every flow of a run shared one
  `Design` object, hashed before the flows ran and recorded beside that hash, so a flow that
  edited it changed what its dependencies were given and what `settings.json` recorded under a
  hash computed from something else. The launcher now gives each flow its own copy, as it already
  did for settings, and the flows that did edit it no longer do:
    - `yosys`/`yosys_fpga` emptied `rtl.parameters`, so a VHDL top's generics never reached GHDL
      at all: synthesis used their defaults;
    - GHDL rewrote the VHDL standard (`2008` to `08`) and stored the top it found in `tb.top`;
    - Verilator merged the testbench's defines into the RTL's; bsc added `BSV_POSITIVE_RESET` to
      the RTL parameters.
- `ghdl_synth` only analyzes before `ghdl synth`, which elaborates by itself: on the LLVM and GCC
  backends, `ghdl make` rejected two sources sharing a base name ("both compiled to 'fifo.o'")
  before the per-source conversion began. Two sources whose output names would collide are an
  error naming both, raised before anything is converted (a bare `assert` before). Its synthesis
  flags are no longer given twice, and an `(entity, architecture)` top passes both.
- `yosys_fpga` handed GHDL each VHDL file twice (`ghdl ... sqrt.vhdl sqrt.vhdl -e sqrt`): the
  script lists them already.
- Yosys scripts accept paths with spaces. A `.ys` path is quoted where yosys strips the quotes;
  where it passes them on verbatim (`read_verilog -I`, `show -prefix`, the GHDL and slang
  plugins), a space-free link in the run directory stands in. `rtl_graph` no longer crashes,
  `.svh` headers are include directories, and GHDL `lib_paths` become `-P<dir>`.
- VCS is given `+incdir+` for every header directory, and Verilator also searches the
  testbench's header directories (`Design.header_dirs`).
- Liberty files that share a stem no longer overwrite each other when pre-processed, and each no
  longer carries every earlier library's content.
- A PDK's `time_unit` is checked when the platform loads, instead of failing once a run renders
  its SDC or scales the delays it parses.
- `$DESIGN_ROOT` and `$DESIGN_DIR` in a design's file paths (sources in either form, `{ file =
  ... }` parameters, a generator's `sources`) were never expanded: the path kept a literal
  `$DESIGN_ROOT` and failed only once a flow read the file. They now name the design root, so
  `$DESIGN_ROOT/src/a.vhd` and `src/a.vhd` are the same source. A glob is expanded after them, so
  `$DESIGN_ROOT/src/*.vhd` no longer silently matches nothing. However a source's path is
  spelled, and wherever the design sits, it is the same source: see *Changed* for the design
  hash, which counts a source by its path only relative to the design root, and only where other
  files find it by its place.
- A glob in a design's sources expands in a stable, sorted order. `glob` returns filesystem
  order, while source order is semantic -- it is the order VHDL units are compiled in, and it is
  part of the design hash -- so one design got a different compile order, and a different
  identity, on a different filesystem.
- A glob that matches no file is an error naming the pattern, instead of contributing no sources
  at all and letting the design reach a tool missing its top-level unit.
- A generator is skipped by what its sources *name*: `run_only_if_sources_modified` compared the
  raw `rtl.sources` entries against the filesystem, before the validator that interprets them, so
  every spelling but a plain relative path looked absent and the generator re-ran on every load.
  A `{ file = ... }` source reached `Path(dict)` and failed the design with an opaque
  `TypeError` naming no key.
- Expanding a path no longer accumulates state: the environment filter was a module-level list
  that every expansion appended to, so it grew without bound as a design's sources, parameters
  and generator inputs were resolved.
- A design's source files are checked when it is loaded again, as documented: a missing source,
  `{ file = ... }` parameter or generator source is a validation error naming it. It used to
  load and fail only once a flow read the file (a generator source, with a traceback). A source a
  generator creates later is given as `{ path = ... }`, which is not checked.
- `xeda run --json` reports an invalid design file as a `DesignValidationError` (or
  `DesignFileParseError`) with its message, as it already did for a design from a project file,
  instead of only saying the flow "did not complete successfully".
- Clock units are case-sensitive, as in SI, and parse the same on every run. The unit registry
  was case-insensitive, so `"5.5nS"`, `"10uS"` or `"2mS"` were nanoseconds on some runs and
  siemens on others (it depended on Python's hash seed), and `"100mhz"` was silently a 0.1 Hz
  clock (millihertz). A clock unit in another case (`mhz`, `mHz`, `NS`, `Ms`, `KHz`) is now an
  error naming the right spelling (`MHz`, `ns`, `ms`, `kHz`).
- `openroad`: the `optimize` setting never reached synthesis -- the function that builds the abc
  script returned nothing -- so abc always ran yosys's default mapping script. See *Changed*.
- `xeda scrub <flow> <design>` never removed the `<design>/<flow>` directory a default run
  creates, only the hashed ones `--cached-dependencies` makes, and still reported success. The
  `--incremental` help of `run` and `scrub` also described run directory names that only exist
  with `--cached-dependencies`.
- `quiet` no longer depends on the order settings are given in: `verbose` or `debug` given with it
  turned it off, but assigned after it (as a depending flow passes its `verbose` level on to its
  dependencies) did not. `verbose` and `debug` now always take precedence.
- A copy of a tool subclass (`VivadoTool`, `GhdlTool`, the cocotb tool, ...) kept the cached
  version and info of the tool it was copied from, since invalidating only looked at the
  subclass's own cached properties, not inherited ones.
- `PhysicalClock.period_ps = 2500` set a 2500 ns period instead of 2.5 ns.
- A generator that does not write the sources the design declares is reported against the
  generator, naming it and the files it was supposed to produce, when the design loads. A source
  written `{ path = ... }` skips the check the sources validator does, so this used to surface
  far later and far worse: a bare `FileNotFoundError` from inside the design hash, naming a path
  and nothing else. The sources are re-expanded after the generator runs, since a glob is
  exactly what was waiting for it, so a generated source counts the same however it is declared
  -- checked, `{ path = ... }` or a glob all give one design one hash.
- A file that is missing when a design is hashed explains itself: `{ path = ... }` defers the
  existence check, it does not waive it, and a design whose sources have no content has no
  identity. It used to be an errno from `open()`.
- Nothing xeda writes as JSON is serialized by reading an object's `__dict__` any more. That is
  not a serialization format: it is what put a source's private `_specified_path` into
  `settings.json`, it left a `Path` for the encoder to stringify by luck rather than by rule,
  and on a plain `Enum` -- whose `__dict__` carries `__objclass__` -- descending into it does
  not terminate (`semantic_hash` already carried a guard against exactly that). A pydantic model
  serializes through pydantic; anything else states its JSON form in `as_json_value()`; anything
  unrecognized is written as its text.
- `ghdl`: converting a design to Verilog (`--out=verilog` into a directory) failed when two VHDL
  sources shared a filename stem, as `rtl/a/fifo.vhd` and `rtl/b/fifo.vhd` do. The fallback meant
  to tell them apart built its prefix from `Path.parents`, a list of *ancestors*, so the name it
  produced (`vout/rtl/b_rtl_._fifo.v`) still held path separators and named a directory that does
  not exist. Each generated file is now named after the source's path relative to the design root
  (`rtl_a_fifo.v`), via `Design.source_artifact_name`, which any flow that writes one artifact per
  source should use.
- A project file giving both `flow` and `flows` (or `design` and `designs`) is an error instead
  of one being silently ignored, and a design entry that is not a table is reported instead of
  dropped.
- `diamond_synth` constraints use the main clock's period, name and port, and a run without a
  clock reports "diamond_synth needs a clock" instead of failing while writing the constraints.
- A `clock_period` override applied to an existing clock keeps that clock's name and port, and
  command-line and API runs with the same settings now share one run hash.
- Settings layers now merge key by key, in one order everywhere: flow defaults < project
  `flows.<flow>` < design `[flows.<flow>]` < `-s`. Previously `-s yosys.flatten=true` replaced the
  design file's whole `yosys` section, a design's `[flows.nextpnr]` replaced the project's whole
  section, and a remote run let the design file override `-s` (the reverse of a local run).
- A run's hash no longer depends on where anything is: the same settings run from another
  directory, or an identical copy of a design somewhere else, used to get a different hash, so
  `--cached-dependencies` re-ran them. A path under the design (`$DESIGN_ROOT/c.xdc`) now counts
  relative to it. Local and remote runs compute the hash the same way. Design hashes now include
  each source's content, type, `standard` and `variant`, and its position in the compile order,
  plus clock, language, attribute and testbench metadata, so behaviorally different designs
  cannot silently reuse one run. See *Changed* for source and parameter paths, which the design
  hash no longer covers at all.
- Example designs: every `[flows.*]` section now validates. Two named `cxxrtl`, which was never a
  flow (it is `yosys_sim`); `examples/vhdl/xedaproject.toml` duplicated `vivado_synth` under its
  pre-2022 name `vivado_prj_synth`; `sqrt.toml` asked `vivado_alt_synth` for a synthesis strategy
  that only exists for implementation (`ExtraTimingCongestion`, now `ExtraTiming`). A test keeps
  them valid.
- `yosys`: a `netlist_*` switch set after the settings were created -- as the flow itself does to
  write liberty-mapped netlists without expressions (`-noexpr`) -- never reached `write_verilog`.
  The flags are now derived from the switches when the script is written.
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
- Clocks: every command-line way of setting a clock period (`-s clock.period=5.5`,
  `-s clocks.main_clock.period=5.5`, `-s clocks.main_clock.freq=200MHz`) failed with
  `unsupported operand type(s) for /: 'float' and 'str'`. Non-positive periods and frequencies
  now report a clear error. The canonical single-clock setting is `clock.period`/`clock.freq`;
  legacy `clock_period` remains accepted as compatibility input but cannot be combined with
  `clock` or `clocks`.
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
- `Design.model_dump()` and `Design.model_dump_json()` no longer pass `serialize_as_any=True`.
  That flag duck-types every value by shape, so it skipped the serializer an *arbitrary*
  (non-pydantic) type declares on its own core schema -- exactly how `FileResource`/`DesignSource`
  serialize -- which is what made `model_dump_json()` of a design with sources raise
  `PydanticSerializationError`. Polymorphism is now declared only where it is needed:
  `RtlSettings.generator` is `SerializeAsAny[Generator]` (so a `ChiselGenerator` keeps its own
  fields), joining `Design.dependencies`, which already was. Both still prune
  (`exclude_unset`/`exclude_defaults`), so `model_dump_json()` is the same document xeda writes
  through `model_dump(mode="json")`; their `exclude=` of `rtl_hash`, `tb_hash`, `rtl_fingerprint`
  and `tb_fingerprint` is gone, since those are properties, not fields, and were never dumped.
- A design source now serializes to JSON faithfully. The new `FileResource.as_json_value()`
  (overridden by `DesignSource`) emits a bare path string when there is nothing else to say, and
  otherwise a table: `{"file": ...}` plus any `type`/`standard`/`variant` the design *stated* (a
  value merely inferred from the filename suffix is left out, since reloading re-infers it the same
  way), or `{"path": ...}` for an unchecked resource. It previously serialized to a bare path, which
  lost stated compile metadata that is part of the design hash, and turned a `{ path = ... }`
  source -- one naming a file that need not exist yet -- into a checked one, so reloading the
  document it was written to failed on a file the design never promised was there.
- `settings.json` is now written through pydantic. `utils.dump_json` serializes with the new
  `utils.json_encodable`: a pydantic model through `model_dump(mode="json")`, an enum as its
  value, a set as a list, an object declaring `as_json_value()` as that, and anything else as its
  text. It used to read `__dict__` directly, so a `DesignSource` was recorded as its raw instance
  state (private `_specified_path` included), which nothing could load back, and the
  `Path`/enum/`FileResource` conversions the models declare were skipped. `settings.json` is also
  leaner now, because `Design.model_dump()`'s `exclude_unset`/`exclude_defaults` finally apply to
  it.
- The documents `--json` prints and the JSON files xeda writes encode a value the same way:
  `introspect.json_safe` now hands everything that is not plain data to `json_encodable`. It used
  to dump a model in python mode and stringify what was left, which flattened a design's sources
  to bare paths -- dropping a `{ path = ... }` table and every stated `type` -- where the file
  kept them; and a raw set was written to a file as its `repr` text.
- Two pydantic-v1 leftovers are gone: `DesignSource.__json_encoder__` (v2 never calls it, and it
  would have raised anyway, since `json.dumps` cannot encode a `Path`), and the `default=` encoder
  chains in `send_design` and `print_results` that probed for `obj.json` and `obj.__json_encoder__`
  -- in v2 `obj.json` is a deprecated bound method, which those chains would have handed to `json`
  as a value. Both now use `json_encodable`.
- `xeda run --remote` now ships file-valued parameters correctly. `send_design` used to send
  `rtl.parameters` as they were, i.e. as absolute paths on the *sending* machine, so the remote
  handed its tool a path to nothing. A parameter is now re-rooted from its value alone: a path
  under the design root keeps its place relative to the root -- an existing file travels in the
  archive at that same place, a path with no file yet (an output a testbench writes) is only a
  place for the remote to write -- and an existing file elsewhere travels among the sources. Both
  travel as tables, so the remote resolves them against its own design root rather than its
  working directory, and a remote run computes the same design hash as the local run that sent it
  -- which they never did before, since repointed sources alone used to change it.
- A generator is told the design root in `$DESIGN_ROOT` in all three of its forms. The shell
  string and table forms deferred to a `DESIGN_ROOT` the shell happened to export -- another
  project's, say -- and the argv form set none at all. A `DESIGN_ROOT` the generator's own `env`
  states is still kept.
- `xeda run --remote` no longer crashes after fetching the run's artifacts when the local run
  directory is not under the directory xeda was started in (`--xeda-run-dir`, `XEDA_RUN_DIR`):
  a log message computed its path relative to the start directory.

### Changed
- Design- and project-file suffixes are case-sensitive, and read by one table: `.TOML` is rejected
  naming `.toml`, and `.yml` is YAML for a project file too. A string with a design-file suffix is
  always a design file -- for `xeda run --remote` as for a local run -- never a design's name to
  look up in a project.
- `DesignFileParseError`, `DesignValidationError` and `FlowNotFoundError` are `XedaException`s. The
  new `DesignNotFoundError` and `ProjectFileError` are raised by `FlowLauncher.run`, which no longer
  returns `None` for a design it cannot load or find.
- `xeda dse` builds the `best.json` it updates and the `--json` document the CLI prints from one
  method, `FlowOutcome.as_json_value()`, instead of two by hand (one by dumping the outcome's
  `__dict__`); the two documents did not differ in practice. `FlowOutcome` is slotted again --
  `slots=False` existed only for that dump, and `attrs` generates the pickle protocol a slotted
  class needs to cross the process boundary a design-space exploration sends it over.
- **`openroad` synthesizes with the abc script its `optimize` setting selects**, now that it
  reaches yosys: OpenROAD-flow-scripts' area script for `"area"` (the default), its speed script
  for `"speed"`. Both size and buffer for timing, so synthesized area differs from before, when
  abc used yosys's default script.
- **`optimize = "area+speed"` is gone** from `openroad` and `yosys`: it selected exactly the area
  script. Write `"area"`.
- **`yosys` and `yosys_fpga` no longer have an `optimize` setting.** It was documented and
  settable but nothing ever read it; `optimize` is `openroad`'s setting, and reaches yosys as the
  abc mapping script it selects (`abc_script`) together with `post_synth_opt`. Setting it on
  `yosys` is now the error it always was in effect.
- **pydantic 2.** Xeda now requires `pydantic >= 2.13.5, < 3` (previously `>= 1.10.22, < 2`).
  Code that uses xeda as a library must move to the pydantic 2 model API (`model_dump()`,
  `model_validate()`, `model_json_schema()`, `model_copy()`).
- **Settings and design files are checked strictly against each setting's type.** A value of
  another type is an error naming the setting, instead of being silently converted: write text
  settings as text (`speed = "2"`, `compile_args = ["-j", "8"]`), not numbers; `true`/`false` are
  no longer turned into the text `"True"`/`"False"`; a fractional number is no longer truncated for
  a whole-number setting. Settings whose values really are of several kinds say so: Vivado's
  `set_synth_properties`/`set_impl_properties` take text, numbers and booleans (written to Tcl as
  `true`/`false`).
- A list setting may be given as comma-separated text (`-s xdc_files=a.xdc,b.xdc`), now also when
  the list is optional; spaces around items and empty items are dropped, so an empty string is an
  empty list. A setting that also accepts plain text keeps the text whole.
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
- A flow and the dependency it launches (`nextpnr` and `open_xc7` run `yosys_fpga`,
  `openfpgaloader` runs `nextpnr`, `vivado_power` and `vivado_postsynth_sim` run their Vivado
  flows) resolve the settings they share when the dependency is launched: each one (`fpga`,
  `clocks`, `board`, ...) comes from the flow if it is set there, otherwise from the dependency's
  own settings, and both then use that value. A part or clocks given only in the nested
  `yosys`/`nextpnr` settings are therefore used rather than erased, whatever order settings were
  given in.
- `openfpgaloader` no longer requires `clock_period`, like every other synthesis flow, and reports
  a missing target as "set `fpga` or `board`" instead of an empty error.
- `nextpnr` and `open_xc7` take the verbosity level every flow shares (`verbose = 2`) rather than a
  flow-specific switch; any level above 0 passes `--verbose`.
- `vivado_synth` and the flows built on it report a missing `fpga` when their settings are read,
  rather than accepting it and failing later. An FPGA given as an empty mapping is reported as
  missing its device, the same as one with only empty fields.
- A simulation's `vcd` setting of `false` or an empty name writes no waveform.
- `settings.json` records `flow_settings`, the run's input exactly as it is identified (the
  merged layers; feed it back to reproduce the run), and `effective_flow_settings`, what the flow
  made of it. The input is never modified by the flow.
- An FPGA's `speed`, `grade` and `generation` accept a number as well as text (`speed = -1`); no
  other text setting does.
- `generics` and `parameters` are one design setting stored once (as `parameters`); giving both
  in one section is an error rather than a silent choice.
- Library use: validate user-given flow settings with `Settings.from_input(data, design_root=...,
  runner_cwd=...)`, which resolves path variables and reports a `FlowSettingsError`;
  `Settings(**data)` raises pydantic's `ValidationError`. The hidden `design_root_`/`runner_cwd_`
  settings are gone.
- **A design no longer counts where it is.** A source counts by its content, type, `standard` and
  `variant`, and its position in the source order. A source that other files find by its name or
  place also counts by its path *relative to the design root*: a Verilog `include` searches the
  including file's directory first, then the header directories, so two layouts of the same files
  can build different netlists -- counted by content alone they were one design, and
  `--cached-dependencies` reused one's netlist for the other. That covers Verilog and
  SystemVerilog sources and headers, Bluespec, C++, cocotb modules and memory files; VHDL,
  constraints and scripts are named explicitly wherever they sit, and count by content alone.
  Before, the recorded path was absolute whenever a glob or an absolute spelling produced it, so a
  design's identity changed when it moved; moving a whole design now never changes it. Similarly,
  a parameter whose value is a path under the design root -- typically one given as a file
  (`{ file = ... }` or `{ path = ... }`) relative to it -- counts relative to it
  (`$DESIGN_ROOT/rom.mem`), the rule `flowrun_hash` applies to settings; a path outside the root
  (`{ file = "../shared/rom.mem" }`) still counts as the location it names. The parameter's value
  is still the absolute path the tool is handed, and it is the parameter's only value: nothing
  else records how it was written. `file` and `path` differ only in whether the file must exist
  when the design loads, so the two spellings of one path are the same design. A parameter file's
  content is not hashed. **Existing run directories and cached results will not be reused after
  upgrading**, because these changes give every design a new `design_hash`.

### Removed
- Dependency on `click-help-colors`, replaced by `click-extra`.
- `Design.relative_path` (use `Design.source_path_as_named`), and `units.normalize_quantity`,
  `units.UNIT_ALIASES` and `units.unit_maybe_scale`; `units.check_unit_case` now takes
  `(unit, text)`.

### Added

- `xeda run --remote` asks the remote which xeda its interpreter imports, and where from, before
  shipping anything, and logs both (`Remote xeda: 0.4.0 at ...`). The worker is started as
  `python3` by the remote's *non-login* shell -- the `PATH` from its login environment is applied
  only once that interpreter runs -- so it can import another install than the one the user
  upgraded. That is how a stale 0.2 checkout surfaced as `rtl.sources: unhashable type: 'dict'`.
  A remote without xeda, or with one older than the design archive needs (0.4.0), is refused with
  that interpreter and install named; one on another release line is warned about.
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
