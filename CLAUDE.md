# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Xeda is a cross-EDA automation platform: it drives simulation, synthesis, and implementation flows for
commercial and open-source EDA tools (Vivado, Quartus, Diamond, ISE, DC, VCS, ModelSim, GHDL, NVC,
Yosys, nextpnr, OpenROAD, Verilator, Bluespec, ...) from a single declarative design description.
The package lives in `src/xeda`; the console entry point is `xeda = "xeda.cli:cli"`.

## Commands

Development install (repo uses `uv` with `uv.lock`, but plain pip works too):

```bash
uv sync                              # or: python -m pip install -U --editable .
```

Tests, lint, format, type-check:

```bash
pytest tests/ -n auto                # full test suite, one pytest-xdist worker per CPU
pytest tests/                        # the same, serially
pytest tests/test_vivado.py::test_vivado_synth_py -s -v   # single test
tox                                  # CI matrix: py311-py314 + mypy + black + ruff
tox -e mypy                          # mypy --install-types --non-interactive src - currently clean
tox -e black                         # black --check --diff src tests (line-length 100) - clean
ruff check src tests                 # .ruff.toml, line-length 120
```

**The suite is safe to run in parallel** (`pytest-xdist`, in the `dev` group; tox and CI use
`-n auto`), with outcomes identical to a serial run (checked on the full suite with the real
tools: 3767 tests, serial 15.6 min, `-n auto` on 10 cores 3.2 min). Each test works under
`tmp_path`, so workers share nothing but read-only files (the examples, `tests/resources`, the
fake tools) and the opt-in layers' checkout `xeda_run/`. The exception that needed a fix is the
external-repository cache (`XEDA_TESTS_EXTERNAL_CACHE`): every worker asks for the same pinned
checkout, so `test_bsc_external._fetch_pinned_commit` holds an `flock` beside it while it fetches
(`tests/test_external_cache.py`). On platforms without `fcntl`, cache access skips in xdist
workers; run the external tests serially there. Keep it so: a test must not write outside
`tmp_path`, leave a process-wide change (`chdir`, `environ`, a registered flow) behind, or take a fixed name, and a
session-scoped fixture runs once per worker, not once per run. `addopts` deliberately has no
`-n`: it would start workers for `pytest tests/test_x.py::test_y`. Under `-n`, the conftest
checkout guard still fires (per worker, at its teardown, on whichever test ran last there).

`jsonschema` is a test-only dependency (in the `dev` group and in tox), used to check that the
published design schema agrees with the loader.

`mypy src`, `black --check src tests` and the Pyflakes rules (`ruff check --select F src tests`, the
`tox -e ruff` env) all pass; keep them that way. The full `ruff check` ruleset reports many
pre-existing findings (mostly `UP006`/`UP007` PEP-585/604 annotations and `RUF012`) and is not
enforced. Don't mass-fix those; keep new code clean.

