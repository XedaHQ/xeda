# Changelog
All notable changes to this project will be documented in this file.


## [Unreleased]

### Fixed
- Generator freshness now follows symlinked directories among its `sources`, validates damaged
  output records as stale, and rechecks its input identity after acquiring the record lock. The selected direct
  generator executable is part of the content identity. A POSIX lease on the existing design-root
  directory serializes bootstrap and differing-identity generations for the same tree without
  creating the run root early; separate roots sharing an external output are outside that lease.
- `xeda run --remote --rebuild-all` forces local generator loading before shipping while the
  remote flow remains fresh; the remote runner's existing fresh-flow policy remains separate from
  local generator freshness.
- `yosys` given a `platform` merged its liberty files only when `dont_use_cells` was set, so a
  platform with an empty dont-use list (`sky130hs`) reached abc as several files, of which abc and
  `dfflibmap` were handed the first; and it never marked the platform's own dont-use cells,
  although `dont_use_cells` promised to add to them. Both now hold.
- A platform's per-corner files (`lib_files`, `dff_lib_file`, `rcx_rules`) sent to a `--remote`
  run arrived as unexpanded `$DESIGN_ROOT/...` text, naming no shipped file and giving the remote
  another run identity: the path fields of a mapping or list of nested models are expanded too.
- A run's identity recognizes a root and a path both as written and as resolved: a design root, a
  start directory or xeda's own installation reached through a symbolic link counts the same
  either way, and a `--remote` run whose run directory lies under a linked path (a linked HOME,
  macOS's `/tmp`) is no longer refused for "a different request identity".
- `yosys`'s `other_maps` takes the platform's latch mapping only when it is left unset: an
  explicitly empty list (`-s other_maps=`) now means no extra mapping, where the platform's latch
  map replaced it.
- `yosys` lists a `timing_report` artifact only when `sta` writes one; a remote run asked for the
  report it never wrote.
- `yosys_fpga`'s `synth_pass_only` reads the design's sources as a bare `yosys <files>`
  does (by each source's `type`, which is its suffix's unless the design states another): a nonempty `read_verilog_flags` (Xeda's own `-sv` default included), a `systemverilog`
  front end other than `default` (the default is the slang plugin) and a nonempty
  `read_systemverilog_flags` are refused at planning, each naming what to write
  (`read_verilog_flags: []`, `systemverilog: default`; `-s read_verilog_flags=` on the command
  line), so the mode reproduces the native netlist for a `.v` source that `-sv` cannot even read
  and for a `.sv` source read by Yosys' built-in front end. The full recipe keeps its readers, and
  a `.sv` source is still read with `-sv`. A run that asked for `synth_pass_only` alone now needs
  those written with it.
- Target loading applies design overrides to dictionary inputs, rejects `targets: null`, and
  keeps the loader-selected target name authoritative over design and project overrides.
- `yosys_fpga` reads each target's primitive library (Xilinx, ECP5, Nexus, iCE40) before the
  design with the flags the synthesis pass itself reads it with (`-lib -specify`, plus the
  device define for iCE40), so the early read and the pass's own agree. (A netlist can differ
  slightly from earlier releases: yosys numbers generated names from a shared counter, and the
  library read advances it.)
- `yosys_fpga`'s mapped Xilinx `LUT` count includes every distributed RAM primitive
  (`RAM32X1D`, `RAM64X1D`, `RAM64M`, ...), not `RAM32M` alone.
- `nextpnr` lists an SDF, routed netlist, SVG or placement dump as an artifact only when this
  run wrote it.
- FPGA board lookup rejects unknown names after resolving the selected custom database;
  7-series part parsing preserves device, package and speed suffix boundaries. Bundled Arty
  pin constraints and a local ULX3S fallback are available without network access.
- Yosys FPGA synthesis loads release- and family-specific primitive libraries, defaults to
  `-sv` with opt-in `-noautowire`, and reports mapped LUT resource estimates with method detail.
- **Settings compose origin first** (project < design < command line < API); a nested section
  such as `openroad`'s `synthesis` refines the dependency's own section only within one origin.
  Locally and with `--remote` alike.
- **`-s flows.<flow>.key=value` sets a setting of any flow in the run** (the requested flow or a
  declared dependency); `-s key` and `-s flows.<requested>.key` are one setting, two values for it
  are an error, and a misdirected or mistyped flow name suggests the right one.
- **`-s` ends at the first token that is not KEY=VALUE** (or at the next option), so it no longer
  swallows the design file; `--` ends the options.
- **A failed run leaves a failure document**: `results.json` with `success: false`, `error.type`,
  `error.message` and the run's identity, also when a dependency fails; the previous
  `results.json` is removed before a run starts.
- **A simulation passes only on evidence that it ended.** A cocotb run, on any simulator, needs
  at least one test that ran and none that failed (an all-skipped run fails). Verilator runs under
  Xeda's own C++ main (a design's own C++ driver keeps its place, and Xeda's hooks still record how
  it ended) and reports `sim.ended_by`, `sim.time` and related keys; it gains `timeout`,
  `fail_severity` (default `error`) and `stop_time`, simulates the testbench's top by name
  (`--top-module`: `tb.top`, else `rtl.top`; with cocotb, `tb.cocotb.toplevel`, else `rtl.top`),
  applies `rtl.parameters` only when the RTL top is the simulated top, and copies the model's output
  to `sim.log` in `sim_dir` (not under cocotb). Every simulator family, including VCS, xsim,
  postsynthesis simulation, delegated Vivado power activity, and all accepted `bsc_sim`
  backends, now uses the same evidence verdict: a silent exit 0 or a drained event queue fails.
  Each flow persists normalized `sim.evidence`, `sim.ended_by`, `sim.time`, `sim.time_unit`,
  `sim.errors` and `sim.warnings`, captures runtime diagnostics and honors the shared
  `timeout` and `fail_severity` settings. A requested stop or Bluesim `max_cycles` must be
  confirmed by the observed time or measured cycle count and final simulated time. Icarus runs
  explicitly through `vvp`, avoiding the generated executable's incompatible shebang on macOS.
- ModelSim batch runs capture an owned logfile and require matching runtime stop reason,
  time and TESTSTATUS evidence. VHDL `std.env.stop` is accepted as completion; Verilog `$stop`
  is an error-rank event and fails at the default threshold. Compilation/loading diagnostics
  are excluded from the runtime verdict.
- A failed dependency's `FlowDependencyFailure` names the dependency and its `results.json`.
- A flow whose reports or checks fail without an exception or a tool exit status leaves
  `error.type = "ReportedFailure"` and a message in its `results.json`, as every failure document
  does; a dependent used to quote an empty error (`dependency yosys_fpga failed: ;`). The name is
  a cause, kept apart from `FlowFailed`, the verdict at the top of the `xeda run --json` document.

- **A delivered output is no longer read again at every launch when nothing about it changed.**
  Xeda held a verified record of the file it had delivered and still read the whole file twice on
  every later launch -- once in the check before the run, once in the copy that then copied
  nothing -- because the record was anchored at the file's own change time, before which a file is
  never settled, so it could never vouch by metadata. A check that does have to read a destination
  now reads the clock of that destination's own file system first
  (`digest.filesystem_time_ns`) and anchors the record it then took to that time, so a later check
  recognizes an unchanged file by its size, mtime, inode change time and inode -- the same trust
  rule every other file Xeda tracks follows -- and reads nothing. In steady state an unchanged
  re-delivery reads the output in the run directory once, to note its digest, and the
  destination not at all; it used to read the destination twice besides. The destination is read
  once, by the first check after it has settled (more than two seconds, `digest.RACY_NS`, after its
  last change), and that read anchors its record; a launch still inside that window anchors
  nothing and reads it twice, once in the check and once in the copy, as before. Nothing is
  trusted that was not verified against a clock read at that moment: a destination whose
  directory takes no marker is read at every launch as before, and one found on another device
  than its anchor was read on is read once, against the clock of the file system it is on now, and
  anchored afresh. Every mutation check is unchanged -- an
  edit given back its old mtime, and a different file put in the destination's place, are still
  refused.
