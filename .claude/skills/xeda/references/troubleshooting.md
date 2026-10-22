# Troubleshooting Xeda runs

## Triage order

1. `error.type` and `error.message` of the `--json` document. Usage errors (exit 2) are
   `BadParameter` and `UsageError`.
2. `FlowFailed`: the cause is `results.error` (the same as `error` in `results_json`; branch on
   its stable `code`). `FlowDependencyFailure`: open the failed producer's `results.json` (the message names it) and
   start there. `nodes` lists every run directory; a node planned but never entered shows
   `"state": "not run"`.
   A run whose checks rejected the results is reported again, unchanged, on the next launch with
   the same inputs and settings: its node state is `rejected again`. Examples: a failed cocotb
   test, an end record that fails, negative hold slack in Vivado with `fail_timing`, negative slack
   in Quartus, ISE or Diamond. The first launch gives `failed`. `--rebuild-all` runs it again. A
   consumer of a failed, rejected or blocked producer is `blocked`. Tool failures run again at
   every launch. They include a tool that exits with an error, a Vivado setup-slack miss with
   `fail_timing` (the Xeda script stops the tool) and a `nextpnr` timing miss without
   `timing_allow_fail` (nextpnr stops itself).
3. `settings.json` in `run_path`: `flow_settings` is what you asked for, `effective_flow_settings`
   is what the flow made of it.
4. The tool log and the generated scripts in `run_path`. Add `--debug` for verbose logs and a
   traceback.

## Messages and fixes

### Settings

| Message | Fix |
| --- | --- |
| `Extra inputs are not permitted: <name> (extra_forbidden)` | No such setting on this flow. A `did you mean` hint names the setting with the right case (settings are case-sensitive; a group leaf is `group.leaf`). `xeda list-settings <flow> --json \| jq -r '.fields[].name'`. A producer's setting goes under `-s flows.<producer>.<name>=...`. |
| `` <flow> runs Vivado in non-project mode, which has no runs to use `<setting>` on: it is a setting of vivado_synth `` | A flow refuses a setting that belongs to a related flow and names that flow. Move the setting there (`flows.vivado_synth.<setting>`). |
| `` `-s flows.<name>.*` names no flow of this run (<flow>) `` | Name a flow of the run (the requested flow or a producer). The error suggests the right one. |
| `` `<name>` was removed: use ... `` | Any removed flow, setting, option or key fails this way. Do what the message says; when nothing replaces the setting, it says so (`` `no_console` was removed: no flow read it; delete it ``). For example, use `timeout` (on the simulators) for `timeout_seconds`. For `open_xc7`, use `fpga_pack` to build and `openfpgaloader` to program: move its placement settings to `flows.nextpnr`, its synthesis settings to `flows.yosys_fpga` and its pin files to `rtl.sources`. |
| `` `yes` is text, not a boolean: write `true` `` | A boolean takes `true` or `false` only. |
| `` `10` needs a unit, such as `10 ns`: a bare number does not mean anything `` | A setting of a kind (`time`, `frequency`, `capacitance`, ...) needs a unit: `clock.period: 10 ns`, `-s clock.period=10ns`. `list-settings` shows the kind as the `type`. See "Units" in `design-file.md`. |
| `` `5 MHz` is a frequency, not a time `` | The unit is of another kind. Use a unit of the kind that the message names. |
| `` unit `ff` in `3.9 ff`: units are case-sensitive, did you mean `fF`? `` | Write the unit as the message says (`MHz`, not `mhz`). |
| `` unknown unit `nanoseconds` in `10 nanoseconds`: a time takes one of fs, ps, ns, us, µs, ms, s, min, h `` | Use one of the units listed. A spelled-out name is refused. |
| `` cannot read `fast` as a time: write one number and one unit, such as `10 ns` `` | The text is not one number and one unit (a word, `5 ns * 2`, `1_000 ns`). |
| `must be greater than 0 ns, got -1 ns` | The value is outside the limit of the setting. |
| `Input should be a valid number, unable to parse string as a number` | The setting still takes a plain number (`timeout` in s, `hold_detour_max` in ns; `freq` of `openfpgaloader` is an integer in Hz). Remove the unit. |
| a type error on a list or text setting | A setting takes its declared type only. `list-settings` shows `type` and `enum`. A list given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`); `[a, b]` is refused. |
| `` `true` is a switch, and `bitstream` names a file `` | Give a name or location, or leave it unset. |

### Design

| Message | Fix |
| --- | --- |
| `rtl.sources: Value error, source file does not exist: <path>` | Relative paths resolve against the design file's directory. A file a generator writes is `{path: "gen/top.v"}`. |
| `rtl.<key>: Extra inputs are not permitted` | Unknown design key. |
| `<key>: Extra inputs are not permitted` at the root, often with `rtl.sources: Field required` | The flat form (`sources`, `top`, `clock` at the root) beside an `rtl:` section. Write everything under `rtl:`. |
| `` `.json` names several kinds of file ... give its type `` | Add `type` (`{file: "top.json", type: JsonNetlist}`). |
| `` `standard` was removed from a source `` | Use `language.vhdl.standard`. |
| `<flow> reads none of the design's sources: ...` (`NoReadableSource`) | The design lists no source the flow reads. Pick a flow for the design's languages, or give a source its `type`. |
| `<flow> cannot read the design's Vhdl source(s) ...; it reads ...` | Same: `verilator` reads no VHDL; `ghdl_sim`, `nvc` and `ghdl_synth` read only VHDL; `bsc` reads Bluespec and Verilog. A simulator checks `tb.sources` too. |
| `<flow> does not read the design's Bluespec sources, and more than one flow converts them: ...` | Two converters fit. Choose one with a chain (`bsc+<flow>`) or a binding (`flows.<flow>.inputs.design: bsc`). |
| `... ; simulate a Bluespec testbench with bsc_sim` | A Verilog simulator cannot read a Bluespec `tb`. Run `bsc_sim`. |
| `` the design defines `BSV_POSITIVE_RESET`, which bsc defines itself when `positive_reset` is true: remove the define, or set `positive_reset: false` `` | `bsc` and `bsc_sim` refuse a design that defines the macro while `positive_reset` is on. Remove the define, or set `positive_reset: false`. |
| `` <flow> needs `fpga`, the target FPGA device ... `` | Give `flows.<flow>.fpga.part` or `-s fpga.part=<part>` (or a `board`). A shared setting travels only along the edges of the run's graph. In `vivado_synth+openfpgaloader` the part under `flows.yosys_fpga` reaches neither node (`yosys_fpga is not part of this run`). Only `nextpnr`, `fpga_pack` and `openfpgaloader` take `board`. |
| `<flow> cannot run cocotb tests; use one of: ghdl_sim, nvc, verilator` | Pick a cocotb simulator, or drop the `.py` testbench. |