Most tests use `tests/fake_tools/`, but some end-to-end tests drive genuinely installed tools
(`test_ghdl.py`, `test_nvc.py`, `test_verilator.py`, `test_yosys.py`, `test_openroad.py`'s yosys
synthesis, `test_bsc.py`'s `bsc`/`bsc_sim` flows, the GHDL half of `test_remote_run.py`, parts of
`test_cli_structured_output.py`, and `test_cocotb.py`'s runs of every cocotb simulator).
Those **skip** when the tool is missing or installed-but-broken, via the probes in
`tests/tool_utils.py` (`require_ghdl()`, `require_yosys_ghdl_plugin()`, `require_bsc()`,
`require_bluesim()`, ...). Setting `XEDA_TESTS_REQUIRE_TOOLS=1` turns those skips into failures;
CI sets it, so a tool vanishing from CI cannot look like a pass -- CI installs bsc 2026.07.1 from
its official release tarball for exactly this reason. `test_remote_run.py` and `test_dse_run.py` run
`xeda run --remote` and `xeda dse` end to end on the fake Vivado; the remote one replaces only
the transport (a filesystem-backed fabric `Connection`, execnet's `popen` gateway for `ssh=`),
so no SSH server is needed. tox passes
`GHDL_PREFIX` through: on macOS the OSS CAD Suite yosys GHDL plugin cannot find `std` without it
(the suite's `ghdl` wrapper sets it, its `yosys` wrapper does not). After sourcing the suite's
`environment` for a local tox run, drop its `py3bin/` from `PATH`: it holds a bundled
`python3.11` that tox would otherwise build `py311` on, and that venv cannot start. Tests that exercise
cocotb-based example designs need `pip install -r examples/requirements.txt`.

Running flows manually:

```bash
xeda list-flows                      # all registered flows
xeda list-settings vivado_synth      # settings schema for a flow
xeda list-results vivado_synth       # result keys a flow writes to results.json
xeda design-schema                   # JSON Schema of a design file
xeda list-boards / list-platforms / list-optimizers
xeda run vivado_synth examples/vhdl/sqrt/sqrt.toml -s clock.period=5.0 impl.strategy=Debug
xeda dse vivado_synth --design <file>  # parallel design-space exploration (Fmax search)
xeda scrub <flow> <design_name>      # remove previous run dirs
```

Flow runs land in the **run root**, `./xeda_run/` (configurable via `--run-root` / `XEDA_RUN_ROOT`,
API `run_root`); a **run directory** is one flow's. The exact layout depends on `--hashed-run-dirs`:

| options | path |
| --- | --- |
| *(default)* | `<design>/<flow>/` |
| `--hashed-run-dirs` | `<design>/<flow>_<16-char settings hash>/` |

A dependency's run directory is a **sibling** of the flow that launched it, in the same layout,
never nested under it. `--hashed-run-dirs` names a directory by the flow's *input settings* only,
so editing the design never moves it. The old `<design>_<design_hash>/` layer (dropped with
`--no-incremental`) is gone; delete such directories by hand. Xeda always reuses a flow's
directory across runs unless `--clean` empties it first (see "Caching and run directories"
below). Each run dir gets `settings.json`, `results.json` and `trace.json`, plus `reports/`,
`outputs/`, `checkpoints/`. Every run directory lies under the run root -- xeda created and marked
it (`.xeda-run-root`, `.gitignore`, `CACHEDIR.TAG`); keep nothing of yours there.

A run directory is `<run root>/<design>/<flow>` (or `<flow>_<hash>`) and nothing else:
`get_flow_run_path` refuses a design name that is not a name (`design.DESIGN_NAME`) and a directory
that leads out of the run root through a symbolic link; one that is itself a link resolving inside
the run root is used. **What a run wrote is told by identity, never by a clock** (`xeda/run_dir.py`):
the launcher snapshots every file and directory under the run directory right before `run()`
(`Flow.start_run`, `run_dir.OutputSnapshot`, keyed by device and inode, so every name of an
earlier run's file -- through a link, in another letter case -- finds that file's state; a flow
built directly takes it at construction). `Flow.wrote_output` counts an output as the run's own
only if its state -- device, inode, size, mtime, ctime (`run_dir.record_output_state`) -- differs
from the snapshot's, or the snapshot never saw it; one under a directory the snapshot could not
read, or outside the run directory, has no known prior state, which is never proof. A failed run's
`results.json` lists only the artifacts it wrote (`artifacts.drop_unwritten_artifacts`, from
`_execute`), and a failed `--remote` run keeps only what the remote vouches its run wrote, judged
on the remote's own file system (`remote_runner`'s second message). A tool may leave symbolic
links in its run directory: before the run, a working location whose own name is a link leading
out of the run directory is removed as a link, never what it points to, and one reached through
such a link is refused, naming it (`default_runner._free_working_locations`,
`RunDirectory.inside`); a delivery never expands a link to a directory outside the run directory,
nor copies from a link that leads nowhere (`deliver._check_link`). xeda never writes through a
link: a file goes through `utils.replacing_file`/`replacing_copy` (complete, then renamed over the
target) at the path `RunDirectory.writable` located.

### Machine-readable CLI (for agents and scripts)

`--json` always means "a parseable result on stdout". Query commands (`list-flows`,
`list-settings`, `list-results`, `design-schema`, `list-boards`, `list-platforms`,
`list-optimizers`) take `--format {table,json,jsonl,yaml}` with `--json` as shorthand.
Executional commands (`run`, `dse`, `scrub`) take a plain `--json` flag, which routes tool output,
logs and result tables to **stderr** so stdout carries only the JSON document.

`xeda/introspect.py` is the single source of truth behind all of it (`flows_info`,
`settings_info`, `results_info`, `design_schema`, `boards_info`, `platforms_info`,
`optimizers_info`), returning plain JSON-serializable data. Use it rather than re-deriving
metadata; the CLI, the docs and the agent skill all read from it.

`design_schema()` emits the **input** syntax of a design file, not the model's own schema: it
adds the flat top-level form (`sources`/`top`/`clock` at the root, folded into `rtl` by
`Design.process_compatibility`), `test`/`tests` as aliases for `tb`, field names alongside
aliases (`language`/`hdl`), the shorthands validators accept (`tb.top = "tb"`,
`tb.cocotb = true`), and drops `additionalProperties: false` because files may use dotted-key
shorthand (`clock.port`) that is expanded before validation. `design_schema(input_syntax=False)`
returns the model schema. `tests/test_design_schema.py` validates every example design against
both the schema and `Design.from_file`, so the two cannot drift. The emitted document is **draft
2020-12** (`$defs`, `prefixItems`); `$schema` is set from `introspect.JSON_SCHEMA_DIALECT` to the
dialect pydantic actually produced, never stamped independently, so a validator cannot be pointed
at the wrong draft.

Under `--json`, tool output is redirected via `proc_utils.set_tool_output(sys.stderr)` - which also
feeds `Popen(stdout=...)`, because a child process with `stdout=None` inherits fd 1 directly and
would otherwise corrupt the JSON. Rich output moves with `console.redirect_console(sys.stderr)`.

**Any code that spawns a subprocess outside `run_process` must honor
`proc_utils.tool_output_redirect()`** (it returns `None` unless output has been redirected, so
normal runs are unaffected). `Design.Generator.run_cmd` and the remote runner's `RemoteLogger`
both do; a new one that does not will corrupt `--json` output.

Every failure path must still emit a JSON document. That includes argument errors:
`XedaHelpGroup.main` runs click with `standalone_mode=False` when the invocation asked for
machine-readable output, so a `UsageError` becomes `{"success": false, "error": {...}}` on stdout
instead of a bare exit 2. Error documents carry `error.type` (the exception class name) and
`error.message`.

Flow names are resolved by `FlowChoice.convert` through `get_flow_class`, so every name the
resolver accepts works on the command line (canonical, CamelCase class name, aliases, dashes) and
an unknown name gets close-match suggestions. It returns the *canonical* name, so downstream code
never re-normalizes.

Note: the repository working tree accumulates untracked scratch output (`xeda_run/`, `sky130*/`,
`asap7/`, netlists, notebooks). Don't treat those as part of the source.

## Architecture

Four orthogonal abstractions, deliberately decoupled:

- **`Design`** (`design.py`) - *what* to build. Loaded from TOML/YAML/JSON (`Design.from_file`), with
  `rtl` (`RtlSettings`) and `tb` (`TbSettings`) sections, both subclasses of `DVSettings`. Sources become
  `DesignSource`/`FileResource` objects that carry a content hash; `design.rtl_hash` / `design.tb_hash`
  feed the run-directory hashing. Designs can also be fetched from a `GitReference` or produced by a
  `Generator` (e.g. `ChiselGenerator`).
- **`Flow`** (`flow/flow.py`) - *how* to build. Abstract; concrete flows live in `flows/<tool>/`.
- **`Tool`** (`tool.py`) - an executable, runnable natively, in Docker (`Docker` model), or remotely.
- **`FlowLauncher`/`FlowRunner`** (`flow_runner/default_runner.py`) - orchestrates instantiation,
  dependency resolution, run-dir management, caching, and result reporting.

`xedaproject.py` handles multi-design project files (`xedaproject.toml`), including top-level `flows`
settings that get merged into dependency flows.

### Flow lifecycle

`FlowLauncher.launch_flow()` is the one procedure every flow run and every dependency run goes
through, make's order: bring every prerequisite up to date, then judge this flow against them. Its
stages (each a method; the docstring lists them): **input** (`_input_settings`: validate in
context, apply `--debug`) -> **identity** (`_run_identity`: design hash + `flowrun_hash`, run dir,
locked via `run_lock` until the trace is written) -> **prepare** (construct the flow with its own
*copy* of the input, `init()`, which registers dependencies -- runs even for a flow that turns out
fresh, so it must not change a file in its run directory, all of which are outputs) ->
**dependencies** (`_run_dependencies`, recursing, each in a sibling run directory) ->
**freshness** (without `rebuild_all`, the default: `trace.check_trace` against what the flow
would consume now; a match reuses the recorded results and skips **run** entirely; `_launch`
itself removes the trace here, before **run**, so nothing vouches for the directory from this
point on) -> **run** (`_launch` records every expected input as the run finds it,
`trace_inputs.snapshot_inputs`, and writes `settings.json`; then `_execute`: `run()`,
`parse_reports()`, `check_results()`) -> **report** (`_report`: artifacts, `results.json`; `_launch` then writes a
fresh `trace.json`, on success, once `_report` has returned).

**Every failure path after a run starts leaves a failure document**: `results.json` with
`success: false`, `error.type`, `error.message` and the run's identity -- a failing `run()`, and a
failing dependency, which the depender's directory reports too. The previous `results.json` is
removed before the run, so an earlier success never stands for a run that died
(`tests/test_failure_results.py`).

- **The input settings are never modified.** The launcher keeps them (they are what the run is
  hashed by and recorded as `settings.json`'s `flow_settings`); the flow gets a deep copy as
  `self.settings`, which `__init__`/`init()`/`run()` may complete with resolved paths, derived
  options and outputs (recorded as `effective_flow_settings`, as of the end of the run).
- **Neither is the design.** It is hashed before the flow runs, recorded beside that hash and
  handed on to the dependencies, so the flow gets a deep copy as `self.design`; nothing a flow
  does to it reaches anyone else (`tests/test_flow_design_isolation.py`). Still, a flow computes
  what it derives from the design where it uses it (a template global such as `top_is_vhdl()`)
  rather than editing it.
- A flow's settings come from layers merged key by key (`flow_runner/settings_layers.py`),
  **origin first**: defaults < project < design < command line < API, each origin composed on its
  own and nesting (`nextpnr.yosys` over `[flows.yosys_fpga]`) applying only within one origin
  (`compose_flow_settings`), so a design's `[flows.yosys_fpga] flatten` beats a project's
  `[flows.nextpnr] yosys.flatten`. `-s flows.<flow>.key=value` sets any flow of the run (the
  requested flow or one of its declared dependencies; an unknown flow is an error with
  suggestions); `-s key` and `-s flows.<requested>.key` are one setting (two values for it are an
  error); a `-s` that names the wrong flow suggests the right one. `--remote` follows the same
  rules. `-s` takes space-separated KEY=VALUE items and ends at the next option or the first
  token that is not KEY=VALUE (its key must look like a setting name), so it never swallows the
  design file; `--` ends the options. Local runs, remote runs and dependencies all use
  `merge_layers`. Under the field holding a declared
  dependency's settings (`nextpnr.yosys`), that dependency's own sections (`[flows.yosys_fpga]`)
  are the base (`settings_layers.flow_settings_from_sections`, used by the launcher and the
  remote runner alike): the depender resolves shared settings in `init()`, before the dependency
  launches, so it must see them up front -- `fpga` given only for `yosys_fpga` reaches `nextpnr`.
- A dependency's launch settings are composed in `default_runner.dependency_settings`: the
  design's/project's own section for the dependency's flow, refined by what the depending flow
  passed to `add_dependency` (for a declared dependency, `resolve_dependency`'s result); then the
  depender's `debug`, and a `verbose` level above 1, carry over.

- `init()` (not `__init__`) is where a flow registers dependencies via
  `self.add_dependency(DepFlowClass, dep_settings, copy_resources=[...])`. Deps run in nested run dirs
  and completed instances are available as `self.completed_dependencies` / `self.pop_dependency(Cls)`.
  Example: `VivadoPostsynthSim` depends on `VivadoSynth`; `Nextpnr` depends on `YosysFpga`.
- `run()` generates scripts and invokes tools. `parse_reports()` populates `self.results`;
  `self.results.success` decides pass/fail. Helpers: `parse_report_regex()`, `parse_regex()`,
  `parse_xml()` (`utils.py`). **Every report (or log, results file, bitstream) a flow reads by
  path goes through `Flow.report_file(path)`** -- `parse_regex` does -- which is None for a file
  this run did not write (`Flow.written_by_this_run`, by the file's identity and state against
  the snapshot the launcher takes of the run directory just before `run()`, `Flow.start_run`;
  never a clock): a run directory is reused, and a previous run's report must never pass for
  this run's when the tool fails before writing its own. `tests/test_stale_reports.py` sweeps
  every flow (a stale copy of each file its `parse_reports` reads, and a tool that writes
  nothing: none is read, the run fails). The runner then calls `check_results()`, the checks a
  whole family of flows shares, which must pass too: `SimFlow`'s reads cocotb's results for every
  simulator that ran a cocotb testbench, from the results file this run wrote (`report_file`;
  `Cocotb.add_results`: a missing or unreadable results file, or one in which no test ran, is a
  failure). No cocotb simulator overrides it (`tests/test_cocotb.py` sweeps them, on real tools).
- `Cocotb.env()` is what every cocotb simulator run goes through, right before it simulates; it
  also removes an earlier run's results file (`RunDirectory.remove`, in the simulating flow's run
  directory), since cocotb writes none when its test module fails to import and the simulators
  still exit 0.

### Flow registration

`Flow.__init_subclass__` auto-registers every non-abstract subclass in `registered_flows` under its
canonical snake_case name (`VivadoSynth` -> `vivado_synth`, via `camelcase_to_snakecase`), its
CamelCase class name, and any `aliases`. Registering the canonical name matters: `snakecase_to_camelcase`
is not a lossless inverse (`open_xc7` -> `OpenXc7` != `OpenXC7`), and relying on that round-trip used to
make `open_xc7` and `yosys_sim` unrunnable. `get_flow_class` normalizes dashes, retries
case-insensitively, and raises `FlowNotFoundError` with close-match suggestions. `flows/__init__.py` `walk_packages()`s the subpackages to populate `__builtin_flows__`,
and also re-exports flow classes explicitly in `__all__` - **add new flows to both the import list and
`__all__`** so they appear in `xeda list-flows` and CLI completion.

Flow base classes to inherit from (`flow/__init__.py`): `SimFlow` (adds `vcd`, `stop_time`, cocotb
integration keyed on `cocotb_sim_name`), `SynthFlow` (adds `clock` / `clocks` with
`PhysicalClock` reconciliation against `design.rtl.clocks`), and its `FpgaSynthFlow` (adds `fpga: FPGA`)
/ `AsicSynthFlow` specializations.

For a single physical clock, use `clock.period` or `clock.freq`; `clock_period` is a legacy input
spelling only. Supplying it together with `clock` or `clocks` is an error. Multi-clock constraints
use the `clocks.<name>.period`/`freq` mappings.

### Settings

Every flow declares a nested `class Settings(<Base>.Settings)`. Settings are pydantic models
(`XedaBaseModel`) with `extra = forbid`, so an unknown key in a design/CLI override is a hard error -
this is intentional and surfaces as `FlowSettingsError`. CLI `-s key=value` supports dotted
hierarchical keys.

**A setting accepts exactly its declared type; there is no implicit conversion.** A number is not
text (`compile_args = ["-j", "8"]`), text is not a list, `True` is not a name. Where a setting's
values really are of several kinds, its type says so and whatever consumes it renders each kind
explicitly: Vivado run properties are `str | int | float | bool`, rendered for Tcl by
`tcl_property_value`. Text that is naturally written as a number is declared per field with the
`Code` type (`xeda.dataclass`): an FPGA's `speed = -1`, `grade`, `generation`. On top of that, `Flow.Settings._normalize_flow_setting` gives every *flow*
setting three conveniences, applied before any field validator runs (by a model `before` validator
on construction and reload, by `Flow.Settings.__setattr__` on assignment):

1. A list setting given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`; spaces around
   items and empty items are dropped, so `""` is `[]`). A setting that also accepts plain text
   keeps it whole.
2. `$PWD`, `$DESIGN_ROOT`, `$DESIGN_DIR` are expanded at every `Path` leaf of the annotation
   (`_expand_path_values`): scalars, `str | Path` unions, list/dict/tuple elements -- in
   `lib_paths` only the path half of each tuple, never the library name.
3. A dependency's settings given as an instance are deep-copied, on construction and assignment.

**Dependencies share settings declaratively, and resolve them at launch.** A flow that launches
another declares which settings they share, keyed by the field holding the dependency's settings:
`dependency_settings = {"yosys": ("fpga", "clocks")}`. Settings only ever hold what was written --
validation copies nothing between a flow's settings and its dependency's, so the result never
depends on the order settings were given in. `init()` launches the dependency with
`self.add_dependency(YosysFpga, ss.resolve_dependency("yosys"))`: each shared setting comes from
the flow unless it `is_unset` there (`None` or empty), otherwise from the dependency, and the flow
adopts the resolved value too. The result is a private deep copy; the dependency settings as given
stay untouched. Every nested `Flow.Settings` field must be declared; `tests/test_dependency_settings.py`
enforces that and derives all its checks from the declarations.

**Values derived from settings are computed where they are used, not stored in settings.** Yosys's
`write_verilog_flags()` / `attributes_to_unset()` read the `netlist_*` switches when the script is
rendered, so a switch set later (as `Yosys.init` does for `netlist_expr`) still takes effect.
The same goes for anything that depends on several settings: `Flow.Settings.is_quiet` is `quiet`
unless `verbose` or `debug` is set. A flow setting's *field* validator never reads another
setting (`info.data`): it runs only when its own field is validated, so its result would depend
on the order settings were given in. `tests/test_model_invariants.py` checks every flow.

`tests/test_example_flow_settings.py` validates every example design's and project's `[flows.*]`
section against its flow (loading a design never does). The sweeps in
`tests/test_model_invariants.py` and `tests/test_malformed_input.py` pair every
setting of every flow with each value in `tests/settings_samples.PROBES` (every structural kind:
empties, numbers, bools, text, namedtuples, nested containers) and check that no input is a
traceback, that assigning a value is identical to constructing with it, that settings reload
unchanged from their `settings.json`, and that a setting all flows share keeps its type everywhere.

**A setting a flow cannot run without goes in `Flow.required_settings`** (name -> what it is and
how to give it), checked once at launch by `check_required_settings` -- never made required on
the model, and never checked in `run()`: the same settings sit inside another flow's as a
dependency's, where the launching flow supplies what they lack. `FpgaSynthFlow` requires
`fpga`; `tests/test_required_settings.py` sweeps every FPGA flow.

**Every settings field must have a `description=`.** `tests/test_documentation.py` fails otherwise
(its allowlist is empty - all ~520 visible fields are documented). The same test requires each flow
to have its own docstring (not an inherited base-class one, which `xeda list-flows` used to show)
and to declare `results_description`.

### Results

Artifact labels map to paths or nested mappings, lists and tuples of paths; relative paths
are rooted at the flow's run directory. Use `artifacts.iter_artifact_paths` to visit their
path leaves and `artifacts.map_artifact_paths` to rewrite paths without losing the grouping.
The remote runner rewrites every fetched path to its local copy; `--post-cleanup` keeps
the artifacts in `results.json`, their parent directories, the two JSON documents and the
run-directory marker.

Flows document the keys they write to `results.json` via a class-level `results_description`, built
with `describe_results(*shared_keys, **flow_specific)` from `xeda.flow`. Shared keys come from
`COMMON_RESULT_DESCRIPTIONS` in `flow/flow.py`; `describe_results` raises `KeyError` for a key that
is in neither, so it cannot silently invent a description. A flow that reports nothing beyond the
common keys declares `results_description = {}` explicitly. `xeda list-results <flow>` renders it.

After `parse_reports` and `check_results`, the runner calls
`flow.add_canonical_result_aliases()`, which **additively** copies flow-specific keys to canonical
names per `Flow.results_canonical_aliases` (`Fmax` <- `f_max`/`maximum_frequency`, `lut` <- `LUT`,
`ff` <- `FF`). Nothing is renamed or removed.
Note `clock_frequency` is deliberately *not* aliased to `Fmax` - it is the constrained frequency,
not the achieved one.

### Templates

Tool scripts (TCL/SDC/XDC/YS/...) are Jinja2 templates in a `templates/` directory next to the flow
module. `Flow._create_jinja_env` builds a `ChoiceLoader` over `PackageLoader`s for the flow's own module
*and its base classes' modules*, so a subclass inherits its parent's templates. Undefined variables are
errors (`StrictUndefined`). `self.copy_from_template("x.tcl", **ctx)` renders with `settings`, `design`,
and `artifacts` in scope. The Vivado scripts share their TCL procs (`errorExit`, `showWarningsAndErrors`,
the critical-path reports) through `util.tcl`, which every template calling one includes
(`{% include 'util.tcl' %}`); `tests/test_vivado_script_errors.py` checks both, since the fake
records an undefined proc as a tool command where real Vivado fails with `invalid command name`.
New template file extensions must be added to `[tool.setuptools.package-data]` in `pyproject.toml`
or they won't ship in the wheel.

### Tool execution

`run_process` and `Tool.run` take `timeout` (seconds; on expiry the whole process group is
stopped and `ProcessTimeout` raised -- a Docker container is named and `docker kill`ed) and `tee`
(a file the output is also copied to).

Instantiating `Tool(...)` inside a flow method auto-discovers the calling `Flow` via `inspect.stack`, so
it inherits `dockerized`, `print_commands`, and console-color settings and appends its version info to
`flow.results.tools`. Subclass `Tool` to pin an executable, a default `Docker` image, `minimum_version`,
and `highlight_rules` (regex -> ANSI, used to colorize tool output) - see `VivadoTool`. Use
`tool.derive("other_exe")` to spawn a sibling executable from the same image/config. **A query
about the tool itself (its version) goes through `Tool.probe_stdout`**, which runs it in a
temporary working directory: a flow creates tools in `init()`, in its run directory and before the
freshness check, where every file is an output, and `vivado -version` writes vivado.jou and
vivado.log where it runs (the fake `vivado` does too).

### Caching and run directories

A run is identified by `design_hash` (from `rtl_hash` + `tb_hash`: each source's content hash,
type, `standard`, `variant`, and its position in the source order, plus behavior-affecting
RTL/testbench metadata) and `flow.flowrun_hash` (flow name + input settings). Both are semantic --
they depend on what the inputs mean, not where anything is: moving a whole design never changes
`design_hash`. Every source counts by its path relative to the design root, outside it too
(`../lib/defs.vh`, `Design._source_fingerprint`): a tool can resolve another file from any
source's location, so re-arranging even VHDL or constraint sources changes the identity.
`send_design` keeps sources under the root at their relative place for the same reason.
`flowrun_hash` writes any path under the design
root or the start directory relative to it (`$DESIGN_ROOT/c.xdc`), the start directory and design
root are validation *context* rather than settings. A parameter's value is its only record: one given as a file
(`{ file = ... }` or `{ path = ... }`) becomes the absolute path the tool is handed, and a path
under the design root counts relative to it (`location_free`, the `flowrun_hash` rule; one outside
the root counts as the location it names); its content is never hashed. `send_design` re-roots such a path for a remote from the value alone. Settings paths count as text; only design sources are read
for content, and no directory's content is ever hashed. The xeda version is deliberately not part
of the hash. Local and remote runs share `flowrun_hash`.

**Runs are make-like by default.** A flow re-runs only when something it consumed or produced
changed since its last successful run; `--rebuild-all` (API `rebuild_all=True`) runs every flow.
By default there is one directory per flow; `--hashed-run-dirs` (`hashed_run_dirs=True`) gives
one per settings variant (`<flow>_<flowrun_hash>`) -- dependencies are always siblings in the same
layout, never nested. A flow that runs logs why (`log.info("Running %s: %s", flow.name,
flow.stale_reason)`); a fresh one logs that it is up to date and its recorded results are shown as
if it had just run.

An option takes a value only when the value is data (a directory, a host); a behavior switch is a
flag (D22). `run`, `dse` and `scrub` read only the environment variables they declare
(`DeclaredEnvvarsCommand`: `XEDA_RUN_ROOT`, `XEDA_DEBUG`, `XEDA_REMOTE`, `XEDA_LOG_LEVEL`,
`XEDA_DETAILED_LOGS`), never an automatic `XEDA_<OPTION>`: a leftover `XEDA_CLEAN=1` would empty
every run directory on every run. `tests/test_option_names.py` is the oracle: each launcher option
of `run` is named as the setting it sets, none is a choice, and no two names differ by a trailing
`s`.

`trace.py`/`trace_inputs.py` implement this. `trace.json`, written into the run directory last and
atomically after a successful run (`write_trace`) and removed before the next run executes
(`remove_trace`), is what makes a directory's freshness self-certifying: its mere presence means
"the last run here completed and succeeded" (S4 in `design-notes/13-foundations.md`). It records
`flowrun_hash`, `design_hash`, `xeda_version`, a digest of every file of the installed xeda package
(`xeda_code_digest`, once per process: an editable install keeps its version across edits) and of a
plugin flow's own modules (`flow_code_digest`), the programs it started as `FileRecord`s of the
resolved executable (`ProgramRecord.file`: size, mtime, inode change time, inode, content hash --
a program is checked exactly as any other input; a container image is recorded by its ID alone,
`ProgramRecord.path`), and every file as a `FileRecord` (`size, mtime_ns, ctime_ns, inode, sha`,
`digest.record_file`): its **inputs** -- the design's files (`design_files`: one walker over `rtl`
and `tb`, so a file-valued parameter counts), every existing file a path-typed setting names
(`setting_files`: nested models too, not a dependency's settings; relative paths under the design
root *and* the start directory), every entry under a directory such a setting names
(`setting_directory_files`: `xeda.listing.directory_files(follow_links=True)`, recursive, a
symbolic link followed -- cycles and re-entry broken by `(st_dev, st_ino)`, so a library reached
only through a link is tracked like any other -- `.git`/`.hg`/`.svn` skipped; not the flow's
working locations (a setting with the `WORKING` role: `reports`, `outputs`, `checkpoints`, and a
flow's own, e.g. OpenROAD's `results_dir` -- see "Every path a flow writes has a role" below), not
the run directory or run root themselves where a named directory holds them; no size cap, a
warning past `LARGE_LISTING_FILES`/`SLOW_LISTING_S`), the files the flow registered in
`Flow.implicit_inputs` by the end of `init()` (`registered_input_files`: yosys's `abc_script`,
expanded against the start directory or the environment) and the dependencies' outputs --
**recorded just before the run starts** (`snapshot_inputs`, with `inputs_recorded_ns` as their
racy threshold; each file as itself, `follow_symlinks=False`, reusing the previous run's record
where `FileRecord.trusted` vouches for it), so a file edited while the run goes on no longer
matches; its **implicit inputs**, known only after the run (depfile
entries, `yosys -E`, and files `run()` registers in `Flow.implicit_inputs`, such as open_xc7's
chip database wherever it was found; a depfile entry under the installation prefix of a host program the flow started, the directory above its resolved `bin/`, is the tool's own file and is not recorded); and its **outputs**
(`output_files`, with `outputs_recorded_ns`): every entry of the run directory after the run
(`run_directory_files`: `xeda.listing.directory_files`, recursive, artifact or not, since a
depender may read any of them by path; a link recorded as itself, never followed, by its target
text and, for a link to a file, that file's content; a directory recorded by its metadata alone,
a fixed `"directory"` digest -- its own entries are recorded in their own right; a FIFO, socket or
device by its metadata and a `"special:<kind>"` digest, never read; the names xeda reserves,
`trace.RESERVED_FILES` and the clock markers, left out) plus the artifacts outside it -- hashed once
after the run, checked by metadata after that. The reports a flow read while parsing its results
(`Flow.reports_read`, noted through `Flow.report_file`) are removed from the run directory only
when the next run turns out not to be fresh, right before it executes
(`trace_inputs.run_reports`/`default_runner`) -- not right after the trace that recorded them, so
a fresh run's own outputs, reports included, are left alone -- so a tool that fails before writing
its own report can never leave a previous run's report to be read as if it were this run's. It
also records
**where each path-typed setting points** (`setting_locations`, by key path such as
`lib_paths[0][1]` from `setting_path_leaves`/`flow.map_keyed_path_leaves`): an absolute leaf as
itself, a relative one as each existing path it is found at under the design root and the start
directory (not inside the run directory) -- `flowrun_hash` is location-free, so this is what binds
a setting naming a directory, and a launch from another start directory or of another design tree
with the same text reports "`<key>` now names `<B>` (was `<A>`)". **What a run owns is decided by
location alone**: every run directory lies under the run root now (D21), and is the run's
exclusively -- every entry in it an output, and one that appears there after the run makes it
stale ("new file in the run directory"); anywhere else, a file a setting, a depfile or the design
names stays an input, recorded as unknown (`MODIFIED_DURING_RUN`) if it was absent before the run
or changed during it, so the next launch runs again. **Whether it changed is judged by identity,
never by a clock across file systems**: against its record from before the run
(`snapshot_inputs`), by its own size, mtime, inode change time and inode
(`trace_inputs.changed_since`); a program's file against its state when it was started
(`proc_utils.StartedPrograms.before`). A file first known after the run (a depfile's entry) has
no record from before it: on the run directory's own file system, the run directory's
**file-system clock** (`digest.filesystem_time_ns`: a marker file's mtime, never the process
clock) decides; on another file system nothing does, and it is recorded unknown
(`UNRECORDED_BEFORE_RUN`), so the next launch runs once more, saying so ("input first read by the
last run, on another file system ...") -- yosys's own library files on another volume do that
once after a flow's first run (`tests/tool_utils.launch_until_fresh`). It
also carries a `run_id` for the run itself and, keyed by each dependency's run directory relative to
the run root, the `run_id` of the dependency run it consumed (`dependency_runs`) -- provenance (S3),
since there is no per-edge output digest yet (plan 2). `trace.check_trace` re-derives all of this
(`trace_inputs.expectation`)
and returns the first mismatch as the stale reason. A file counts as unchanged by its metadata when
`FileRecord.trusted` says so -- size, mtime, inode change time and inode all equal, and the later of
mtime and ctime more than 2s before the record was taken (racy timestamps) -- otherwise by content
hash, so a `touch`, `chmod`, `cp -p` or a branch round-trip costs a hash, not a re-run, while an
edit given back its old mtime is still caught (its ctime moved). A check reads the file-system clock
only before it first reads a file's content, and refreshes the trace (`Freshness.refreshed`) when a
record it read changed or has settled since (`FileRecord.settled_before`), so a racy file is not
hashed at every later check; in a read-only run directory it checks without refreshing. A dependency
that ran again always makes its depender stale too, even if nothing it declared as an input actually
changed -- there is no cross-edge cutoff until declared inputs/outputs (plan 2). What is not tracked
(each can make a stale result look fresh; `--rebuild-all` is the escape): what a symbolic link in a
run directory points to, when that is a directory (the link is recorded by its target, never
followed); programs started indirectly (a compiler under `make`, Python packages such as
cocotb); environment variables; files a tool finds on its own without reporting them. A hand edit of
any file a dependency's run left, or a file added there, makes the dependency stale, and its
dependers follow through its new `run_id`.
Pin constraints fetched from a URL are not verifiable either, so a flow using them always runs.

Dependencies are brought up to date first, then the depending flow is judged. Within one launch, a
run directory is entered at most once: two configurations of one flow resolving to the same
directory in one launch is a `FlowSettingsError` naming both requesters
(`FlowLauncher._claim_run_dir`). A flow whose `Flow.always_runs()` gives a reason always runs,
keeps no trace and reports that reason: `openfpgaloader` and a programming `open_xc7` ("it
programs a device"), a flow asked for a fresh random seed ("it draws a new random seed"), nextpnr
with pin constraints from a URL. Seeds are settings with fixed defaults (verilator and cocotb
`random_seed = 1`; `randomize_seed` defaults to false), so a default configuration is reusable. The
base `Flow.always_runs` returns `None`; every override calls `super().always_runs()`. There is no
longer a "setting names the directory it runs in" case: every launched run directory is xeda's own
(D21), so a flow's working locations can never overlap the directory it reads its inputs from --
that case existed only for the now-removed `--cwd`.

`--clean` empties a flow's run directory before it runs and runs every flow ("make clean, then
make"; it implies `--rebuild-all`). `--post-cleanup`/`--post-cleanup-purge` clean up after the
*requested* flow completes, dependencies included but deferred to the end so a depender can still
read a dependency's files; pruning removes the trace first, so a pruned run is not reused. A
POSIX lock file (`<run dir>.lock`, `run_lock.py`, beside the run directory, never inside it; none
on Windows) serializes concurrent launches of the same run directory; `xeda scrub`
removes it with the directory. `--remote` always mirrors into the hashed layout
(`<flow>_<flowrun_hash>`, `RemoteRunner.Settings.hashed_run_dirs`, `Literal[True]` as `Dse`'s), so
remote runs of different settings never share a directory, and refuses `--rebuild-all`, `--clean`
and `--hashed-run-dirs` alike (a remote run always runs fresh); it also refuses a deliverable
setting given as a location before shipping anything, and delivers `--outputs-to` only after a run
that succeeded, from the fetched artifacts in its local (always hashed) mirror. From its first write
to the mirror to that delivery it holds the mirror's `run_dir_lock`, as a local launch of the same
directory does, and it closes the gateway and connection however it ends.

`--xeda-run-dir`/`XEDA_RUN_DIR` (a hidden option per command catches both), the API keyword and
property `xeda_run_dir`, `--cached-dependencies`/`--no-cached-dependencies`,
`--incremental`/`--no-incremental`, `--cwd`, the API/launcher parameter `run_path`, and the
launcher settings `cached_dependencies`,
`skip_if_previous_run_exists`, `incremental`, `cleanup_before_run` are removed: each fails with
`` `<name>` was removed: use <replacement>``, naming the option above. `--rebuild`/`--run-dirs`
never shipped: click suggests the flags. Flow settings named `clean`/`clean_before_run` are removed the same way
(GHDL's former `clean` is now `clean_before_analyze`, an unrelated per-analysis setting).

**Every path a flow writes has a role** (D21), a `json_schema_extra` marker on the setting's field:
`WORKING` (`xeda.dataclass.WORKING`) for a working location, always a bare name inside the run
directory whatever it is given (`sim_dir`, `bobj_dir`, `impl_folder`, a log path, ...), or
`deliverable(conventional=...)` for a setting the user may give a location, which is then
**delivered** -- copied to that location once the whole launch has finished, while the run itself
always writes the fixed `conventional` name in the run directory (plan 2's convention is
`outputs/<design>.<ext>`; `flow.output_name`). `dataclass.written_role(model, field)` reads the
marker back; `introspect`'s `writes` key (`"working"`/`"deliverable"`/`None`) exposes it through
`xeda list-settings --json`. `tests/test_written_paths.py`'s `ROLES` is the one table of every
written field of every flow, bsc's four working locations (`bobj_dir`, `info_dir`,
`verilog_out_dir`, `sim_dir`) included; a new written setting needs its role added there. A plain
nested model's path fields (not only a `Flow.Settings`' own) expand `$PWD`/`$DESIGN_ROOT` too, once,
when the flow's settings are built or a field of theirs is assigned -- `cocotb.results_xml` and
`yosys_sim.cxxrtl.filename` are the settings this covers; a value assigned straight onto the
nested model afterwards is not expanded, and `written_path_problems` reports it as such.

**`xeda.deliver`** makes good on a deliverable setting's location, or `--outputs-to`, once a
launch has finished, never sooner: `ReadInputs` holds every file the launch's flows read (the
design's files, the files given, and every read setting of every launched flow, a dependency's
nested settings included) and every directory such a setting names, with each entry under it --
the trace's own listing (`trace_inputs.register_read_settings`, local and remote alike) -- so a
destination can be none of those files, nor lie in one of those directories, a file there yet or
not (whether a tool reads it cannot be known before the run; `--outputs-to` into one is refused
up front). Each node notes what it
delivers, with every file's digest, as its own run completes (`Deliveries.collect`, under its run
directory's lock); the copies themselves are made in `_finish_launch`, before the deferred
clean-ups, once every flow of the graph has registered its reads -- a dependency's output could
otherwise replace a file a later sibling or its own depender reads before that depender's `init()`
has even run. An existing file at a destination is replaced without asking only when it is xeda's
own earlier delivery there, unchanged: inode and content digest are what decide (never mtime
alone, which the R38 trust rule never lets vouch for a delivery on its own) -- a same-inode file
holding exactly the delivered bytes is xeda's copy whatever touched it since, while another inode,
or different bytes, fails closed and asks. See `docs/run-directories.rst`'s "Outputs where you
name them" for the user-facing rules (never onto an input nor into a read directory, never a
directory, never into a run root, `--overwrite-outputs`, the delivery record beside the run
directory).

`tests/test_isolation.py` is the isolation oracle (O1-O4), in four parts:

- **O1, the canary sweep, and O3, the audit hook**, exercised together by
  `test_nothing_outside_the_run_root_changes_but_what_was_named`: every registered flow
  (`FLOWS`/`settings_samples.flow_classes()`), launched under stand-in tools, in four scenarios
  (`twice`, `clean`, `purge`, `delivered` -- 100 cases) inside a `World` seeded with a canary file
  at every name a template or xeda itself could write (`CANARIES`: every flow's template
  filenames, `trace.json`, `.xeda.lock`, a Vivado project, `Logs/canary.log`, ...) and a symlink
  out to a sibling `outside/` directory. `_state` snapshots every entry of the design directory's
  *parent* (type, mode, content or link text, a file's modification time) before and after a
  launch; nothing outside the run root may differ but exactly the destinations the launch named
  (a located deliverable, the `outputs_to` directory) and the files delivery wrote there (O1) --
  a tool's file beside a delivered one, or a touch-only change, fails it
  (`test_the_sweep_sees_an_extra_file_beside_a_delivered_one`,
  `test_the_sweep_sees_a_touch_only_change`). At the same time, a `sys.addaudithook`
  installed once for the session (`_audit`, live only inside a `watching()` context) records every
  write, create, rename or delete a launch makes (`open` in a writing mode,
  `os.remove`/`rmdir`/`mkdir`/`chmod`/`utime`, `os.rename`/`os.link` -- which also covers
  `os.replace` -- `os.symlink`, `shutil.copyfile`, `shutil.rmtree`, `os.truncate`): a write inside
  the run root is ignored, one under the named delivery destination is allowed only when it comes
  from `xeda/deliver.py` (`_from_deliver`, walking the call stack), anything else is a violation
  (O3) -- so a stray write is caught even if the canary sweep's own before/after diff happens to
  miss it. `test_the_oracle_sees_every_change_outside_the_run_root` is O3's teeth test: it
  exercises every one of those calls directly and checks the hook counts them all (CPython audits
  a missing `dir_fd` as `-1`, not `None`, which is why `_placed` treats both as "no descriptor").
  Its limits, stated in the module docstring: tools outside `FAKED` are stubbed, so their real
  writes are not observed; the audit hook sees only the test's own process; paths outside the
  snapshotted parent are not compared -- the opt-in real-tool layers are where to extend it.
- **O2, the read-only tree**: `test_a_launch_needs_nothing_writable_but_its_run_root` makes every
  flow's whole tree read-only except the run root (`_freeze`/`_thaw`) and checks the launch ends
  the same way it does on a writable tree; `test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree`
  repeats it with real `bsc`/Bluesim on PR #88's `gcd` example. `test_the_command_line_changes_nothing_outside_the_run_root`
  covers the CLI itself (`--clean`, `--post-cleanup`, plain), asserting exit 0 so the oracle cannot
  pass merely because the run failed early, and
  `test_ise_synth_from_the_design_directory_deletes_none_of_its_files` is the original P10
  regression (ISE used to delete the design's own files from its start directory), with and
  without `xtclsh` on `PATH`.
- **O4, the static scan** (moved verbatim from the deleted `test_run_dir_ownership.py`): an AST
  walk of every `.py` file under `xeda/` for a call that deletes, moves over or replaces a file by
  name (`unlink`, `remove`, `rmdir`, `rmtree`, `rename`, `move`, `replace`, `truncate`, ...) outside
  `xeda.run_dir`/a `.run_directory` receiver
  (`test_nothing_deletes_but_through_the_run_directory`, checked against the exact reviewed sites
  in `REVIEWED_PY_DELETIONS`), and a text/regex scan of every non-Python file (tool scripts,
  OpenROAD and platform scripts) plus every flow's own tool commands for a deleting or
  `-force`/`-overwrite`/`create_project`-style replacing command
  (`test_every_tool_command_that_deletes_or_replaces_is_reviewed_and_guarded`, against
  `REVIEWED_SCRIPT_DELETIONS`, each entry recording why it is safe and, where it deletes or
  replaces by name, the guard text that must still be present in that flow's module). A new site,
  or a second copy of a reviewed one, fails the oracle until it is reviewed and added. Its third
  scan (`test_every_raw_write_by_name_is_reviewed`, from PR #89, with its mutation test) finds
  every raw write by name -- `open` for writing, `write_text`, a copy, a rename, a link -- that
  does not go through `replacing_file`/`replacing_copy`, against `REVIEWED_WRITES`.
  `test_links_a_tool_left_are_never_followed_out_of_the_run_directory` is the tool-made-links
  case: links in, out, dangling, cyclic and at a working location's name, through a relaunch,
  `--clean`, post-cleanup, a purge, `xeda scrub` and deliveries, with the outside tree unchanged.

`tests/conftest.py`'s autouse, session-scoped fixture snapshots the checkout's top level, `tests/`
and every example design's own directory before the suite runs, and fails if any of them gained a
new entry by the end -- the exemptions are `tests/__pycache__` (the suite's own imports) and a
top-level `xeda_run/`, the latter only when an opt-in
layer (`XEDA_TESTS_VIVADO`/`XEDA_TESTS_DOCKER`/`XEDA_TESTS_EXTERNAL`) is set, since those tests
deliberately work in the checkout's own `xeda_run/` (a container can mount it where the system
temp directory is not). A `__pycache__` in an example's directory is a failure: `test_ghdl.py`
and `test_nvc.py` simulate the examples in place.

### Other runners

- `flow_runner/dse/` - `Dse` launcher running many flow instances in parallel (`pebble`) under an
  `Optimizer`; `FmaxOptimizer` (`fmax.py`) does the binary/interpolation search for max clock frequency.
- `flow_runner/remote.py` - `RemoteRunner` ships the design over SSH via `execnet`, runs xeda remotely,
  and streams tool stdout/stderr back through a PTY pair. The streaming/PTY behavior is heavily tested
  in `tests/test_remote_streaming.py`; the code injected into the remote (`STREAM_OUTPUT_SETUP`,
  `remote_runner`) must stay dependency-free and only use long-stable xeda API - a test asserts this.
  The design archive `send_design` builds is read by the *remote's* xeda, which forbids unknown
  keys, so it must stay loadable by `REMOTE_XEDA_MIN_VERSION`. **Requirement: a remote runs the
  latest published xeda or newer** (currently 0.4.3), and `check_remote_xeda` refuses anything
  older. On each release, raise `REMOTE_XEDA_MIN_VERSION` and `test_remote_run.py`'s
  `RELEASED_RTL_KEYS`/`RELEASED_TB_KEYS`/`RELEASED_GIT_REFERENCE_KEYS` (with the keys it takes as
  `null`) to the new release; until then the archive and the
  shipped `remote_runner` may rely on nothing newer than it (a newer API only behind a
  `getattr` probe). `REMOTE_PROBE` reports
  which xeda the remote interpreter imports (execnet starts `python3` from the *non-login* PATH).
  A failed remote run's artifacts are fetched only if the remote vouches its run wrote them:
  `remote_runner` sends its results, then that list, judged on the remote's own file system (the
  remote flow's `wrote_output`, or, on an older xeda, the remote directory's state recorded
  before the run: identity, size, times); `_transfer_artifacts` drops the rest. A file merely
  existing on the remote (an earlier run's) proves nothing, and no clock or file of this side is
  ever compared with the remote's.
- `platforms/` - ASIC PDK descriptions (asap7, nangate45, sky130hd/hs) for OpenROAD/DC;
  `board.py` + `data/boards.toml` for FPGA boards.

Board-aware settings read their database through `WithFpgaBoardSettings.board_data()`.
`custom_boards_file` replaces the bundled database and resolves relative to the design root.
A board's local `lpf` resolves relative to its database file, bundled or custom
(`WithFpgaBoardSettings.board_file`, a context manager: a bundled file may exist on disk only
while it is open). A flow sharing
`board` with a board-aware dependency must also share `custom_boards_file`.

## Conventions and gotchas

- **Help screens are click-extra's, and the theme rides on `context_settings`.** `XedaHelpGroup`
  subclasses `click_extra.Group`; the xeda palette (yellow headings, green options, carried over
  from the old `click_help_colors` setup) lives in `XEDA_HELP_THEME` / `HELP_FORMATTER_SETTINGS`
  in `cli_utils.py` and is injected through `CONTEXT_SETTINGS`, because cloup resolves the
  formatter from the *context* and child contexts inherit it -- a `formatter_settings=` passed to
  a command styles only that one screen. `XedaHelpGroup.main` additionally calls
  `set_default_theme()`, since click-extra's auto-injected `help` subcommand (`xeda help run`)
  renders its target through a plain `click.Context` that carries no formatter settings.
  click-extra also highlights every command name, option, choice, metavar and envvar wherever it
  appears in help prose; when a name is also an ordinary English word (the `run` command) pass
  `excluded_keywords=HelpKeywords(cli_names={...})`, which needs an explicit `cls=ColorizedCommand`
  for cloup's `command()` overloads to accept it. Colors follow `NO_COLOR` / `FORCE_COLOR` /
  `CLICOLOR`, so they survive a pipe when one of those is set -- assert on `click.unstyle(...)` in
  tests rather than on raw help output.
- **Two parameters must never share a destination.** click >= 8.5 warns on every invocation when
  they do, and one silently overwrites the other. `xeda run` hit this with the positional
  `DESIGN` argument and `--design-file`; the option now uses `design_file_opt`.
- **cocotb integration sets `GPI_USERS` itself.** Xeda builds the simulator environment by hand
  rather than going through `cocotb_tools.runner`, so anything the runner sets has to be mirrored
  in `Cocotb.env()`. From cocotb 2.1 the GPI library no longer finds its Python entry point on its
  own and aborts with "No GPI_USERS specified"; `Cocotb.gpi_users()` supplies libpython plus the
  entry point from `cocotb-config --pygpi-entry-point`. That flag does not exist before 2.1, so it
  is probed rather than assumed, which is what keeps cocotb 2.0 working. Test selection moved
  too: cocotb 2.x ignores 1.x's `TESTCASE` and reads `COCOTB_TEST_FILTER`, so
  `Cocotb.test_selection` picks the variable by the version `cocotb-config` reports (2.0.0 reads
  the filter without listing it in `--help-vars`). Its filter matches each `testcase` name
  exactly, as 1.x does (`check`, or `tb.check`, never `foo_check`) -- stricter than the runner's,
  which matches every test name ending with it. When a cocotb upgrade breaks every simulation at
  once, or a setting silently stops working, compare `Cocotb.env()` against
  `cocotb_tools/runner.py` first.
  `Cocotb.env()` also sets `PYTHONPYCACHEPREFIX` to the run directory's `__pycache__`: the
  testbench is imported from the design's directory (`PYTHONPATH`), and pytest's assertion
  rewriting would otherwise write its bytecode there, into the user's tree
  (`test_isolation.py::test_a_cocotb_simulation_leaves_the_design_directory_as_it_was`, real
  GHDL and nvc).
- **pydantic 2 is pinned** (`>=2.13.5,<3`). Import `field_validator` / `model_validator` from
  `xeda.dataclass` (which re-exports and adds `XedaBaseModel`), not directly from `pydantic`.
  Every validator needs an explicit `@classmethod` under its decorator.
- `XedaBaseModel.model_config` sets `validate_assignment`, `arbitrary_types_allowed`,
  `ignored_types=(cached_property,)`, `populate_by_name`, `use_enum_values` and
  **`validate_default=True`**. After `model.model_copy(update=...)`, call
  `invalidate_cached_properties()` - stale `cached_property` values are a recurring bug source (see
  `Tool.derive`).
- **`validate_default=True` is deliberate**: a default goes through the same validators as a given
  value, so omitting a setting and writing its default explicitly (as `settings.json` does) are the
  same. Never opt a field out with `validate_default=False`; that is what made reloaded settings
  differ from the originals. Make the validator handle the default instead.
- **`Optional[X]` needs an explicit `= None`.** A bare `x: Optional[int]` (or
  `Field(description=...)` with no default) is a *required* field.
- **pydantic runs a subclass's `before` validators ahead of its base class's**, at field and model
  level alike. So a base-class *field* validator cannot normalize input for the subclass's
  validators, and a model `before` validator cannot change the value being *assigned*.
  `Flow.Settings` therefore normalizes with a model `before` validator plus `__setattr__`.
- Fields ending in `_` (e.g. `Tool.design_root_`, `Tool.flow_settings_`) are internal and marked
  `json_schema_extra={"hidden_from_schema": True}`; they are excluded from user-facing settings docs.
  Flow settings have none: where settings were given (design root, start directory) is validation
  context -- `Flow.Settings.from_input(data, design_root=..., runner_cwd=...)`, kept privately as
  `settings.context` for assignments -- so it is never dumped or hashed.
- **`Flow.Settings` has no custom `__init__`, and must not get one**: pydantic validates a model
  with a custom `__init__` *through* it, which drops the validation context. User input goes
  through `from_input`, which reports a `FlowSettingsError`; `Settings(**data)` raises pydantic's
  `ValidationError`.
- Arbitrary (non-pydantic) types used as fields need `__get_pydantic_core_schema__` *and*
  `__get_pydantic_json_schema__` - see `FileResource`/`DesignSource` in `design.py`. Without the
  latter, `model_json_schema()` raises `PydanticInvalidForJsonSchema`.
- **Import `field_validator`/`model_validator` from `xeda.dataclass`, never from `pydantic`.**
  Their versions guarantee two things for every validator:
  a `TypeError` raised in a validator becomes a validation error rather than escaping as a
  traceback, and a `mode="before"` validator gets a defensive copy of its input so the widespread
  "normalize by writing back into `values`" pattern cannot rewrite the caller's own mapping (a
  design's `flow[...]` section, a `Settings` kwargs dict), including nested clock/parameter/corner
  mappings. Validators should still guard their own inputs and raise `ValueError` with a useful
  message - the shim is a net, not a substitute.
- **A validator must copy a nested *model instance* before normalizing it.** pydantic keeps the
  caller's object rather than re-validating it, so `value.fpga = ...` edits settings the caller
  still owns. See `VivadoAltSynth.validate_synth`. `revalidate_instances` is no way out: it
  downcasts a subclass instance to the annotated class (`Design.dependencies`,
  `RtlSettings.generator`, `Tool.docker`, `SimFlow.cocotb`, ... hold subclasses).
- **A `mode="before"` model validator runs on every assignment, and its writes stick.** Under
  `validate_assignment`, `model.x = v` hands it the full state; `x` keeps its raw value, but every
  *other* field it rewrites is written back. So treat the assigned field as authoritative (detect
  assignment with `info.field_name if info.data is None else None`) and change nothing on an
  assignment that does not concern you, or you silently revert direct edits. See
  `SynthFlow.Settings._synthflow_settings_root_validator`. Better still, store one value: a
  setting with several names is one field with `validation_alias=AliasChoices(...)` plus a
  property for the other name (`DVSettings.parameters`, alias and property `generics`);
  `introspect.design_schema` advertises every choice. `dataclass.input_names(model)` is the one
  table of every accepted spelling; the layer merge and the flow-setting conveniences share it.
- The wrapper passes a non-`dict` input to a `mode="before"` model validator straight through to
  pydantic, which rejects it, since those bodies are written against a mapping. A validator that converts a
  shorthand itself, like `FPGA` turning `"xc7a..."` into `{"part": ...}`, opts in with
  `@accepts_non_mapping` under `@classmethod`.
- **Validators must be idempotent.** Assignment and every `settings.json` reload re-run them, so
  one that *transforms* drifts each time (ISE's option quoting turned `"High"` into `""High""`).
  Format for a tool at render time in the template instead. `tests/test_model_invariants.py`
  re-assigns every field of every flow's settings to itself and fails on any change.
- **Nested models serialize by their *annotated* type.** A field holding a subclass needs
  `SerializeAsAny[...]` (see `Design.dependencies`, which holds `GitReference`s) or the subclass's
  own fields are silently dropped from `model_dump()`. `model_dump(serialize_as_any=True)` is not
  a substitute: it duck-types *every* value by shape, so it also skips the serializer an arbitrary
  (non-pydantic) type declares on its own core schema -- `FileResource`/`DesignSource` do exactly
  that -- which is what made `Design.model_dump_json()` raise `PydanticSerializationError`. Declare
  `SerializeAsAny[...]` on the field instead.
- Read a model's state with `utils.model_state()`, not `__dict__`: permitted extras live in
  `__pydantic_extra__`, and `__dict__` alone drops them from `settings.json` and from
  `semantic_hash()`. Conversely `__dict__` also holds `cached_property` caches, which
  `model_state()` filters out.
- **Everything xeda writes as JSON goes through `utils.json_encodable`** (`dump_json`, the remote
  design archive `send_design` builds, the results table `print_results` renders). A pydantic
  model therefore serializes with `model_dump(mode="json")`, so its fields' own serializers run;
  a `FileResource`/`DesignSource` is not a pydantic model itself, so it defines its JSON form in
  `as_json_value()` -- a bare path string, or the table it cannot be rebuilt without (`{"path":
  ...}` for an unchecked resource, plus any `type`/`standard`/`variant` the design *stated*).
  **Nothing is serialized by reading its `__dict__`**, and a new type that needs a JSON form
  declares `as_json_value()` rather than relying on one. An object's attributes are not a
  serialization format: that is what put a private `_specified_path` into `settings.json`, and
  on a plain `Enum` (`__objclass__` in its `__dict__`) descending into it does not terminate --
  `semantic_hash`'s `_sorted_dict_str` carries an explicit guard for that. `json_encodable` is
  the *one* encoder, with `utils.with_json_keys` for keys (which `json` hands no hook: a `Path` or
  tuple key would raise, so every writer and `json_safe` apply it -- no key may fail a document).
  `introspect.json_safe`, which builds every `--json` document, encodes through the same two
  and reads the text back, so a value printed and the same value written to a file cannot differ
  (`test_serialization_fidelity.py` checks exactly that).
  `utils.model_state` is for hashing, not for output.
- State that must survive `model_dump()` -> `model_validate()` belongs in a hidden
  trailing-underscore field, not a `PrivateAttr` or an `__init__` side effect, since a dump
  carries only fields. See `AsicsPlatform.voltage_expressions_`, which lets `select_corner()`
  re-evaluate `$(VOLTAGE)` on a platform reloaded from `settings.json`.
- **Physical quantities from PDK/board files must be `float`.** asap7/nangate45 give
  `abc_load_in_ff`, `macro_place_halo` and `macro_place_channel` fractionally; typed `int`, the
  platforms would not load.
- `units.convert_unit()` translates pint's own exceptions (`UndefinedUnitError` derives from
  `AttributeError`, `DimensionalityError` from `TypeError`) into `ValueError`, so a bad unit in a
  design file is a field error rather than a traceback. The registry is case-sensitive, as in SI:
  a case-insensitive one made pint pick among homographs ("nS": nanosecond or nanosiemens) by
  hash seed. `check_unit_case` rejects a `CLOCK_UNITS` unit in another case ("mhz", "mHz", "Ms"),
  naming the right spelling, rather than reading it as whatever it spells in SI. A quantity is
  exactly `<number>[<unit>]` (`units._QUANTITY_RE`): text never reaches pint's expression parser,
  and bools and non-finite values are rejected.
- **A design's files must exist when it is loaded.** `FileResource` checks a `file` (or a plain
  path) and expands `$DESIGN_ROOT`/`$DESIGN_DIR` to the directory relative paths resolve against;
  `{ path = ... }` is unchecked, for files a generator creates later. A validator that builds one
  converts `FileNotFoundError` (neither a `ValueError` nor a `TypeError`) into a `ValueError`.
  `FileResource._specified_path` keeps the path *as written*, before expansion; it plays no part
  in the design hash, which counts every source's path relative to the design root. It is how
  `Design.source_path_as_named` names a source that lies outside the design
  root, which has no relative name.
- **A path a user named goes into a tool script through a filter, never raw.** yosys templates:
  `read_path` for a file yosys expands as a glob pattern of its own (`read_verilog`,
  `read_liberty`, `techmap -map`, `dfflibmap -liberty`; `yosys.common.frontend_name`), `path`
  for any other argument it unquotes, `verbatim_path` for plugins. TCL templates of every flow:
  `tcl_word` for one literal argument, `tcl_quote` inside a `"..."` message, `tcl_list` for
  Vivado's file-list commands (`read_verilog`, `read_vhdl`, `read_xdc`, `add_files`, `get_files`), which
  split a single word at its spaces (`utils.py`, registered by `Flow._create_jinja_env`). Never
  `eval` a command with a path: it parses the path a second time. `tests/test_yosys_templates.py`
  and `tests/test_tcl_paths.py` enforce this; the latter runs rendered scripts under `tclsh` with
  every tool command recorded. Its oracle requires every value a Tcl template reads from the
  design or the settings that holds user text (`str` or `Path`, alone or in a list, at any depth:
  `design.name`, `design.tb.top[0]`, `settings.fpga.part`) to go through a filter, unless
  `REVIEWED_RAW` says why it is written as words (tool flags, a simulation time); and it renders
  every fake-tool flow's scripts and constraint files for a design whose every text carries Tcl
  metacharacters, which must reach the tool whole. Never `eval` a value in a template: rendered
  text is parsed once already. A path xeda
  located before the run (`RunDirectory.inside`) is handed to the script as that value itself:
  Diamond's `impl_folder`, rendered in double quotes, named whatever Tcl substituted it into.
  PDK files and xeda's own run-directory paths are left raw.
- **A flow's clean-up deletions, and every file a flow or the launcher writes where a flow runs, go
  through `RunDirectory` (`xeda/run_dir.py`)**, the flow's read-only `self.run_directory`, a frozen
  dataclass the launcher decides once and hands to the flow's constructor: `RunDirectory.claimed`
  (a directory xeda chose that lies under its run root -- every launched flow's, now, D21) or
  `RunDirectory.unlaunched` (a flow built directly, not through a launcher: xeda deletes nothing
  there, logged instead). `inside`/`holds` locate a path inside the directory without following a
  symbolic link out of it (a link is itself, its last component never resolved); `writable(path)`
  is what every file xeda writes there itself goes through (`copy_from_template`, the launcher's
  `settings.json`/`results.json`, `Tool.run`'s `env.sh`, and every flow's own writes) -- it removes
  a link at that name first, so the write always makes a regular file, and raises in an
  `unlaunched` directory rather than silently deleting through someone else's link. The file
  itself is then written complete-then-renamed (`utils.replacing_file` / `replacing_copy`: a
  temporary beside the target, `os.replace`d over it only once whole, `keep_on_error` for a failed
  tool's log, `copy_mode_from` before the commit), so an interrupted write leaves the earlier
  file. A path that leads out of the run directory -- by `..`, or through a symbolic link a tool
  may have made -- is refused by `inside` with a `RunDirectoryError` naming the link; a link at a
  path's own name is removed as itself, never followed. `remove(*paths)`
  deletes each -- file, link (as itself) or directory tree -- inside the directory only, nothing in
  an `unlaunched` one; `clear()` empties the whole directory; `delete()` also removes the directory
  itself. A tool is free to replace a project or a directory by its own name inside the run
  directory (`-force`/`-overwrite`, Diamond's and ISE's new project): the whole directory is
  xeda's, so there is nothing of the user's there for the tool to lose. **`RunDirectory` is not the
  only writer of a run directory, though**: `trace.json` (`write_trace`/`remove_trace`,
  `flow_runner/trace.py`) and the marker `filesystem_time_ns` touches to read the run directory's
  clock (`digest.py`) are xeda's own reserved names, created and removed directly by those modules
  rather than through `RunDirectory`; a dockerized tool's own `.<tool>_docker.env` (`Tool.execute`,
  `tool.py` ~140, written with a plain `open(..., "w")` right before the container starts) bypasses
  it the same way -- there is nothing of a flow's or a user's under any of these names for that
  boundary to protect. `tests/test_isolation.py` is the current oracle (see "Caching and
  run directories" above; it replaced `test_run_dir_ownership.py`'s `--cwd` sweep, which is gone
  with `--cwd` itself).
- **Compare a source's type with `SourceType`, never with free text**: `src.type is
  SourceType.Xdc` in Python, `src.type.name == "Vhdl"` in a template. A `SourceType` equals only
  its own name, so `src.type == 'verilog'` is silently never true -- ModelSim compiled no source
  and `vivado_project` read no design XDC that way. `tests/test_source_type_comparisons.py`
  sweeps the flows' code and templates.
- **Every consumer of a design path goes through `design.py`'s helpers**, or it re-derives the
  loader's rules and gets them wrong. `_expand_design_path` expands one path;
  `_expand_source_glob` expands *and sorts* a pattern and rejects one matching no file (`glob`
  returns filesystem order, and source order is semantic -- VHDL compile order, and part of the
  design hash); only `*` makes a source a pattern (`_is_source_pattern`) -- `?`, `[` and `]` are
  part of a file name (`fifo[1].v`), and as glob syntax named another file (`fifo1.v`);
  `_source_paths_as_given` interprets an unvalidated `rtl.sources` entry, which is
  what `process_generation` needs since it runs before the sources validator.
  **A flow that writes one artifact per source names it with `Design.source_artifact_name(src,
  suffix)`**, never from `src.path.stem`: sources are distinct files but their stems are not
  (`rtl/a/fifo.vhd`, `rtl/b/fifo.vhd`), so a stem alone cannot tell their outputs apart. The
  name folds the source's path relative to the design root into one filename-safe token
  (`rtl_a_fifo.v`); a fold another of the design's sources shares (`my-fifo`/`my_fifo`,
  `fifo.vhd`/`fifo.vhdl`, a dependency's same-named file) carries a digest of its path, so
  distinct sources get distinct names by construction. It is naming only, so nothing may read it
  back as design state. GHDL (`--out=verilog`, one Verilog file per VHDL source) is the only
  flow that needs it today; any other that emits per-source artifacts uses the same helper.
- Most tests use fake EDA tools: `tests/fake_tools/` holds symlinks (`vivado`, `quartus_sh`,
  `xtclsh`, `dc_shell`, `diamondc`, `vsim`) to `fake_tool.py`, a click-based stub that dispatches
  on `Path(__file__).stem`. A fake **runs the TCL script it is handed under `tclsh`**, the tool's
  own commands recorded rather than run (`TCL_RECORDER`, into `fake_<tool>.calls` in the run
  directory), and fails as the tool would on a TCL error -- so a template that renders broken TCL
  fails every test using it. Vivado's then writes canned reports
  (`tests/fake_tools/resource/fake_vivado_reports`). `tool_utils.use_fake_tools(monkeypatch)` puts
  them on `PATH`; `tool_utils.fake_calls(run_dir)` reads what the scripts ran; commands named in
  `XEDA_FAKE_TOOL_FAIL` raise a TCL error, as a failed compile does (so does `exec` of a program
  named there, such as `xvhdl`). Where recording is not enough, `TCL_MODEL` models a tool's own
  commands: ISE's `process` (a failed process shows only in its result and status, never as a
  TCL error; `XEDA_FAKE_ISE_FAILURE`) and Diamond's
  `prj_run` write the reports and bitstream the real steps write, unless
  `XEDA_FAKE_TOOL_NO_OUTPUT` is set -- a step that succeeds without its output. Vivado's
  `launch_runs` runs each project run's enabled steps up to its `-to_step`, sourcing the step
  hooks `set_property` attached, in `<project>.runs/<run>`; a step named in
  `XEDA_FAKE_TOOL_FAIL` fails, and `get_property` reports the run's `STATUS` and `PROGRESS` as
  Vivado 2024.2 does. `tool_utils.fake_returns` (`XEDA_FAKE_TOOL_RETURNS`) makes any call
  return what a test says, by its leading words (`get_property STATUS impl_1`). To fake a new tool:
  add a symlink, add an entry to the `fake_tools` dict (the option or argument naming its script,
  for `RunTcl`), and use `use_fake_tools`.
- **A simulation passes only on evidence that it ended.** `SimFlow.check_results` judges a run by
  `SimEvidence` through `judge_evidence` (`flow/sim.py`), never by the tool's exit status alone:
  what the simulator's own end record shows (`sim.ended_by`, `sim.time`, ... result keys), against
  `fail_severity`. cocotb needs at least one test that ran and none that failed (an all-skipped
  run fails). Verilator is driven by xeda's own C++ main, which installs Verilator's `VL_USER_*`
  hooks and writes the end record; a design's own C++ driver, or cocotb, replaces it. Its
  settings: `timeout`, `fail_severity` (`warning`/`error`/`failure`/`fatal`, default `error`),
  `random_init` (default false), `x_initial`/`x_assign` (`"0"`), `rtl.parameters` applied when the
  RTL top is the simulated top, `--top-module`, and `stop_time` (rejected with cocotb or a design's
  own driver); minimum Verilator 5.024. `bsc_sim` has `timeout`. The other simulators are not
  converted yet; `tests/test_sim_evidence.py` is the oracle and lists them.
- **ModelSim exits 0 unless told otherwise.** Its `exit` takes the status as `exit -code N` (a
  plain `exit 1` exits 0, and the fake `vsim` mimics that); without `vsim -onfinish stop`, `$finish`
  exits vsim at once with status 0; and a testbench's `$error` or failed assertion never changes
  the status -- `run.tcl` reads `coverage attribute -name TESTSTATUS` instead and fails at
  the `fail_severity` setting (default `failure`). `BreakOnAssertion` is set to 4 after loading
  the design so VHDL `failure` does not stop the testbench before its finish. ModelSim reports
  VHDL `failure` and SystemVerilog `$fatal` as the same TESTSTATUS (3), so `fatal` and `failure`
  have the same status threshold. The flow's default image is `chaseruskin/modelsim-intel`
  (ModelSim-Intel Starter 2020.1, amd64, no license).
- **A Vivado project run's outcome is in its properties alone.** `wait_on_run` returns normally
  when the run failed in Vivado 2021.1 and raises an error in 2024.2, so `vivado_synth.tcl`'s
  `xedaWaitOnRun` waits either way, then requires `STATUS` "<step> Complete!" and `PROGRESS`
  "100%" (a failed run reports "<step> ERROR"), records the status (the `status` result) and
  otherwise exits 1 naming the run, its status and `<project>.runs/<run>/runme.log`.
- **Yosys FPGA synthesis options are chosen by the installed yosys release.** The `synth_*`
  passes changed their options across releases (ABC9 became the default in 0.36, Nexus moved to
  `synth_lattice` in 0.59, 0.69 made ABC9 unconditional and dropped `-retime`), so
  `YosysFpga.Settings.synth_command(release)` maps each setting to the option the target's pass
  has in that release, or rejects it -- a setting is never silently dropped. `yosys_release`
  treats an unreadable or newer yosys as `NEWEST_CHECKED_YOSYS`. The oracle is `PASS_OPTIONS` in
  `tests/test_yosys_fpga_flags.py`, read from the passes' sources for every supported release
  from 0.63:
  on a new yosys release, add its option changes there and raise `NEWEST_CHECKED_YOSYS`.
- **A flow rejects a target it cannot handle in `init()`, before its dependencies run** --
  after `resolve_dependency`, which may be what supplies `fpga`. `nextpnr` checks its device
  mapping and settings of other architectures (`_target`), `openfpgaloader` its packer, so an
  unsupported family never costs a synthesis or place-and-route run.
- **Real proprietary tools and containers are opt-in layers**, skipped unless their variable is set
  (and then failing on what they need): `XEDA_TESTS_VIVADO=1` runs Vivado flows on tiny designs
  (`tests/test_vivado_real.py`, `vivado` on PATH); `XEDA_TESTS_DOCKER=1` runs flows `dockerized`
  in their default images (`tests/test_dockerized.py`), skipping one whose image is not present
  locally -- a test never pulls. Both work under the checkout's `xeda_run/` (or
  `XEDA_TESTS_WORK_DIR`), which a container can mount where the system temp directory is not.
  `XEDA_TESTS_EXTERNAL=1` runs `bsc`/`bsc_sim` end to end on real, external Bluespec repositories
  at pinned commits (`tests/test_bsc_external.py`), cloned once per session into
  `XEDA_TESTS_EXTERNAL_CACHE` or the checkout's `xeda_run/external/`; CI runs it on the latest
  Python version only. Its slowest test (Piccolo's core) also needs `XEDA_TESTS_EXTERNAL_SLOW=1`,
  which CI does not set.
- Formatting is inconsistent by design: `black` (line-length 100) is enforced on `src/` only; `ruff`
  (line-length 120, `target-version = "py311"`) checks the whole repo.