- `verilator` records the cause of the end of a run correctly. `$error`, a failed assertion and
  `$stop` end a run as `sim.ended_by: error` (it was `fatal`, and the log said "a fatal error ended
  it"), and `$fatal` as `fatal`. A report that ends a run is one event: a `$stop` that reached
  Xeda's hooks directly recorded a `stop` and a `fatal` event.
- A simulation whose testbench is written in a hardware description language (Verilog,
  SystemVerilog, VHDL, Bluespec or Chisel) needs `tb.top`. Without it, Verilator ran `rtl.top`,
  which has no stimulus, and the run failed as drained, and NVC failed on `nvc -e` with no top.
  Every simulation flow now refuses the design when it is planned, and the message names `tb.top`.
  Not affected: a cocotb testbench, a design with no testbench, `ghdl_sim` with a VHDL testbench
  (it finds the top with `ghdl find-top`), and a design with a C++ driver of its own for
  `verilator` and `yosys_sim`, whatever HDL its testbench also holds.
- `verilator` includes the header of its hooks by name (`-include xeda_hooks.h`, found in the
  directory the model is built in), no longer by the absolute path of the run directory: the C++
  compiler flags go through make, which splits them at a space. The makefile of Verilator 5.052
  still refuses to build in a directory whose path has a space, so a run root with one still fails
  there.

### Added
- **A design generator is judged by content, not by a modification time.**
  `rtl.generator` runs again only when something it reads or produced changed: the digest of
  every file of its `sources` (a directory counts as every file in it, outside the design root
  too: a library tree, an editable clone), of its selected executable, and of every source its
  last generation left. A generator is an external tool and xeda assumes nothing about its
  language or environment, so what it reads is what `sources` names; a package upgrade xeda
  cannot see needs `--rebuild-all` or `always_runs`. A `touch`, a `chmod`, a `cp -p` or a
  branch round-trip costs a hash rather than a re-run, while an edit given back its old
  modification time is caught. `generated_sources` names which of `rtl.sources` the generator
  writes, when it writes only some of them (an entry that is none of `rtl.sources` is an
  error); `always_runs` says its inputs cannot be judged at all, and a generator declaring no
  `sources` runs on every load anyway.
  The record of a generation is an entry under `<run root>/.cache/generators/`, written under its
  own durable lock beside the Xilinx chip databases -- never beside the design, whose tree holds
  nothing of xeda's. A load with no run root in sight, or one whose run root cannot be written,
  generates every time: the direction xeda takes wherever it cannot prove something is up to
  date. `--rebuild-all` (and `--clean`) regenerates too, which is the escape where something xeda
  cannot see changed; `xeda run --dry-run` still creates and writes nothing, and a generator that
  fails leaves no run root behind. On upgrading, nothing records the generations an earlier xeda
  ran, so every generated design generates once more on its next load -- and `--dry-run` refuses
  to plan it until one real run has recorded that. `--dry-run --rebuild-all` (or `--clean`) plans
  as that launch would load the design, so it refuses a generated design too.
- `vivado_synth` and `vivado_alt_synth` declare their outputs (`netlist`, `netlist_timing`,
  `sdf`, `checkpoint_synth`, `checkpoint_route`, `bitstream`, and `sdf_min` on `vivado_synth`
  alone, which writes both SDF corners), each switched on by its own setting and recorded in
  `results.json`'s `outputs` with its digest. A new `write_timing_netlist` setting writes the
  timing netlist and its SDF on its own: `write_netlist` now writes the functional netlist and the
  exported constraints only, so a run that wanted the timing files asks for both. Both flows now
  precede `openfpgaloader` in a chain (`vivado_synth+openfpgaloader`), which names a bitstream
  when the loader demands one. `vivado_postsynth_sim` still asks its synthesis for both netlists.
- `custom_boards_file` accepts a YAML board database (`.yaml` or `.yml`) as well as TOML, by the
  file's suffix; YAML is read by the same strict YAML 1.2 loader as every other YAML file, so a
  duplicate key or a non-string key names the file and line. Any other suffix is an error naming
  the file and the accepted suffixes. A relative path still resolves against the design directory
  and a board's local `lpf` against the database file's directory, in either format. The bundled
  databases stay TOML.
- **`yosys_fpga` can omit Xeda's pre- and post-synthesis stages** with `synth_pass_only = true`.
  Design parameters, `synth_flags` and explicit ABC9 script selection
  still apply in either mode. An unset ABC9 script preserves each mode's default: the full Xeda
  recipe uses `flow3` and a constrained clock supplies a clock-derived ABC9 delay; pass-only mode
  leaves the script to the synthesis pass and adds no clock-derived delay. `abc9_script` selects
  one of Yosys' installed scripts (`default`, `default.area`, `default.fast`, `flow`, `flow2`,
  `flow3` or `flow3mfs`) in either mode when ABC9 mapping is enabled; the legacy `flow3` setting
  remains supported, but cannot be combined with `abc9_script`. Settings that add Xeda stages or
  separate post-pass operations, including `rmports`, and any reader choice that is not yosys's own
  (`read_verilog_flags: []`, `systemverilog: default` and no `read_systemverilog_flags` must be
  written) are rejected in pass-only mode. This mode alone does not guarantee the
  same result as a native Yosys command: comparisons must match the installed Yosys, source paths
  and order, parameters, synthesis-pass flags and ABC9 script. It makes those comparisons useful for
  isolating Xeda's surrounding stages, without making a netlist quality claim.
- Automatic project discovery accepts one of `xedaproject.yaml`, `xedaproject.yml` or
  `xedaproject.toml`; multiple matches report the conflicting files and ask to keep one.
- **Flow chains**: `xeda run yosys_fpga+nextpnr+fpga_pack design.yaml` runs the last flow of a
  `+`-joined chain, each preceding flow supplying the next one's compatible required inputs
  (`FLOW.OUTPUT` picks one output of a producer). One parser and one edge predicate serve the
  command line, saved bindings, suggestions, `list-flows` and completion. A chain that does not
  fit is a usage error; it suggests a valid chain only when another output of the producer or the
  flows on the required declared default routes between the pair fix it (`nextpnr+openfpgaloader`
  -> `nextpnr+fpga_pack+openfpgaloader`), and has no suggestion otherwise; a flow that programs a
  device can only end a chain, and a flow with no declared I/O (`bsc`, `vivado_project`, the Vivado simulation and power flows)
  runs alone.
  `-s flows.<flow>.key=value` sets any flow of the chain. `--json` adds `request` and per-node
  `node`/`inputs`, lists nodes planned but never entered as `"state": "not run"`, and keeps
  the last flow's `flow`, `results` and exit status. Chains are local: `--remote` and `dse` refuse
  them. Bluespec chains and chains through the other Vivado flows need the later conversion of those
  flows to declared I/O (`vivado_synth` and `vivado_alt_synth` declare their outputs; see below).
- **Saved input bindings**: `flows.<consumer>.inputs.<input>: <producer>[.<output>]` (an ordered
  list for a list input) in a design or project file, on the command line or through the API.
  An explicit binding is chosen before a typed source, which is chosen before the default
  producer. `inputs` is wiring for the resolver, not a setting: it is absent from `list-settings`,
  `settings.json` and the design hash. A chain replaces a binding saved in a design or project and
  is an error, even when equal, against a command-line or API binding of the same input, naming
  the chain position and the origin. A binding for a flow that declares no inputs says so.
- A node's identity (`flowrun_hash`, the hashed directory suffix, the trace) includes where each
  declared input comes from -- the producer's own identity and output, or sources -- never how
  the request spelled it. Equal graphs from a chain, a saved binding and the defaults have equal
  identities; the trace says "declared input bindings changed" when the wiring did.
- `xeda list-flows` shows what each declared flow takes (required or optional), makes and can be
  followed by (`can_follow`, `can_precede`, `target_dependent`, `action_reason` in `--json`), and
  shell completion of `xeda run` completes chains in bash, zsh and fish.
- **Xilinx 7-series builds with openXC7 1.0**: `nextpnr` places and routes Artix-7, Kintex-7,
  Spartan-7, Virtex-7 and Zynq-7000 parts with `nextpnr-himbaechel` (the full part as
  `--device`, typed `Xdc` pin sources, flow clocks as `create_clock`), with the new settings
  `chipdb`, `prjxray_db`, `delay_matrix`, `hold_fix`, `hold_detour_max` and `placement`. The
  die's chip database is generated on first use into `<run root>/.cache/xilinx-chipdb/`,
  identified by the installed toolchain's content, and shared by every design under that run
  root (an `xc7a100t`: about a minute and 3.5 GB of memory, once); the installation is never
  written to. Results: `ff`, `bram`, `dsp`, `io`, `device`, `fabric` (the die whose totals are
  shown), `clock_port` only when the reported domain is itself a top-level port, and `lut` as the
  distinct LUT locations the placement occupies.
- **`fpga_pack`**, a flow that packs `nextpnr`'s configuration (or a typed `EcpConfig`, `IceAsc`
  or `Fasm` source) into a bitstream with `ecppack`, `icepack` or `fpga-as`: `outputs/<design>.bit`
  (`.bin` for iCE40), published only when the packer succeeded with a nonempty file. It programs
  nothing.
- `lut` carries `LUT:STAGE` and `LUT:METHOD` in the open-source FPGA flows. It is reported per
  toolchain and stage and is not certified comparable with Vivado's utilization report.
- A real-toolchain test layer, `XEDA_TESTS_OPENXC7=1` (`tests/test_openxc7_real.py`): an Arty
  A7-100T design from openXC7's demo projects through `yosys_fpga`, `nextpnr` and `fpga_pack`,
  with the same FASM configuration as the upstream Makefile's commands.
- `Flow.prepare_inputs()` registers implicit inputs after producer hand-over and before
  freshness, under the producer's read lease. Prepared inputs are reserved against delivery.
- Declared FPGA I/O specializes types by target family and enables demanded configurations;
  dry-run plans expose effective input/output types. `prjxray_db` agrees across edges whose
  endpoints declare it.