### Chains

A malformed chain is `BadParameter` (exit 2) before anything runs.

| Message | Fix |
| --- | --- |
| `` Flow `a` has no compatible output for a required input of `b` `` / `` ... takes no required input `` | The pair does not fit, and the default producers of `b` do not reach `a`. `Did you mean ...?` names a valid chain when another output of `a` fixes it. `bsc` makes the `design` that the flows which read Verilog take (`bsc+yosys`). `bsc_sim` has no chain relation: run it alone. |
| `` Flow `openfpgaloader` programs a device and can only end a chain `` | Put it last. |
| `` Flow `a` appears more than once `` | A chain is a path; each flow once. |
| `` Input `nextpnr.netlist` takes JsonNetlist, but `yosys_fpga.netlist_edif` makes Edif `` | A link that passes through flows binds every input of them that the producer feeds, and `a.output` must fit each. Use the output the `Did you mean ...?` names, or none. |
| `` Flow `x` appears more than once ... The link `a+b` passes through `x` `` | A flow that a link passes through cannot appear elsewhere in the chain. Remove it, or name the flows between `a` and `b` yourself. |
| `` The chain link `a+b` passes through `x`, which the source of `b.input` takes out of the plan `` | A planning error (exit 1). A source or binding for an input of a later flow removes the flow `x` that the link passes through. Drop that source or binding, or write the chain without that link. |
| a binding error naming the consumer, input and where it was written | `unknown input`, `unknown producer` or `declares no inputs`. A binding names a flow's output (`yosys_fpga.netlist`), never a file. A chain and a command-line or API binding of one input conflict even when equal: bind it in one place. |
| `` vivado_power: `checkpoint` must come from the producer of the `netlist_timing` ... `` | Related inputs come from one producer. Add the `-s flows.<node>.inputs.<input>=<producer>.<output>` bindings the message lists. |