- `xeda run --dry-run` prints the plan in producer order, with run directories, hashes,
  declared input origins and optional outputs switched on for consumers; `--json` emits one
  document. It changes no run root, writes no markers or locks and probes no tools.
  Loading that needs a generator
  or Git dependency fetch is refused before side effects; undeclared runtime dependencies are
  unknown, freshness is not evaluated, and `--remote` is refused.
- `In` / `Out` declarations, exposed by `xeda list-flows --json` as `declared`, `inputs` and
  `outputs`. A declared flow's `results.json` records enabled outputs as path/content-digest
  records; output-record validation failures use `MissingOutput` and the usual run identity.
  Traces record ordered declared input bindings.
- `run_process` and `Tool.run` take `timeout` (the process group, or a named Docker container, is
  stopped; `ProcessTimeout`) and `tee`.

### Changed
- **`openroad` consumes a declared netlist from `yosys`.** The resolver supplies `yosys.netlist`,
  or a typed `VerilogNetlist` source skips synthesis, and `-s flows.yosys.*` with
  `xeda run openroad` now reaches that producer. Platform copies and `openroad`'s own merged
  library are written only in `run()`, so a fresh relaunch runs no tools and rewrites no flow
  outputs. Its merge and all four `set_dont_use` passes keep the platform's plus the user's cell
  restrictions through one template global. **Breaking: `openroad.blocks` was removed**: it fails
  with `` `blocks` was removed: use `flows.yosys.black_box` ``.
- **Breaking: `--remote` needs a remote of remote protocol 1** (`xeda.REMOTE_PROTOCOL_VERSION`), the
  first released protocol: canonical resolved settings, relocated read inputs with their path
  identities, declared output records with checked hand-over, current-run evidence for remote
  simulations, the FPGA build graph (`fpga_pack`, a programming-only `openfpgaloader`), the node
  identity of flow chains (a node's `flow_hash` counts where its declared inputs come from), the
  declared Vivado outputs, and `yosys`'s declared netlist with its ASIC configuration. A remote
  without the marker (every earlier release) or with a lower one is refused before anything
  ships, with an "upgrade the remote xeda" error.
- A run's identity counts a bundled platform's files relative to xeda's installation
  (`$XEDA/platforms/...`), so two installations -- this side and a remote -- agree on it; a
  platform under the design root still counts relative to it, and one elsewhere as its absolute
  location. `flowrun_hash` (and so a hashed run directory's name) changes for every run given a
  bundled platform.
- **`yosys` owns its ASIC configuration.** Given a `platform`, `yosys` alone derives what
  `openroad` used to hand its synthesis: the corner's liberty set, merged into one library in its
  own run directory (named `<platform>_merged`, with the platform's own dont-use cells and
  `dont_use_cells` marked), the flip-flop library, the platform's mapping files, tie and buffer
  cells, abc's driver cell and load, flattening, the abc script `optimize` selects with
  post-synthesis optimization, and a gate-level netlist without attributes or hexadecimal
  constants (`netlist_attrs`/`netlist_hex` unset: kept unless mapping to a liberty library). An
  explicitly given setting is never replaced. `xeda run yosys -s platform=nangate45` produces the
  netlist `openroad`'s synthesis produced, byte for byte. `yosys` declares its gate-level netlist
  (`netlist`, switched on by `netlist_verilog`) and gains `corner`. `openroad` hands its
  synthesis only the settings the two share (`platform`, `corner`, `dont_use_cells`, `clocks`);
  `yosys`'s own settings reach it from a design's or project's `flows.yosys` section.
- **Breaking: `optimize`, `abc_driver_cell` and `abc_load_in_ff` moved from `openroad` to
  `yosys`.** Given to `openroad` they fail with `` `optimize` was removed: use
  `flows.yosys.optimize` `` (and likewise); `abc_driver_cell` is a cell name (text), no longer an
  integer. `abc_driver_cell` and `abc_load_in_ff` given where nothing maps to a liberty library,
  and `stop_after: rtl` with a `netlist_verilog` (the default), are refused before anything runs.
- `openroad`'s `platform` is a `required_settings` entry rather than a required model field, so it
  has the type `yosys`'s and `dc`'s have. The bundled `nangate45` platform names itself, so
  `-s platform=nangate45` and the path to its `config.toml` are one platform.
- **A design selected as a target runs in its own directories**:
  `<run root>/<design>/<target>/<flow>` (or `<flow>_<hash>` with `--hashed-run-dirs`), producers
  and dependencies beside it within the target, so building the targets of one design in turn
  neither re-runs nor overwrites one another's flows. A design without a target (or with an empty
  `targets` table) keeps `<design>/<flow>`. The target name is still no part of any hash. A run
  made before this (`<design>/<flow>`) is not taken for a target's, and a target's launch never
  touches it. A plan belongs to the target it was made for: an equal design of another target is
  refused, and so is a target that is no name (one that is a flow's, or that differs from
  another target of the design only in letter case, is refused when the design loads).
  `DefaultRunner.run_path_of`/`get_flow_run_path` take `target=`.
- **`xeda scrub` takes `--target NAME`**: it removes only that target's run directories of the
  flow (`<design>/<NAME>/`), and without it the flow's run directories directly under the design
  and under every target, as found on disk (no design file is read; run directories of other
  flows are never searched). The directories are listed and confirmed once. `--json` gains
  `target`, and `scrubbed` now lists the run directories removed (it listed the design's directory).
  A directory that is no longer what was listed once scrub holds its lock (replaced, renamed,
  turned into a link, or out of the run root) is refused rather than removed.
- **A run directory's lock file is `<run dir>.lock` beside the run directory, whatever it has
  become**: the parent is resolved, and the last component only when it is a link staying inside the
  run root (a launch through the link's name and one through the real name share one lock), so a
  run directory replaced by a link out of the run root no longer sends the lock file there. The
  launcher, remote runner, DSE purge and scrub refuse to lock a directory reached through a link
  out of the run root, or one that is itself a link out of it or to nowhere.
  The names of removed flows (`open_xc7`, `openxc7`, in any spelling) are refused as target
  names, as flows' are, since their legacy run directories may be under `<design>/`.
- **Delivery and `replacing_copy` use the platform's copy primitive** instead of a hand-written
  byte loop: `fcopyfile` on macOS, `copy_file_range` then `sendfile` on Linux, between the two open
  descriptors the atomic write needs, looped until the whole file is copied. Any failure,
  including one after some bytes were written, starts over from a rewound source and an emptied
  destination, last with the plain loop, so a half-copied file is never completed by appending.
  The destination is a separate file with the same logical content. On Linux, `copy_file_range`
  may use filesystem reflinks (shared copy-on-write extents) or a server-side copy. The temporary
  file, digest re-check and rename around the copy are unchanged. Ubuntu CI exercises the Linux
  copy path on a real kernel.
- **Breaking: YAML is read as YAML 1.2, strictly.** One shared loader reads every YAML design and
  project file. `yes`, `no`, `on`, `off`, `y` and `n` are text, not booleans (write `true` or
  `false`); the octal `010` is gone (`010` is decimal 10; write `0o10` for octal) and the
  sexagesimal `1:30` is text; timestamps and underscore-separated numbers (`1_000`) are text;
  `0x1F` is 31 and scientific notation such as `1e3` is a float. Duplicate mapping keys, and
  keys that are not strings, are errors naming the file and line. Core explicit tags and ordinary
  aliases are accepted; non-core tags, merge keys and recursive aliases are rejected. Quote values
  meant as text. TOML and JSON input are unchanged.
- **Breaking: a boolean setting accepts only `true` and `false`.** pydantic's lax booleans used
  to read the text `yes`, `on`, `y`, `t` and `1`, and the numbers `1` and `0`, as booleans, so
  `debug: yes`, `debug: 1` and `-s debug=on` worked. They are now errors that say what to write
  (`` `yes` is text, not a boolean: write `true` ``, `` `1` is a number, not a boolean: write
  `true` or `false` ``), for flow settings, design fields and the
  command line alike, and a word YAML 1.1 read as a boolean that reaches a field that is not
  one says so too (`` `on` is text in xeda YAML (YAML 1.2): write `true` ``). A number that
  reaches a text field says to quote it. The command line's `-s` text `true` and `false`
  (case-insensitive) remain booleans; `try_convert_to_primitives` no longer converts `yes`/`no`.
- The remote runner finds its project file with the same helper as a local run
  (`xedaproject.resolve_project_file`): more than one of `xedaproject.yaml`, `.yml` and `.toml`,
  or a named file that does not exist, is a `ProjectFileError` (now defined in
  `xeda.xedaproject`, still importable from `xeda.flow_runner`), and `""` means none given.
- All shipped example designs and projects now use YAML, and documentation and the agent
  skill show YAML first. TOML and JSON designs/projects remain accepted; bundled board and
  platform databases stay TOML. Trivium keeps its former TOML configuration in `trivium.yaml`
  and its distinct DC-capable configuration in `trivium-dc.xeda.yaml` (formerly
  `trivium.xeda.yaml`).
- **`openfpgaloader` only programs.** It takes `fpga_pack`'s bitstream, or a typed `Bitstream`
  source, and builds nothing itself: its `nextpnr`, `packer_args` and `bitstream_file` settings
  are removed (use `[flows.nextpnr]`, `[flows.fpga_pack]` and a `Bitstream` source). `verify` is
  accepted only with `write_flash`. The default graph is
  `openfpgaloader -> fpga_pack -> nextpnr -> yosys_fpga`. `results.tools` records the
  programmer's version (asked with `-V`, which touches no device).
- A failed `nextpnr` is reported by the errors in its log: a constraint error at its original
  file and line, a missed timing constraint as such (`timing_allow_fail` keeps the result), and
  anything else as the tool's own failure -- never by a parser warning.
- nextpnr takes pin constraints from typed design sources in source order, then falls back
  to the board file when none are supplied. Typed SDC sources precede the `sdc` setting's file;
  duplicate clock constraints fail with their original locations. The `lpf_cfg`, `pcf_cfg`
  and `pdc_cfg` settings are removed with typed-source migration messages.
- Remote simulations apply the shared simulation evidence rule and fail simulations that exit
  successfully without confirmed completion evidence.
- All `SimFlow` families now share `timeout` and `fail_severity` (`warning`, `error`, `failure`
  or `fatal`; default `error`). Failure and fatal have the same rank. `timeout` bounds each
  subprocess invocation containing simulation, including analysis/elaboration in a combined
  invocation; it is not a cumulative dependency deadline. Nonzero process exit always fails.
- Breaking: nvc's `exit_severity` setting was removed; use `fail_severity`. VHDL
  `std.env.stop` is accepted as completion, while Verilog `$stop` remains an error. Breaking:
  bsc's uncertified legacy `cvc`, `cver`, `isim`, `ncverilog` and `veriwell` backends are now
  rejected before compilation; supported choices include Bluesim, Verilator, Icarus, ModelSim,
  Questa, VCS, vcsi and xsim.
- CXXRTL accepts an observed exit 0 from a linked user-owned C++ driver even when simulated time
  is unknown, but rejects `stop_time`. xsim requires source-preserving `elab_debug`; explicitly
  setting it to `off` is rejected so the adapter can identify VHDL `std.env.stop` separately
  from Verilog `$stop`.
  xsim's requested `stop_time` is an absolute bound across prerun and runtime, measured from its
  actual current time.
- Real-tool verification: ModelSim-Intel Starter 2020.1 and Vivado 2024.2 were verified.
  VCS and Questa were not verified against licensed tools; they fail closed when recognizable
  native evidence is absent. VCS quiet `$finish(0)` and VHDL completion fail without a native
  finish diagnostic, and UCLI time checkpoints alone do not prove HDL completion. Real Icarus
  evidence and builtin-task registration remain mandatory Linux CI gates; M3 remains deferred.
- **Every design source has a type.** A suffix xeda cannot type -- unknown (`.txt`), ambiguous
  (`.json`, `.bin`, `.cfg`, `.config`) or in another letter case (`.VHD`) -- is a load error
  asking for `type = "..."`, with `Data` for a file with no automatic HDL frontend. An invalid
  explicit `type` is an error naming the closest ones (it used to fall back to the suffix
  silently). New types: `Lpf`, `Pcf`, `Pdc`, `JsonNetlist`, `EcpConfig`, `IceAsc`, `Fasm`, `Bitstream`,
  `VerilogNetlist`, `VhdlNetlist`, `Blif`, `Edif`, `Ucf`, `Xcf`, `Qsf`, `Ldc`, `Fdc`, `Sdf`,
  `Spef`, `Saif`, `Vcd`, `Fst`, `Ghw`, `Vpd`, `Fsdb`, `Checkpoint`, `Liberty`, `Def`, `Odb`,
  `Gds`, `Cdl`, `Chipdb`, `C`, `CHeader`, `ObjectFile`, `Vlt`, `Data`; `.init` and `.hex` are
  `MemoryFile`. A design listing a newly typed file (a `.c` source) re-runs once.
- A flow hands its tool only the source types it reads: Quartus no longer writes `XDC_FILE` or
  `MEMORYFILE_FILE` assignments, Vivado, Diamond, ISE and DC no longer add a source of a type
  they cannot use, and a language a flow cannot read is an error naming the source.
- **Settings connected flows share (`fpga`, `board`, `custom_boards_file`, `clocks`, `prjxray_db`,
  `platform`, `corner`, `dont_use_cells`) must agree wherever both endpoints of a declared edge
  declare them.** `platform`, `corner` and `dont_use_cells` are `yosys`'s and `openroad`'s; a
  `platform` is compared by what it describes and handed on whole, never merged key by key. On the FPGA path
  `fpga` and `clocks` are shared by `yosys_fpga`, `nextpnr`, `fpga_pack` and `openfpgaloader`;
  `board` and `custom_boards_file` by the last three (`yosys_fpga` takes neither); `prjxray_db`
  only by `nextpnr` and `fpga_pack`. Different values in two places are an error naming both
  (the depending flow's value used to win silently); an explicit CLI leaf wins for
  the whole connected group, preserving unrelated leaves. API overrides keep their
  highest-precedence origin.
- `nextpnr` records the selected ECP5, iCE40 or Nexus configuration; an enabled output that is
  missing or stale fails, while disabled/out-of-context outputs remain absent.
- `nextpnr` takes its netlist from `yosys_fpga`'s recorded output, checked by content, or from a
  `JsonNetlist` among the design's sources (synthesis is then skipped); it no longer reads
  `yosys_fpga`'s settings or run directory. A flow reading a dependency's outputs holds that
  dependency's run directory for reading, so a concurrent xeda process that would rebuild it
  waits (POSIX only). Scrub and DSE purge use the same exclusive lock, and scrub
  retains durable lock files.

- **Verilator simulation**: `random_init` now defaults to false, as in Verilator, and
  `random_seed` is used only with it; `x_initial`/`x_assign` default to `"0"` (were `"unique"`);
  `stop_time` with cocotb or with a design's own C++ driver is an error (only Xeda's own driver
  enforces it); `generate_systemc` without an `sc_main` among the design's C++ sources is an error
  (Xeda's driver runs a C++ model); Verilator 5.024 or newer is required.
- **The run root is `--run-root`** (`XEDA_RUN_ROOT`; the API's `run_root`, the launchers' first
  argument and property; the key `run_root` of `xeda scrub --json`), for `run`, `dse` and
  `scrub`: it is the directory holding every run directory. `--xeda-run-dir`, `XEDA_RUN_DIR` and
  the API keyword and property `xeda_run_dir` fail naming their replacement. `xeda dse` and `Dse`
  use one default, `./xeda_run`, shared with `xeda run --hashed-run-dirs` (`Dse` defaulted to
  `xeda_run_dse`).
- `xeda run`, `dse` and `scrub` read only the environment variables they declare:
  `XEDA_RUN_ROOT`, `XEDA_DEBUG`, `XEDA_LOG_LEVEL`, `XEDA_DETAILED_LOGS`; for `run`, also
  `XEDA_REMOTE`; for `dse`, also `XEDA_XEDAPROJECT`, `XEDA_OPTIMIZER`, `XEDA_DSE_SETTINGS`,
  `XEDA_OPTIMIZER_SETTINGS` and `XEDA_MAX_WORKERS` (`--xedaproject`, `--optimizer`,
  `--dse-settings`, `--optimizer-settings` and `--max-workers`, declared on purpose rather than
  dropped with the automatic ones). Every other option had an automatic `XEDA_<OPTION>` variable,
  so a leftover `XEDA_CLEAN=1` emptied every run directory on every run, with nothing on the
  command line to turn it off.
- **Every run lives in a run root xeda created and marked** (`.xeda-run-root`, a `.gitignore` of
  `*`, and a `CACHEDIR.TAG`, each added only where that name is not already present), the first
  time it is used; a directory named as the run root that already holds files and carries no
  marker is refused, naming it and the fix, before anything runs. The one exception: a directory
  named `xeda_run` directly in the start directory is adopted by that name and location alone,
  whatever put files there, with a log line instead of a refusal -- keep nothing of your own in a
  directory called `xeda_run` beside where you run xeda from.
- **An output setting given a location is delivered, not written there directly.** `-s
  bitstream_file=$PWD/sqrt.bit` still writes the run's copy under the setting's fixed conventional
  name in the run directory (`outputs/sqrt.bit`), and copies it to `$PWD/sqrt.bit` once the whole
  launch has finished; moving or renaming the destination therefore never changes what the tools do
  or re-runs the flow. `--outputs-to DIR` delivers the requested flow's artifacts the same way, and
  `--overwrite-outputs` allows replacing a destination that is not xeda's own unchanged earlier
  delivery (otherwise refused, or asked about at an interactive terminal). A destination is never a
  directory, never an input of the launch (a dependency's, or a later flow's), and never inside any
  run root.
- **Working locations are names, not places to reach**: `sim_dir`, `bobj_dir`, bsc's `info_dir` and
  `verilog_out_dir`, `impl_folder`, `results_dir`, DC's `alib_dir`, log paths and the like are
  always used as bare names inside the run directory, whatever they are given.
- Deliveries are made once the whole launch has finished -- dependencies included -- never onto a
  file any flow of the launch reads. `--remote` delivers `--outputs-to` only after a run that
  succeeded, from the artifacts of its local mirror (always the hashed layout).
- `$PWD` (and `$DESIGN_ROOT`) now also expand inside a nested setting given as a mapping or model
  instance, such as `cocotb.results_xml` and `yosys_sim.cxxrtl.filename`, not only in a
  `Flow.Settings`'s own top-level fields.
- `vcs` writes its waveform at a relative path inside the run directory, not the design root.
- `xeda dse`'s log and best-run record, `open_xc7`'s generated chip database
  (`<run root>/.cache/chipdb`), and a git dependency's clone (`<run root>/.dependencies`, unless a
  `clone_dir`/`local_cache` is configured) now go into the run root rather than beside the design.
- A container mounts the design root and the RTL/testbench source directories read-only; the run
  directory a flow's own tools write in, and a `Docker.mounts` entry you configure yourself, are
  writable. (A dependency's outputs are not yet mounted at all for a dockerized flow that needs
  them; that is the planned `docker_mounts`.)
- **A design's name must be a name** (`[A-Za-z][A-Za-z0-9_-]*`): it becomes a path component under
  the run root, so `..` or a `/` in it could otherwise point outside.
- A run directory that is itself a symbolic link is used when it resolves inside the run root
  (0.4.3 refused every such link, wherever it led); one that leads out of the run root is still
  refused, before anything runs. A symbolic link a tool leaves in its run directory is never
  followed out of it: a path a flow writes or removes through one that leads out is refused,
  naming the link, and an artifact that is a link to a directory outside the run directory, a
  dangling link or a cycle of links is not delivered.
- Whether a file a run read changed while the run went on is judged by the file's own identity
  and metadata against its record from before the run, never by comparing its times with a clock
  of another file system. A file a run is found to read only afterwards (a `yosys -E` depfile's
  entry, such as yosys's own library files) has no such record: on the run directory's file
  system its clock still decides, but on another one nothing does, so the next launch runs once
  more, saying so ("input first read by the last run, on another file system").
- A flow's identity counts the parts of the design it reads (`Flow.design_parts`, which replaces
  `reads_source_parts`): the RTL for synthesis and implementation flows, the RTL and the testbench
  for simulations, `bsc` and `vivado_project`. A `design_hash` in `results.json` and in the trace
  is that of those parts, and a flow's trace records only their files as inputs, so editing a
  testbench no longer makes `vivado_synth`, `yosys` and the other flows that never read it run
  again. Only the flow's own freshness is scoped: a flow whose producer reads the testbench still
  runs again with its producer. The first launch after upgrading runs each such flow once.

### Removed
- **`rtl.generator.run_only_if_sources_modified`**: use `always_runs`. A generator's re-run
  decision is its inputs' and outputs' content, never a modification time, so the old switch had
  nothing left to mean; `run_only_if_sources_modified = false` is `always_runs = true`.
- **The `open_xc7` flow**: use `fpga_pack` to build and `openfpgaloader` to program. Its name in
  any spelling, and a `[flows.open_xc7]` section in a design or project file (or
  `-s flows.open_xc7.*`), fail with that message; `xeda scrub open_xc7 <design>` still removes
  the run directories it left.
- **The `<design>_<design_hash>/` run-directory layer.** It only ever appeared with
  `--cached-dependencies --no-incremental`; delete any such directories by hand, xeda no longer
  looks for them.
- CLI options `--cached-dependencies` (use the default, which reuses unchanged runs, and
  `--hashed-run-dirs` to keep settings variants side by side), `--no-cached-dependencies` (use
  `--rebuild-all`) and `--incremental`/`--no-incremental` on `run` and `scrub` (run directories
  are always reused now; `--clean` empties one before running). Launcher settings
  `cached_dependencies`, `skip_if_previous_run_exists`, `incremental` and `cleanup_before_run` (use
  the default, `hashed_run_dirs` and `clean`). Each fails naming its replacement rather than being
  silently ignored.
- The per-flow `clean` setting and verilator's `clean_before_run` (use the `--clean` CLI option).
  GHDL's `clean` is renamed `clean_before_analyze` (whether `ghdl remove` runs before analysis),
  unrelated to the run-directory `--clean`.
- **`--cwd`** (a flow's run directory was never anything but a directory xeda chose):
  `` `--cwd` was removed: use --outputs-to . to receive the outputs here; the run itself goes
  under the run root (./xeda_run)``. With it go the launcher setting and the `launch_flow`/
  `run_flow` parameter `run_path` (`` `run_path` was removed: use run_root to choose where runs
  go (a flow always runs in a directory xeda creates under it), and outputs_to to receive its
  outputs elsewhere``), and constructing a `Flow` without a run path (it is now a required
  argument). The ownership record a `--cwd` run kept, `.xeda-owned.json`, and the lock file inside
  a `--cwd` directory (`.xeda.lock`, which was never released on Windows) go with it -- an older
  xeda's `--cwd` runs may still have left these files behind; see below for what to delete by hand.
- An unmarked `xeda_run_dse` or `xeda_run_<optimizer>` holding runs of an older xeda is refused,
  the same as any other run root that holds files and carries no marker: delete it, or create its
  `.xeda-run-root` to hand it to xeda.
- **The names of the files an older xeda's `--cwd` runs left in your own directories.** Nothing
  reads or manages them any more; delete them by hand: `<flow>.tcl` and the other generated
  scripts and constraints, `results.json`, `settings.json`, the four Vivado hook scripts,
  `<design>.xpr` and its project directories, `reports/`, `outputs/`, `checkpoints/`,
  `.xeda.lock`, `.xeda-owned.json`.
- The `.xeda-run-dir` marker 0.4.3 wrote into every run directory, and the adoption of an
  unmarked directory holding an earlier run of the same flow: the run root's marker makes
  everything under it xeda's. A leftover `.xeda-run-dir` is an ordinary file of the run
  directory.
- **Breaking: `Design.from_toml`.** It only called `Design.from_file`, which reads a YAML, TOML or
  JSON design by the file's format, so the name claimed a format it did not parse. Call
  `Design.from_file` (same arguments); `Design.from_toml` is gone and raises `AttributeError`.
  Bundled platform databases are TOML only, and `Platform.from_toml`, which does parse TOML,
  stays.

### Added
- **`xeda run` is make-like by default.** A flow re-runs only when something it consumed or produced
  changed since its last successful run; `--rebuild-all` (API `rebuild_all=True`) runs every flow,
  as every run did before. A flow that runs logs why (`Running <flow>: <reason>`); a
  flow left alone logs that it is up to date and shows its previously recorded results. Staleness
  covers: no successful previous run; changed settings (named in the reason); a changed xeda
  version or flow code/templates; a changed program (path, size or mtime; a container image's
  ID); a dependency that ran again (until per-edge cutoff arrives, this always re-runs what
  depends on it); an input added, removed, changed or missing; an output deleted or edited; or
  changed design metadata (`top`, parameters, defines). Dependencies are always brought up to
  date before the flow depending on them is judged. Each run directory records this in a new
  `trace.json`, written last and atomically after a successful run and removed before the next
  run executes, so its mere presence certifies the last run there succeeded. A file counts as
  unchanged by size and mtime unless it was touched within 2 seconds of the trace (a racy
  timestamp), otherwise by content hash -- a `touch` or a branch round-trip costs a hash, not a
  re-run, and a file restored with a stale mtime is still caught. `openfpgaloader` and `open_xc7`
  (`Flow.is_action`) always run and keep no trace, since programming a device changes the outside
  world.
- `--hashed-run-dirs` (API `hashed_run_dirs=True`) gives each variant of a flow its own directory
  (`<design>/<flow>_<16-char run hash>/`, the first 16 characters of `flowrun_hash`: the flow's
  input settings and where its declared inputs come from, so editing a source file never moves
  it); the default is one directory per flow (`<design>/<flow>/`). A dependency's run directory is always a sibling of the flow that launched
  it, in the same layout, never nested under it -- so two dependencies of one flow, or the same
  flow run for two different dependers, each get their own directory. Within one launch, a run
  directory is entered at most once: two different configurations of one flow resolving to the
  same directory in the same launch is now an error naming both requesters, instead of the second
  silently overwriting what the first produced. `xeda run --remote` always mirrors its results in
  the hashed layout, so remote runs of different settings never share a directory (`--rebuild-all`,
  `--clean` and `--hashed-run-dirs` are refused with `--remote`).
- `--clean` empties each flow's run directory before it runs and runs every flow ("make clean,
  then make"; it implies `--rebuild-all`).
- A POSIX lock file (`<run dir>.lock`, beside the run directory; none on Windows) serializes
  concurrent launches of the same run directory, so two overlapping invocations sharing a
  dependency take turns with it instead of one clobbering the other's output. `xeda scrub` removes
  the lock file along with the directory.
- `xeda run --json`'s document gains `nodes`: one entry per flow the run touched (dependencies
  included), in completion order, as `{"flow", "run_path", "state": "fresh"|"ran"|"failed",
  "reason"}`. A fully fresh run is `"success": true` with every node `"fresh"`; `nodes` is `[]`
  for an error before anything ran.


## [v0.4.3] - 2026-09-29

Everything up to and including v0.4.3; earlier 0.x releases were not recorded separately.


### Fixed
- xeda could delete a user's files, in three ways. `xeda run <flow> <design> --cwd`, started in
  a directory holding files -- the design's own directory, typically -- deleted every file there:
  `--cwd` makes the current directory the run directory, and the `clean` of `vivado_synth`, `dc`
  and `vcs` (on by default) empties the run directory before the flow runs; an API
  `DefaultRunner(run_path=...)` did the same. A run directory xeda chose was emptied or removed
  whoever's it was: with `--xeda-run-dir myrundir`, a user's `myrundir/<design>/<flow>/` lost its
  files to that `clean`, `--clean`, `--no-incremental` or `--scrub`, and a design named `..` put
  its run directory outside the run root. And files outside the run directory were deleted
  before a run: an existing file at an output path named outside it (`-s bitstream=...`,
  `vivado_sim`'s `saif`), or whatever a work-directory setting such as Verilator's
  `sim_dir = "../x"` pointed at.
  Now xeda marks every run directory it uses with a `.xeda-run-dir` file. A directory given with
  `--cwd` or `run_path` is used only if it does not exist, is empty, or is marked. A run directory
  xeda chooses must lie inside the run root, and a dependency's inside its depender's -- a design
  or flow name such as `..` or `a/b`, or a link leading out, is refused -- and is used only if it
  does not exist, is empty, is marked, or holds an earlier xeda run of the same flow (its
  `settings.json` says so: existing `xeda_run` trees keep working, and are marked on their next
  run); `xeda scrub` and `--scrub` remove only such directories, and `--remote` fetches its
  results only into such a directory, refusing any other before it connects. A run directory is
  a directory itself, never a symbolic link to one: a `run_path` given as a link to a marked
  directory -- or a chosen or dependency's run directory that is a link -- was used, and the run
  cleaned and wrote in whatever the link leads to. Anything else is
  refused, naming the directory, before anything is created, written or deleted. **`--cwd` therefore needs an empty directory, or one xeda
  made**: from a directory holding your files, run without it (the run goes to
  `xeda_run/<design>/<flow>`). xeda deletes nothing outside the run directory: an earlier copy of
  an output is removed only inside it, and a work directory or file a flow removes by name
  (Verilator's `sim_dir`; with `cleanup_bobjs`, the `bobj_dir`, `verilog_out_dir` and `sim_dir`
  of `bsc` and `bsc_sim`; Diamond's `impl_folder`; Vivado's `xsim.dir`; cocotb's results file)
  must lie inside it -- and the tool is handed exactly the path checked: Diamond's script, which
  deletes its `impl_folder`, received it inside double quotes, where Tcl substituted a `$...` or
  `[...]` in it into another directory. Nor does xeda write through a symbolic link: a file it
  generates -- a script, constraints, `settings.json`, `results.json`, a copied resource, the
  marker -- replaces a link at its name rather than writing to the file the link points to, and
  replaces what is there only with a complete file: one whose writing failed half-way leaves the
  earlier one.
  Unchanged in this release: an output path named explicitly (`-s bitstream=/elsewhere/x.bit`)
  is written by the tool where you said, as in 0.4.2, replacing a file already there -- though a
  run whose tool did not rewrite it no longer reports the earlier file as its own. Confirmation
  before replacing an existing file at a named output path comes in 0.5. Files put into a run
  directory of xeda's are removed by its next `clean`.
- Flows record the outputs they write, so `results.json` names them: `ghdl_synth`'s Verilog in
  single-file mode (`generated_verilog`, a list in both modes), `vivado_alt_synth`'s checkpoints,
  netlists, SDF and exported XDC, `vivado_sim`'s VCD and SAIF, `dc`'s mapped netlists, `.ddc`,
  SDF and SDC, and `quartus`'s `.sof` (`bitstream`) -- each only when the setting that writes it
  is on. A failed run no longer lists outputs it never wrote: they are dropped from its results
  with a warning naming them, where a failed `--remote` run used to crash fetching them instead
  of reporting its failure. Nor does it list one an earlier run left in a reused run directory
  (`quartus`'s `.sof`, `dc`'s mapped netlists, `vivado_sim`'s VCD, `vivado_alt_synth`'s
  checkpoints): an output counts as the run's own only if it changed from the state it was in
  before the run -- its identity, size and times, compared with what xeda recorded of it then,
  never with a clock, so an output on another file system whose clock differs is judged alike.
  A failed `--remote` run fetches and lists only the outputs the remote vouches its run wrote,
  judged on the remote's own file system the same way, even on a remote with an older xeda --
  never one merely because a file is there, such as an earlier run's output named outside the
  remote run directory.
  `dc` no longer renders `write_sdf -version None` when `sdf_version` is unset.
- A cocotb simulation fails when its tests fail, on every simulator: `verilator` never read
  cocotb's results, so a failing test gave a successful run. A missing or unreadable results
  file, or one in which no test ran, is a failure too, and an earlier run's results file is
  removed before simulating -- when a test module failed to import, the stale file in a reused
  run directory passed the run on `ghdl_sim` and `nvc` as well. `cocotb.testcase` works on
  cocotb 2.x, which ignored it and ran every test; on every release a name selects exactly the
  test of that name (`check`, never `foo_check`).
- A simulator without cocotb support (`modelsim`, `vcs`, `vivado_sim`, `yosys_sim`, ...) given a
  cocotb testbench fails at launch, naming the simulators that can run it, rather than
  simulating without cocotb and reporting success although no test ran. It is checked before a
  run directory is touched, before a `--remote` run connects, and before a `dse` search starts.
- `netlist_src_attrs` and `netlist_unset_attributes` apply to every netlist yosys writes, the
  JSON one included: the attributes were removed only after the JSON netlist was written, and
  only when a Verilog netlist was written too (a JSON-only configuration failed to render its
  script). They are now removed from the modules themselves and from library boxes as well, and
  `src` whenever `netlist_src_attrs` is false, whatever `netlist_attrs` is. `yosys` and
  `yosys_fpga` on their own therefore strip `src` by default, as documented; the synthesis that
  `nextpnr` and `open_xc7` run keeps it by default, since nextpnr's reports cite source
  locations, unless `netlist_src_attrs` is set explicitly.
- `ise_synth` and `diamond_synth` produce the bitstream: ISE never ran bitgen ("Generate
  Programming File"), and Diamond's Export step was disabled; it now runs with `-task Bitgen`.
  The bitstream is recorded as the `bitstream` artifact there and in `open_xc7`, which kept it in
  a private results key, along with each flow's reports or intermediate files -- each only if it
  exists. A successful ISE or Diamond run that wrote no bitstream fails, naming the expected
  path. A failed ISE process fails the run (`xtclsh` exited 0 after one), and `ise_synth` removes
  its own outputs from a previous run first, so a stale bitstream cannot pass for a new one.
- `vivado_synth` writes the outputs it registers, at the paths it registers them: in project
  mode `write_checkpoint` wrote no checkpoint, the netlists, SDF and exported XDC were registered
  at paths nothing wrote, and the two SDF corners were swapped. Checkpoints go in
  `outputs/synth_design/` and `outputs/route_design/`, and the netlists, SDF and XDC in
  `outputs/route_design/`. They are recorded as `checkpoint_synth`, `checkpoint_route`,
  `netlist`, `netlist_timing`, `sdf_min`, `sdf_max` and `xdc_exported`, the labels
  `vivado_alt_synth` uses. The functional netlist is now Verilog. A requested bitstream comes
  from Vivado's own `write_bitstream` step, so `impl.steps.WRITE_BITSTREAM` settings apply to it,
  and a `.bin` from `ARGS.BIN_FILE` is copied beside it. A negative slack stops the run only
  with `fail_timing`. `vivado_postsynth_sim` and `vivado_power` run again: neither initialized
  Vivado. They now read what their dependencies recorded instead of guessing paths. The
  functional simulation uses the functional netlist, and power uses the routed checkpoint and the
  SAIF the simulation recorded.
- `vivado_synth` fails when a Vivado run it launched does not complete the step it was launched
  to. `wait_on_run` returns normally for a failed run in Vivado 2021.1, so a failed synthesis,
  implementation step or `write_bitstream` (a design without pin constraints fails its DRC) was
  reported as a successful run, without the bitstream asked for; Vivado 2024.2 raises an error
  there, which stopped the script without saying which run failed. The script now checks each
  run's `STATUS` and `PROGRESS` and stops with one message naming the run, its status and its
  `runme.log`. The `status` result, documented but never set, records Vivado's status of the
  last run the script waited for, on success and failure alike (`vivado_alt_synth`, which has
  no runs, no longer lists it). A run that reports success although the requested bitstream is
  missing fails, naming the expected path.
- A Vivado synthesis script that failed to read a source ended with Vivado's `invalid command
  name "errorExit"` instead of the error itself: `vivado_synth` and `vivado_alt_synth` called
  `errorExit`, which only `vivado_sim`'s script defined. It is now one of the procs the Vivado
  scripts share (`util.tcl`), which every script calling one includes.
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
  duplicates and tried to fetch the dependency all over again. The record holds the whole merge,
  including a testbench the dependency supplies to a design that has none of its own, and the
  merged sources are validated like any others, so a file the design and its dependency both list
  is compiled once (it was compiled twice, and the reloaded design hashed differently).
- Files a flow writes one per source (GHDL's Verilog output) get distinct names by construction:
  a name that another source would fold to as well -- `my-fifo.vhd` and `my_fifo.vhd`,
  `fifo.vhd` and `fifo.vhdl`, a dependency's `rtl/fifo.vhd` beside the design's own -- carries a
  short digest of its path; every other name is unchanged.
- Only `*` makes a source a pattern; `?`, `[` and `]` are ordinary characters of a file name. As
  glob syntax, `rtl/fifo[1].v` -- a bus index in a name -- named the other file `rtl/fifo1.v`
  whenever one existed, silently, and failed to load when none did; `rtl/fifo[1]_*.v` matched
  `fifo1_a.v` rather than `fifo[1]_a.v`. A generator's sources follow the same rule.
- yosys reads every file by its own name. Its `read_verilog`, `read_liberty`, `techmap -map` and
  `dfflibmap -liberty` expand a file name as a glob pattern of their own, so a source, liberty
  file or map named `fifo[1].v` was read from `fifo1.v` whenever one existed -- a successful run
  with the wrong netlist. The scripts name those files escaped (`fifo[[]1].v`); every other path
  is left as it is, since the commands that take a name literally find no escaped one.
- A TCL-scripted flow is given every source, and every constraint or script file a design or
  setting names, as one literal word (`tcl_word`, `tcl_quote`, `tcl_list`, shared by every flow's
  templates). Written raw or merely double-quoted, a path with a space was split, `$v` read as a
  variable, and `[x]` run as a command -- Vivado's TCL even started an external program whose name
  begins with `x` -- while a numeric index (`fifo[1].v`) got through by an accident of Vivado's
  `unknown`. Vivado's `read_verilog`, `read_vhdl` and `read_xdc` get a one-file list, since they
  split a single word at its spaces; `vivado_alt_synth`, `diamond_synth` and `modelsim` no longer
  `eval` a command with a path in it, which parsed the path again. So is every setting holding
  text a user writes (a path, a name, a list of them): `diamond_synth`'s `impl_folder` and
  `impl_name`, `syn_cmdline_args` and the design's name and top; `vivado_sim`'s `saif`, `vcd`,
  `xelab_log`, `work_lib` and `vcd_scope`; `vivado_alt_synth`'s `bitstream`; `vivado_power`'s
  `power_report_xml`; `dc`'s TLU+ files, `target_libraries`, `compile_command`, `compile_args`
  and SDF options; `openroad`'s `dont_use_cells` and `place_density`; and the flows' report,
  output and checkpoint directories. So is every text of the design a Tcl script or constraint
  file renders: its name (a name holding `[...]` ran as a command where Vivado's scripts set the
  project name), its tops, clock ports, testbench instance, parameter and define values, and the
  FPGA part, family, device, package and speed. No script `eval`s a value any more (`vivado_sim`
  parsed `xelab`'s arguments, `xelab_log` among them, a second time). Tool flags and simulation
  times are still written as the words given. `dc`'s `max_tluplus` was passed only when `min_tluplus` was set.
- `vivado_project` could not run: it rendered a report helper removed in 2025, and it ended in
  `start_gui`, which fails in every headless Vivado; the design's XDC sources never reached it
  (`p.type == "xdc"`, which no source type equals). It now creates and saves the project -- the
  sources and testbench, the constraints, the strategies and steps, the report hooks
  `vivado_synth` uses, and `tcl_files` -- reports it as `artifacts.project`, and opens it in the
  GUI only when `gui` is set. Its description said it synthesizes, and it listed
  `vivado_synth`'s results; it runs nothing, and reports none.
- `vivado_alt_synth` reads the design's XDC and SDC sources and `xdc_files`, like `vivado_synth`
  (one list, `vivado_synth.constraint_files`): it read only its generated clock constraints and
  ignored both. `tcl_files`, which a non-project run has no fileset for, is a settings error
  naming `vivado_synth` instead of being ignored.
- `diamond_synth` could not render its script: the template used `strategy`, `allow_dsps`,
  `allow_brams` and `fpga_part`, settings the flow had lost. `strategy` (default `Timing`) and
  `allow_dsps`/`allow_brams` (default true) are settings again, the device is `fpga.part`, and a
  run fails for a DSP or block RAM only where that resource is disallowed -- it failed every run
  using one. `diamondc` is no longer handed its own name as the script's first argument.
- `modelsim` compiled no source: its script compared `src.type` with lowercase names, which no
  source type equals. It also read an undefined `debug` and `flow`, so it did not render, and a
  VHDL source's command ran into the next one. `tests/test_source_type_comparisons.py` checks
  every comparison of a source type with text in the flows' code and templates. `vsim` no longer
  `eval`s its options, which split an SDF path with a space, and `-L` names a library of
  `lib_paths` rather than its whole `(name, path)` pair.
- `dc` sources its `hooks` at their stages -- `pre_elab`, `post_elab`, `post_link`, `finalize` --
  which it resolved and then ignored; a hook at another stage is an error naming the stages.
- `vivado_synth` is described as what it is: a project-mode run in batch (it said non-project
  mode, which is `vivado_alt_synth`).
- A `dockerized` GHDL on an Apple silicon Mac could not link: the flow added Apple's
  `-no_compact_unwind` linker flag, meant for a GHDL on the host, to the one in the Linux
  container, where GNU ld misread it (`cannot find -lgcc_s`).
- A tool run in a container got its default arguments twice (`yosys -T -Q -T -Q`): the
  container's command held them, and every run passed them again.
- `dc` failed at `current_design` on every run: its `catch` named the error variable `$err`,
  reading a variable that did not exist yet.
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
  hash, which counts every source by its path relative to the design root.
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
  cannot silently reuse one run. See *Changed* for how the design hash counts source and
  parameter paths: relative to the design root for every source.
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
  fields), joining `Design.dependencies`, which already was. `model_dump_json()` is therefore
  the same document xeda writes through `model_dump(mode="json")`; the `exclude=` of `rtl_hash`,
  `tb_hash`, `rtl_fingerprint` and `tb_fingerprint` is gone, since those are properties, not
  fields, and were never dumped.
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
  `Path`/enum/`FileResource` conversions the models declare were skipped. A design is recorded
  whole, every field, as flow settings are: a record of only the fields pydantic counts as set
  lost whatever was assigned inside a nested model (`design.tb.sources = ...`), because assigning
  a field of the testbench does not mark the design's own `tb` as set.
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
- `bsc` generated no Verilog for a `(* synthesize *)` module defined in another package: every
  source but the top file was compiled without a backend, so only the top's own package got
  Verilog, and the missing submodule was silently left out of `artifacts.verilog` -- downstream
  synthesis then failed on an undefined module. Every Bluespec source is now compiled with
  `-verilog`, and the files are collected by following bsc's `.use` files from the top.
- The Verilog files of library and imported modules were looked for by a recursive glob through
  the run directory and bsc's own library trees, along a search path read from a bsc run without
  the flow's own `-vsearch`, so the design's Verilog directories were never searched. `bsc` now
  reads its own Verilog search path for the run's actual flags and searches it directory by
  directory, as bsc does; a module the design's Verilog sources define is taken from there, and
  one found nowhere (a vendor primitive) is reported as a warning instead of being silently
  dropped. The library modules' own submodules are collected too: a design using
  `Clocks::mkReset`, `mkSyncRegister` or `mkSyncFIFOLevel` got `MakeResetA.v` or `SyncRegister.v`
  without the `SyncResetA.v` or `SyncHandshake.v` they instantiate. The modules the design's own
  Verilog instantiates are collected as well, whether library or bsc-generated, and a `-vsearch`
  given in `extra_flags` is part of the search path the flow collects from.
- In a debug run the flow passed `-cross-info`, which bsc 2026.07 removed, so `--debug` runs
  failed outright; and `debug` changed the generated hardware (it enabled `-keep-fires` and
  `-keep-inlined-boundaries`, and dropped `-remove-unused-modules`). Debug no longer changes
  bsc's flags; those are settings of their own now.
- `sched_conditions` and `haskell_runtime_flags` were accepted but never passed to bsc.
  `sched_conditions` now maps to `-sched-conditions`, `haskell_runtime_flags` to
  `+RTS ... -RTS` (default now empty; the old default was never applied). `incremental`, which
  compiled only the top file and left `-u` to find the rest, is removed (see Removed): every
  source is now compiled in order with `-u`, which recompiles only what changed.
- `bsc` compiled the testbench's Bluespec sources too, and took the *last* Bluespec source
  overall as the file defining `rtl.top` -- the testbench's file when the testbench is written in
  Bluespec. It now compiles `rtl.sources` only.
- `rtl.defines` never reached bsc as preprocessor macros (only `rtl.parameters` became `-D`
  definitions); both do now, and in `bsc_sim` the testbench's take precedence over the RTL's.
- `unspecified_to` was silently dropped unless `optimize` was set (and, for "X",
  `opt_undetermined_vals` too); it is now always passed to bsc, and the combinations bsc itself
  rejects ("X"/"Z" without `opt_undetermined_vals`; "X"/"Z" with Bluesim) are settings errors
  reported before anything runs.
- The recorded bsc version was "Compiler," -- xeda's generic version parsing did not understand
  bsc's own banner format. It now records the release, e.g. `2026.07.1`.
- A `.bh` source (which xeda classifies as `Bluespec`) failed deep inside bsc, which compiles BH
  only from `.bs` files; it is now rejected up front, naming the file to rename.
- `bobj_dir` was made absolute before a `$DESIGN_ROOT` in it was expanded, so the design root
  ended up nested inside the run directory; it is a `Path` setting now, expanded like any other.
- `Tool`'s minimum-version check compared a version with fewer components as equal to a longer
  minimum version, since the missing component was silently skipped: bsc 2026.07 satisfied a
  2026.07.1 minimum. A missing component now counts as 0; a version that could not be read at all
  is still not compared.

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
- **A design no longer counts where it is.** Every source counts by its content, type,
  `standard`, `variant`, position in source order, and path *relative to the design root*.
  A Verilog `include` searches the including file's directory first, and a constraint or script
  may source another file from its own directory, so two layouts of the same files can build
  different results; counted by content alone they were one design, and
  `--cached-dependencies` reused one's result for the other. The path is relative to the root
  outside it too
  (`../lib/defs.vh`): counted as the design file wrote it, the same header written absolutely was
  another design, and a design reloaded from its `settings.json`, which names it absolutely,
  could not reproduce its `design_hash`.
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
- **The `bsc` flows require bsc 2026.07.1 or newer**, checked when the flow starts. bsc 2026.07
  turned `-aggressive-conditions` and `-sched-conditions` on by default, stopped generalizing
  untyped `let`s, removed `-cross-info`, and made Bluesim exit with a failure status on `$fatal`.
- **`bsc` defaults now follow bsc's own** where the old flow deviated without cause, checked by
  measurement (LUTs after yosys `synth_xilinx`, and bsc's run time, on the examples, bluelight's
  Ascon and Piccolo's core): `optimize` is false (`-O` minimizes rule and `if` conditions during
  elaboration; it saved at most 3% of the LUTs, but Piccolo's core, which compiles in about 80 s
  without it, did not finish in 30 minutes with it, stuck in its PLIC); `extra_optimize_flags` is
  empty and applies whether or not `optimize` is set; `promote_warnings` is empty (it promoted
  G0009, G0010, G0005 and G0117, which failed designs such as Piccolo, with its 26 G0010 and 85
  G0117; `promote_warnings = ["G0009", "G0010", "G0117"]` restores it -- G0005 is an error
  already); `aggressive_conditions` is true (the old flow turned it off). Synthesis-oriented
  defaults stay: `unspecified_to = "X"` with `opt_undetermined_vals` (up to 4.8% fewer LUTs),
  `remove_unused_modules`, `remove_starved_rules`, `remove_dollar`, `positive_reset`.
- **`warn_flags` is replaced** by the booleans `warn_method_urgency`, `warn_action_shadowing` and
  `warn_undetermined_predicate` (all on).
- The directories `search_paths`, `verilog_search_paths`, `fdir`, `include_dirs`, `library_dirs`
  resolve relative to the design root; outputs (`bobj_dir`, `info_dir`, `verilog_out_dir`,
  `sim_dir`) relative to the run directory. `cleanup_bobjs` removes `.ba` files as well as `.bo`,
  and the Verilog modules an earlier run generated in the output directory (each `.use` file,
  which bsc writes only at the flow's request, right after the module's `.v`, and that `.v`;
  other files stay): runs of a design share the directory, so a module the design has since
  replaced by Verilog of its own was found again.
  A module both generated by bsc and defined by a design source is now an error.
- `artifacts.verilog` lists the library modules first, then the design's Verilog sources, then
  the generated modules with the top last; `results.modules` lists the hierarchy, top first.
  Library modules are copied into `verilog_out_dir`; with `positive_reset`, a
  `` `define BSV_POSITIVE_RESET `` is added to the generated and copied files, and a first file,
  `bsv_defines.v`, defines it ahead of the design's own Verilog sources, which are passed on
  unchanged.

### Removed
- `dc/templates/run_old.tcl`, which no flow rendered.
- Dependency on `click-help-colors`, replaced by `click-extra`.
- `Design.relative_path` (use `Design.source_path_as_named`), and `units.normalize_quantity`,
  `units.UNIT_ALIASES` and `units.unit_maybe_scale`; `units.check_unit_case` now takes
  `(unit, text)`.
- **`bsc`'s `gtkwave_package` setting** and the Bluetcl script behind it (GTKWave translation
  filters for enums). Design files that set it (e.g. bluelight's `xedaproject.toml`,
  `[flow.bsc] gtkwave_package = ...`) must drop it. Also removed: `warn_flags`, `incremental`.

### Added
- Tests: the fake tools (`tests/fake_tools/`: `vivado`, `quartus_sh`, `xtclsh`, `dc_shell`, and now
  `diamondc` and `vsim`) run the TCL script a flow hands them under `tclsh`, the tool's commands
  recorded, and fail on a TCL error as the tool would -- every test using a fake now checks the
  scripts its flow renders. Two opt-in layers run the real thing: `XEDA_TESTS_VIVADO=1` runs
  Vivado flows on tiny designs, and `XEDA_TESTS_DOCKER=1` runs flows `dockerized` in their
  default images.

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
- `bsc_sim`: simulates a Bluespec testbench (`tb.top`) with Bluesim or, through bsc's Verilog
  link step, Verilator, Icarus Verilog or another `-vsim` simulator (settings `simulator`,
  `sim_args`, `max_cycles` (Bluesim), `vcd`, C/C++ and link options; the design's C/C++ sources
  are linked in (`.c`, `.cc`, `.cpp`, `.cxx`, `.o`, `.a`), so imported C functions -- `import
  "BDPI"` -- work on every simulator, `elab` being on by default for the `.ba` files a Verilog
  link needs, and Verilator without `use_dpi` rejected up front; the design's Verilog sources are
  handed to a Verilog link as files, whatever their names; the `vcd` file's directory is
  created; a link path with whitespace, which bsc's link step cannot take, and more than one
  `tb.top` are rejected before anything runs). The run fails when the
  simulation exits with an error status: `$fatal` or a failing `dynamicAssert`
  (`system_verilog_tasks` is on by default so that `$fatal` survives into the Verilog).
  `$finish(n)`'s argument is a verbosity level, not a status; of Bluesim, Verilator and Icarus,
  `$error` fails only under Verilator.
- A `bsc` setting for every documented (`bsc -help`) bsc 2026.07.1 option that affects the
  output:
  scheduling/semantics incl. `let_gen`, `resource_scheduling`, `sat_solver`; code generation
  incl. `v95`, `use_dpi`, `system_verilog_tasks`, `verilog_filters`, `keep_*`, `remove_*`;
  diagnostics `show_schedule`, `sched_dot`, `show_rule_rel`, ...; messages
  `promote_warnings`/`suppress_warnings`/`demote_errors`; paths `search_paths`,
  `verilog_search_paths`, `fdir`, `info_dir`; `cpp`/`cpp_flags`; `extra_flags` for the rest. And
  `verilog_primitives` ("vivado"/"quartus" pick bsc's vendor-tuned Verilog library modules).
  With `positive_reset`, the define is prepended to library and generated files as bytes, so a
  file need not be UTF-8.
- With `cpp`, the design's macros are also given to the C preprocessor, the only one BH (`.bs`)
  sources go through; a design that defines macros but whose Bluespec sources are all BH is
  warned about.
- `examples/bluespec/`: gcd (BSV, multi-package), fir (BSV, sized by macros), collatz (BH), crc32
  (BSV top with a BH package), verilog_import (`import "BVI"`), all self-checking with an
  `XEDA_INJECT_BUG` hook.
- Tests: `tests/test_bsc.py` (every setting checked against the flag record the installed bsc
  builds from it), `tests/test_bsc_examples.py` (every example: its Verilog elaborates, lints
  and synthesizes; its testbench passes on every simulator it supports and catches its injected
  bug), `tests/test_bsc_external.py` (opt-in `XEDA_TESTS_EXTERNAL=1`: bsc-contrib's
  AXI4/SequenceRules/COBS testbenches against their golden output, bluelight's Ascon core,
  Piccolo's core, at pinned commits), `tests/test_tool_version.py`. CI installs bsc 2026.07.1 and
  runs the bsc tests (the external ones on one Python version).

## [v0.1.0-alpha.11] - 2022-04-16


[Unreleased]: https://github.com/XedaHQ/xeda/compare/v0.1.0-alpha.1...HEAD
[v0.1.0-alpha.11]: https://github.com/XedaHQ/xeda/releases/tag/v0.1.0-alpha.11