`xeda dse` refuses chains and bindings; run them with `xeda run`. `--remote` takes them.

### Run root, run directories and deliveries

| Message or type | Fix |
| --- | --- |
| `RunRootError: <dir> holds files and is not a run root xeda created (it has no .xeda-run-root)` | Name a new or empty directory with `--run-root`. If it holds only earlier Xeda runs, delete them or create `<dir>/.xeda-run-root`; everything in a marked run root is Xeda's. A directory named `xeda_run` in the start directory is adopted without the marker. |
| `RunDirectoryError` | A run directory would lie outside the run root, or a write would go through a link Xeda does not control. Read the message. |
| `DeliveryError` | A setting or `--outputs-to` named a location Xeda cannot deliver to: the run wrote no such file; the destination is an input of the launch, a directory, or inside a run root; or two outputs name one destination (the message names both settings). Only a deliverable setting (`"writes": "deliverable"`) given a location is copied out; a working setting stays a bare name in the run directory. |
| `OutputExistsError` | The destination holds a file that is not Xeda's own unchanged earlier delivery. Rerun with `--overwrite-outputs`. At a terminal you can answer the prompt; under `--json` only the flag works. |
| `Invalid value for '--outputs-to': Directory '<x>' is a file.` | `--outputs-to` takes a directory. |

### Tools

- `ExecutableNotFound`: `Executable 'ghdl' was not found in PATH. flow:ghdl_sim, tool:GhdlTool`
  (`--debug` appends `, PATH:...`). Install the tool, or run it from a container with `-s dockerized=true`. `results.json`'s `tools`
  lists the executables Xeda found and their versions.
- `nonzero_exit` (`timeout` for a time limit): `Command '...' exited with code 1!` in the failed node's `results.error`. If
  the error reaches the CLI itself, the message starts with `Flow <flow> failed: `. The generated
  script and the tool's log are in `run_path`.
- `reported_failure`: a node's `results.json` says so when its reports or checks failed with no
  exception and no tool exit status, for example a tool that exited 0 and left a negative verdict in its reports. The
  top-level document says `FlowFailed`; consumers quote the message
  (`producer yosys_fpga failed (reported_failure): ...`, with `producer_failed` as the consumer's
  own code). A `missing_report` is the same case when the flow asked for a report that this run did
  not write. A rejected run (a failed cocotb test, negative hold slack in
  Vivado, negative slack in Quartus) is reported again on an unchanged launch (node state
  `rejected again`); `--rebuild-all` runs it again. A tool failure, such as a Vivado setup-slack
  miss or a `nextpnr` timing miss, runs again.
- `missing_output`: a declared output is absent, unreadable, outside the run directory or not
  written by this run. `nextpnr` reports its own missing configuration as `reported_failure`, naming
  the setting and path.

### Remote runs

`--remote` failures have their own types. The message names the step and the cause. Test the
destination with `ssh HOST true` first; `xeda remote check HOST --json` is the first diagnostic
(connection, environment, disk, tools on the login `PATH`); add `--debug` for the commands and the
output of the host. A login profile that prints text does no harm (up to 1 MiB before the agent).

| `error.type` | Fix |
| --- | --- |
| `RemoteConnectionError` | OpenSSH could not connect or authenticate (connection refused, No route to host, Could not resolve hostname, Permission denied), `ssh -G` could not resolve the destination, the connection ended or closed during a step, or `ssh` is not on `PATH`. The message has the OpenSSH text and exit status. Fix the destination, the network or `~/.ssh/config`. A launch with no terminal cannot answer a prompt: it needs a key in an agent. Never handle a secret; ask the user to load a key (`ssh-add`) or to run `! ssh HOST true`. |
| `RemoteProvisionError` | Installing Xeda on the host failed. The message starts with the code: `not-linux`, `unsupported-platform` (only x86_64 and aarch64, glibc or musl; also when the C library cannot be identified), `no-downloader` (no `curl` or `wget`), `no-sha256`, `download-failed` (check internet access, or set a proxy in the profile `env`), `checksum-mismatch` (the `uv` archive differs from its pinned SHA-256; a proxy may have changed it), `python-failed` (`uv` cannot install the Python, or the profile `python` is not an executable file), `env-failed` (the message has the output of `uv`; also when the remote root is not writable, or a package has no Linux release), `no-space` (under 50 MiB free; free space or set `root`), `lock-timeout` (remove the lock directory the message names if its process is gone), `bad-request` (report it). |
| `RemoteDependencyError` | A package cannot be installed on the host as it is here: an editable install, a local directory or a direct URL, or a package in `tb.python_packages` that is not installed locally, or the local Python is not CPython. Install the package from a package index. |
| `RemoteIncompatible` | The host planned other flow identities than the local side did. Compare `--dry-run --json` with and without `--remote`; report a difference with no visible cause. |
| `RemoteCanceled` | Ctrl-C, `xeda remote cancel` or a lost connection stopped the launch. If processes survived SIGKILL the message counts them and they may still run on the host. Run the command again to start over. |
| `RemoteInterrupted` | Ctrl-C came after the launch had finished, while Xeda fetched or delivered. The results are on the host, or in the mirror if it came during `--outputs-to` (the delivered files can be incomplete): run the command again. |
| `RemoteDetached` | A `--keep-running` launch keeps running; the document has `launch.id`. Use `xeda remote attach HOST ID` or `xeda remote cancel HOST ID` with the `--run-root` of the run (attach needs the record in `<run root>/@<host>/.launches/`). |
| `RemoteFetchError` | A file did not come back whole, or a declared output is missing. Run the command again: the finished run is reused and only the fetch repeats. |
| `RemoteConfigError` | `remotes.yaml` or one of its entries is invalid (the message names the host and field), or the `--remote` text is empty, starts with `-` or has a space, or the remote root has a character that cannot go in a command line (`"`, `\`, `$`, a backquote, `!`): set `root` in the profile to another path. |
| `RemoteProtocolError` | The agent on the host did not start or sent data that Xeda cannot read: the stream ended before the agent marker, or more than 1 MiB of text came before it. Ordinary login text is skipped. The message has the output of the host. |
| `RemoteError` | A plain remote failure; the message says what to do: no `bash` on the host (install it), a programmer flow in the chain (`openfpgaloader`; run it locally with a binding to the fetched file), or native Windows (use WSL). |

## Simulation verdicts

A simulation passes only on recognized evidence that it ended, never on exit status alone.
`results.json` holds `sim.evidence` (the normalized record), `sim.ended_by`, `sim.time`,
`sim.time_unit`, `sim.errors` and `sim.warnings`. `error.message` states the cause and the fix.

- A silent exit 0, a missing or malformed end record, an unknown end, or a drained event queue
  fails. A testbench that ends with `wait;` needs `std.env.finish` or `$finish`, or a `stop_time`.
  Fix: end the testbench with `$finish` (VHDL `std.env.finish`).
- `$finish`, VHDL `std.env.finish` and `std.env.stop` are accepted. Verilog `$stop` is an
  error-rank event and fails at the default threshold. A nonzero exit always fails.
- cocotb: at least one test must run and none may fail; an all-skipped run fails. `cocotb.tests: 0`
  in `results.json` means no test was collected: check `tb.sources`.
- Every simulator has `timeout` (per subprocess invocation that contains simulation) and
  `fail_severity` (`warning`, `error`, `failure`, `fatal`; default `error`; `failure` and `fatal`
  rank alike).
- A requested `stop_time` needs a matching observed time. GHDL and nvc report the actual stop time;
  a sparse clock that misses it fails. Bluesim `max_cycles` needs the exact measured cycle count.
- `verilator` refuses `stop_time` (`verilator cannot stop at a simulated time`): end the
  testbench with `$finish`, or set `timeout`. A C++ source that holds `main` needs `main: false`;
  without it, a C++ source is a DPI helper. `yosys_sim` also refuses `stop_time`.
- `verilator`'s `optimize` is `true` (`-O3`), `false` (no `-O`) or a level `0`, `1`, `2`, `3`: write
  `2`, not `-O2` (refused, naming the level). Any other value is refused.
- `vivado_sim` and `vivado_postsynth_sim` record `saif` (and the latter `timing_sim`, so a
  `vivado_power` request too) for the instance `tb.uut`. Without `tb.uut` planning fails: set
  `tb.uut` to the instance name of the unit under test in the testbench. `ghdl_sim`, `modelsim`,
  `vivado_sim` and `vivado_postsynth_sim` refuse an SDF file when neither `sdf.root` nor `tb.uut`
  is set. `prerun_time` opens the SAIF after the pre-run (the VCD
  starts at time 0).
- `vcs` has no `ucli` setting (Xeda always runs VCS with `-ucli`); `ucli_script` sets the
  script that drives `simv`.
- `nvc` needs a C++ compiler for non-cocotb runs. It has no `exit_severity`: use
  `fail_severity`. `optimization_flags` is a setting of `bsc_sim`, `ghdl_sim`, `vivado_sim` and
  `vivado_postsynth_sim` only; `nvc` has `optimization_level`.
- ModelSim exits 0 whatever the testbench says; Xeda judges its owned batch log and
  `TESTSTATUS`. Vivado `xsim` needs source-preserving `elab_debug`; turning it off is refused.
- VCS and Questa adapters judge native evidence and fail closed on evidence they do not recognize.
  They are unverified on licensed tools. `bsc_sim` refuses `cvc`, `cver`, `isim`, `ncverilog` and `veriwell`
  before compilation.

### Waveform settings

`vcd`, `fst`, `vcdgz`, `saif` and the other waveform settings take `true` (the conventional file,
`dump.<format>`), `false`, a file name, or a location. `-s vcd=true` is `vcd: true`. `yes` and `on`
are refused.

| Message | Fix |
| --- | --- |
| `` `wave` writes GHW data, and its name x.vcd ends in .vcd: give the name to `vcd` `` | A name that ends in another format's extension goes to that format's setting. |
| `` `vcd` writes VCD data, and its name x.vcd.gz ends in .vcd.gz: give the name to `vcdgz` `` | `ghdl_sim` writes compressed VCD with `vcdgz`. `vcd` is plain VCD. |
| `ghdl_sim writes one waveform, and vcd, fst are set: keep one of them` | One format at a time on `ghdl_sim`, `nvc`, `vcs` and `verilator`. |
| `` `vcd` names a waveform file, but without cocotb xeda writes no waveform ... Set `vcd: true` `` | `verilator` without cocotb: `vcd`, `fst` or `saif` set to `true` builds the model with that trace format, and the testbench calls `$dumpfile("dump.<format>")` and `$dumpvars`. A run that wrote no such file fails. |
| `` vcs cannot write a waveform from `vcd`: ... set `vpd`, `fsdb` or `evcd` `` | `to_vcd` converts FSDB or VPD to VCD. |
| `` yosys_sim cannot write a waveform from `vcd` `` | The design's C++ driver writes the waveform. |

No simulator has a `waveform` setting: use `vcd`.
`nvc` has `vcd` and `fst` (it refuses `wave` and `wave_format`). `ghdl_sim` has `vcd`, `vcdgz`,
`fst` and `wave` (GHW). `modelsim` writes no dump unless `vcd` is set. `vivado_sim`'s `saif` is
`outputs/<design>.saif`.

## Open-source FPGA flows

A Xilinx 7-series build fails before or during `nextpnr`:

- `nextpnr-himbaechel needs the full part in fpga.part`: give `fpga.part` with package, pins and speed grade (`xc7a100tcsg324-1`),
  as the Project X-Ray database lists it.
- A missing Himbaechel share tree, generator, `bbasm` or Project X-Ray database names the path
  searched: put openXC7 1.0's `bin` first on `PATH` (another `nextpnr-himbaechel` without the
  openXC7 share tree cannot serve), or set `chipdb` or `prjxray_db`.
- The first build seems to hang: it generates the chip database (about a minute, gigabytes of
  memory), once per run root.
- `nextpnr: timing constraints are not met: Max frequency ... (FAIL at ...)`: relax the clock, or set
  `timing_allow_fail`.
- `nextpnr constraint error` names the original constraint file and line.
- A `nonzero_exit` of `nextpnr` ends with `nextpnr error: ...`, the first error lines of this
  run's log; more are in `nextpnr.log`.
- `openfpgaloader`: the loader can exit 0 after a failure. Xeda reads `openfpgaloader.log` and
  fails with `reported_failure`, quoting the lines. An ID error means the bitstream is for another
  device than the one on the cable. With several boards of one kind connected, the flow chooses
  the probe when it can (it logs the choice). Otherwise name the probe with `usb_serial_num`.
  To list the serial numbers, run `openFPGALoader --scan-usb`.
  `openFPGALoader has N probes on the cable ...` means no probe has an FPGA that matches the part,
  several do, or one does beside a probe that could not be read. The message lists the probes, each
  with the `usb_serial_num` that names it. `Can't program SPI flash: missing device-package
  information` means the loader's list gives the board no part: set `cable` as well. A Xilinx
  `write_flash` whose log lacks the finished write (`Writing: [...] 100.00%` then `Done`, or
  `Writing: Done` with quiet bars) did not finish it: a locked flash can stop the loader before it
  writes. `verify` does not report that.

A 7-series bitstream can compute the wrong result with timing met. Some openXC7 `nextpnr-himbaechel`
builds write wrong bits with no error: the 1.0 release and `1.0.0-41-g3e5c2cdd` have none of the
fixes. Known defects: an inferred `DSP48E1` multiply ignores its A operand; an initialized `RAM32M`
or `RAM64M` reads 0; a falling-edge shift register shifts on the rising edge; a block RAM with an
initial or reset value on its output register; an `ODDR` on the T input of a tri-state; an `MMCME2`
or `PLLE2`; a `TMDS_33` input, `LVCMOS33` `DRIVE 16` or an `IDDR`; a memory of 64K x 1 or deeper in
cascaded `RAMB36E1` pairs; a high-performance bank pad that an `ODDR` or an `OSERDESE2` drives. A
timing or utilization report cannot show them. Use a build of `main` at `26f5e17a5`
(`1.0.0-75-g26f5e17a`, from `nextpnr-himbaechel --version`) or later; Xeda does not check the build.
`nextpnr` also ignores `set_property PULLUP true` without a warning: write `set_property PULLTYPE
PULLUP`.

## Reading numbers

- `clock_frequency` is the frequency constrained. `Fmax` is the frequency achieved.
- `Fmax` is per tool: `nextpnr` and Vivado time the same netlist differently. Compare `Fmax`
  only between runs of one tool.
- Prefer the canonical keys `Fmax`, `lut` and `ff`; keys that start with `_` are internal.
- `xeda list-results <flow> --json` explains every key. When `documented` is `false`, the keys were
  detected from the flow's source: read an actual `results.json`. A flow can report nothing beyond
  the common keys.
- Flows that enforce timing fail on negative slack by default. Check `wns` and `whs`, and whether
  `fail_timing` (Vivado) or `timing_allow_fail` (`nextpnr`) was turned off.
- `lut` is per toolchain and stage; `LUT:STAGE` and `LUT:METHOD` say which. For 7-series, `nextpnr`
  counts distinct occupied LUT locations in its placement, and `yosys_fpga` estimates from mapped
  primitives (`LUT:LOGIC`, `LUT:RAM`, `LUT:SRL`). Neither is certified comparable with Vivado's
  count: compare one toolchain with itself. `fabric` names the die whose `available` totals are
  shown (an `xc7a35t` is routed as an `xc7a50t`).
- `nextpnr` parses timing and utilization from the JSON report named by its `report` setting. Its
  `lut`, `ff`, `bram`, `dsp` and `io` are given for ECP5 and Xilinx 7-series; other families get
  raw bel-type counts (`ICESTORM_LC`, `OXIDE_COMB`) plus timing.
- `nextpnr`'s `wns` is derived: the difference between the constrained and achieved clock periods.
  With several constrained clock domains, `Fmax` is the lowest achieved frequency and `wns` is the
  least-slack domain's (they may differ), and the scalar `clock_frequency` and `clock_period` are
  omitted. `clock_domains` gives the count; per-domain detail is in `_fmax`.

## Stale or surprising reuse

A run reuses `xeda_run/<design>/<flow>/` (`<design>/<target>/<flow>/` with a target), which keeps
incremental tool state. A flow that runs logs `Running <flow>: <reason>`; an up-to-date flow logs
that and shows its recorded results. If a result looks stale or wrong, run `xeda run ... --dry-run
--json` to see each node as `fresh`, `stale` (with the reason), `unknown`, `rejected again` or
`blocked`, then use `--rebuild-all` or `--clean` (see "Rebuilds" in `SKILL.md`). A failure that is
reported again although you changed a data file the testbench reads means that the file is not
declared: declare it as a source, or use `--rebuild-all`.
