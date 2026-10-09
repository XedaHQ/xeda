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
tox -e black                         # black --check --diff src tests tools (line-length 100) - clean
ruff check src tests                 # .ruff.toml, line-length 120
tox -e docs                          # Sphinx docs, warnings are errors (-W -n); also a CI job
```

CI also runs a `macos` job (`ci.yml`: macOS, Python 3.11, no EDA tool, so the tool-dependent
tests skip) on the test files whose code differs by operating system: file locks, process
groups, pseudo-terminals, file copying, file-system clocks, links. Its file list says why each
file is there; a new test of such code goes into that list.

**A test run started as a background job of a non-interactive shell (`pytest ... &` in a script
or a loop) ignores SIGINT, and so does every process it starts.** A tool that a test interrupts
therefore sets SIGINT to its default itself (`DEFAULT_SIGINT` in `tests/test_proc_utils.py`), and
the tree test there runs with the inherited SIGINT both default and ignored. Such a test waits
for a ready signal from the tool and polls with a generous deadline; it never sleeps for a
guessed time (a limit that must outlast the tool's start uses `TIME_LIMITS`).

**The suite is safe to run in parallel** (`pytest-xdist`, in the `dev` group; tox and CI use
`-n auto`), with outcomes identical to a serial run (checked on the full suite with the real
tools; about 8000 tests, which `-n auto` runs in about 9 minutes on 10 cores). Each test works under
`tmp_path`, so workers share nothing but read-only files (the examples, `tests/resources`, the
fake tools) and the opt-in layers' checkout `xeda_run/`. The exception that needed a fix is the
external-repository cache (`XEDA_TESTS_EXTERNAL_CACHE`): every worker asks for the same pinned
checkout, so `test_bsc_external._fetch_pinned_commit` holds an `flock` beside it while it fetches
(`tests/test_external_cache.py`). On platforms without `fcntl`, cache access skips in xdist
workers; run the external tests serially there. Keep it so: a test must not write outside
`tmp_path`, leave a process-wide change (`chdir`, `environ`, a registered flow) behind, or take a fixed name, and a
session-scoped fixture runs once per worker, not once per run. The environment has its own oracle
(`tests/test_environment_isolation.py`, with the autouse guards in `tests/conftest.py`): a direct
write to `os.environ`, in a test or at import, fails -- use `monkeypatch`. (A module-level `PATH`
append of `tests/fake_tools` once made a missing real `nextpnr-*` resolve to the fake in a full
run, so the real-tool tests passed their probes and failed only there.) `addopts` deliberately has no
`-n`: it would start workers for `pytest tests/test_x.py::test_y`. Under `-n`, the conftest
checkout guard still fires (per worker, at its teardown, on whichever test ran last there).

`jsonschema` is a test-only dependency (in the `dev` group and in tox), used to check that the
published design schema agrees with the loader.

`mypy src` (with `possibly-undefined` on: a local bound under a condition is not read under a copy of it), `black --check src tests tools` and the Pyflakes rules plus `PLW0133`, which flags a built-in exception that is built and never raised (`ruff check --select F,PLW0133 src tests tools`, the
`tox -e ruff` env) all pass; keep them that way. `PLW0133` does not see the exception classes xeda
defines (`RunDirectoryError(...)` on a line of its own); `tests/test_exceptions_are_raised.py` does. The full `ruff check` ruleset reports many
pre-existing findings (mostly `UP006`/`UP007` PEP-585/604 annotations and `RUF012`) and is not
enforced. Don't mass-fix those; keep new code clean.

Most tests use `tests/fake_tools/`, but some end-to-end tests drive genuinely installed tools
(`test_ghdl.py`, `test_nvc.py`, `test_verilator.py`, `test_yosys.py`,
`test_yosys_fpga_netlist_cells.py`, `test_openroad.py`'s yosys
synthesis, `test_bsc.py`'s `bsc`/`bsc_sim` flows, the GHDL half of `test_remote_run.py`, parts of
`test_cli_structured_output.py`, and `test_cocotb.py`'s runs of every cocotb simulator).
Those **skip** when the tool is missing or installed-but-broken, via the probes in
`tests/tool_utils.py` (`require_ghdl()`, `require_yosys_ghdl_plugin()`, `require_bsc()`,
`require_bluesim()`, ...). Setting `XEDA_TESTS_REQUIRE_TOOLS=1` turns those skips into failures;
CI sets it, so a tool vanishing from CI cannot look like a pass -- CI installs bsc from its
official release tarball (pinned in `ci.yml`) for exactly this reason. `test_remote_run.py` and `test_dse_run.py` run
`xeda run --remote` and `xeda dse` end to end on the fake Vivado; the remote one replaces only
the transport (a filesystem-backed fabric `Connection`, execnet's `popen` gateway for `ssh=`),
so no SSH server is needed. tox passes
`GHDL_PREFIX` through: on macOS the OSS CAD Suite yosys GHDL plugin cannot find `std` without it
(the suite's `ghdl` wrapper sets it, its `yosys` wrapper does not). After sourcing the suite's
`environment` for a local tox run, drop its `py3bin/` from `PATH`: it holds a bundled
`python3.11` that tox would otherwise build `py311` on, and that venv cannot start. Tests that exercise
cocotb-based example designs need `pip install -r examples/requirements.txt`.

**CI's tool pins are kept current by a weekly workflow.** `ci.yml` pins the OSS CAD Suite build
date and the bsc version with its SHA-256, and `openxc7.yml` pins the openXC7 installer commit
(`INSTALLER_REV`). `.github/workflows/bump-ci-pins.yml` (Mondays 08:23 UTC, after the suite's build, or by hand) runs
`.github/scripts/bump_ci_pins.py` (standard library only; `--dry-run` prints the changes),
force-pushes the branch `ci/bump-tool-pins` and opens or updates one pull request that lists
old and new with links. Merge it only when CI passes: a newer tool can move a result that a
test pins, such as the counts of `tests/test_openxc7_real.py`. The workflow needs the secret
`CI_PINS_TOKEN`, a fine-grained personal access token with write access to Contents, Pull
requests and Workflows (not a GitHub App token, which expires in an hour): `GITHUB_TOKEN`
cannot change workflow files, and a pull request that it opens does not start CI by itself.
The token reaches the commands that need it and no other: the checkout keeps no credential
(`persist-credentials: false`); the step that decides gets only whether the secret is set
(`secrets.CI_PINS_TOKEN != ''`); the step that runs the script never sees it; the step that
pushes takes it from its env into a shell variable and unsets the env variable before its first
command (every command of a step inherits the step's env), then gives it to the one `git push`
through that command's environment and to each `gh` for itself (`tests/test_ci_pins.py` lists
every way to hand it out, with a change of the real workflow for each). The bsc pin moves only
to a release whose tarball has a digest that GitHub publishes, and the download must have that
digest and the size GitHub lists; without a digest the pin stays, and the run's log and the pull
request say "pin it by hand". A pin that stays because main was rewritten (`behind`, `diverged`)
is a warning in the log, not "up to date". The workflow changes nothing on a fork or off the
default branch; it prints. Keep each
pin on the line the script reads: `tests/test_ci_pins.py` parses the real workflow files, and a
new pin needs a reader and a writer in the script. Dependabot (`.github/dependabot.yml`) keeps
the versions of the actions current.

Running flows manually:

```bash
xeda list-flows                      # all registered flows
xeda list-settings vivado_synth      # settings schema for a flow
xeda list-results vivado_synth       # result keys a flow writes to results.json
xeda design-schema                   # JSON Schema of a design file
xeda list-boards / list-platforms / list-optimizers
xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml -s clock.period=5.0 impl.strategy=Debug
xeda dse vivado_synth --design <file>  # parallel design-space exploration (Fmax search)
xeda scrub <flow> <design_name> [--target T]  # remove previous run dirs (every target's, or T's)
```

Flow runs land in the **run root**, `./xeda_run/` (configurable via `--run-root` / `XEDA_RUN_ROOT`,
API `run_root`); a **run directory** is one flow's. The exact layout depends on `--hashed-run-dirs`:

| options | path |
| --- | --- |
| *(default)* | `<design>/<flow>/` |
| `--hashed-run-dirs` | `<design>/<flow>_<16-char run hash>/` |

A dependency's run directory is a **sibling** of the flow that launched it, in the same layout,
never nested under it. `--hashed-run-dirs` names a directory by the flow's run hash (`flowrun_hash`:
its input settings and the ordered origins of its declared inputs, so a consumer moves when its
producer's settings do), never by a source file's content, so editing a source never moves it. The old `<design>_<design_hash>/` layer (dropped with
`--no-incremental`) is gone; delete such directories by hand. Xeda always reuses a flow's
directory across runs unless `--clean` empties it first (see "Caching and run directories"
below). Each run dir gets `settings.json`, `results.json` and `trace.json`, plus `reports/`,
`outputs/`, `checkpoints/`. Every run directory lies under the run root -- xeda created and marked
it (`.xeda-run-root`, `.gitignore`, `CACHEDIR.TAG`); keep nothing of yours there.

A run directory is `<run root>/<design>[/<target>]/<flow>` (or `<flow>_<hash>`) and nothing else:
`get_flow_run_path` refuses a design name that is not a name (`design.DESIGN_NAME`), a target that
is not a target name (`design.target_name_problem`) and a directory
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
target) at the path `RunDirectory.writable` located; a tool's log goes through `utils.live_log`
(renamed onto the name at once, then written as the tool runs), the one exception to
complete-then-rename (see "Tool execution").

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

In a `run` or `dse` document, `design` is always the design's name and `design_file` the design
file the request named (absolute; `null` for a project's design or none): `introspect.design_info`
builds both for every document -- success, failure, dry run, `--remote` -- from the request's
design argument and the name the launcher recorded as `design_name` (beside `target`, reset at the
start of each request). A failure before the design loads has no name to give for a file: `design`
is `null`, and `design_file` still says which file was named.

Every failure path must still emit a JSON document. That includes argument errors:
`XedaHelpGroup.main` runs click with `standalone_mode=False` when the invocation asked for
machine-readable output, so a `UsageError` becomes `{"success": false, "error": {...}}` on stdout
instead of a bare exit 2. Error documents carry `error.type` (a failure type, usually the
exception class name) and
`error.message`.

Flow names are resolved by `FlowChoice.convert` through `get_flow_class`, so every name the
resolver accepts works on the command line (canonical, CamelCase class name, aliases, dashes) and
an unknown name gets close-match suggestions. It returns the *canonical* name, so downstream code
never re-normalizes.

The documentation (`docs/`, Sphinx, `sphinx_book_theme`) enables no Sphinx extension: the pages are
hand-written reStructuredText. `docs/requirements.txt` lists only what the build uses; add an
extension only together with a page that uses it. `tox -e docs`, the CI `docs` job and Read the Docs
(`.readthedocs.yaml`, `fail_on_warning: true`) all build with warnings as errors, so a broken
`:ref:`/`:doc:` target or malformed markup fails the pull request. `conf.py` reads xeda's version
from the installed distribution, so every one of them installs the package too.

Note: the repository working tree accumulates untracked scratch output (`xeda_run/`, `sky130*/`,
`asap7/`, netlists, notebooks). Don't treat those as part of the source.

## Architecture

Four orthogonal abstractions, deliberately decoupled:

- **`Design`** (`design.py`) - *what* to build. Loaded from YAML/TOML/JSON (`Design.from_file`), with
  `rtl` (`RtlSettings`) and `tb` (`TbSettings`) sections, both subclasses of `DVSettings`. Sources become
  `DesignSource`/`FileResource` objects that carry a content hash; `design.rtl_hash` / `design.tb_hash`
  feed the run-directory hashing. Designs can also be fetched from a `GitReference` or produced by a
  `Generator` (e.g. `ChiselGenerator`), whose re-run decision is `generation.py`'s (see "A
  generator's re-run decision" below).
  **Targets** (`targets.<name>` in a design file, one per board) are overlays the loader applies
  before anything else sees the design: `Design.select_target(data, target)` works on the raw
  mapping -- the overlay takes the design's own keys (not `TARGET_FORBIDDEN_KEYS`), is folded by
  `process_compatibility(defaults=False)` (the design's own fold, so a key means the same in
  both), then `hierarchical_merge`d over the design, `rtl.sources`/`tb.sources` appended -- and
  records the name as `Design.target`, which no hash reads. `Design.from_file(path, target=)`,
  `XedaProject.get_design(name, target)` and the launchers' `target=` (`--target` on `run` and
  `dse`) all go through `Design.target_selected` (target, then `--design-overrides`). One target
  needs no selection; several without one, an unknown one, a name that is a flow's, and a
  written `target` key are `DesignValidationError`s at `targets...`. The oracle
  (`tests/test_targets.py`): a selected target equals the design written flat by hand, in every
  field, hash and dump but `target`. The design's own `flows` table is judged before a target is merged into it, and every
  target's overlay is judged at `targets.<name>.flows` whether it is selected or not
  (`design._flows_table`, the design validator's own function): the merge replaces a value that
  is no mapping by the overlay's mapping, and would otherwise hide a mistake in the table for
  that target alone. `design_schema()` adds `targets` to the input syntax only
  (`introspect._add_targets`); `send_design` leaves `target` out of the remote archive; plans
  carry it as `PlanContext.target`, which names where every node runs: a run directory is
  `<run root>/<design>[/<target>]/<flow>[_<hash>]`, the design's own `Design.target` passed as
  the `target=` keyword of `run_path_of`/`get_flow_run_path` (never read from the launcher, which
  may be reused), judged by `design.target_name_problem` at the path boundary and at load (a
  name, no flow's -- a removed flow's included -- and no two of a design's differing only in case), and `_validate_plan` refuses
  a plan for another target even when every hash is equal. The target is still no part of any
  hash: equal targets build separately and stay fresh separately; a pre-target run
  (`<design>/<flow>`) is neither reused nor touched by a target's launch. Shared leaves (`board`, `fpga`, `custom_boards_file`) at a target's top level are not
  accepted (refused, naming `flows.<flow>.<leaf>`: a top-level leaf has to reach every planned node that
  declares it, which needs the resolver to take a per-origin shared leaf, not a loader-time merge).
- **`Flow`** (`flow/flow.py`) - *how* to build. Abstract; concrete flows live in `flows/<tool>/`.
- **`Tool`** (`tool.py`) - an executable, runnable natively, in Docker (`Docker` model), or remotely.
- **`FlowLauncher`/`FlowRunner`** (`flow_runner/default_runner.py`) - orchestrates instantiation,
  dependency resolution, run-dir management, caching, and result reporting.

`xedaproject.py` handles multi-design project files (`xedaproject.yaml`, also accepted as
`xedaproject.yml` or `xedaproject.toml`), including top-level `flows` settings that get merged into
dependency flows. A project file is found by one helper, `resolve_project_file` (local and
`--remote` runs alike): the file given (it must exist; `""` is none given), else the sole one of
`PROJECT_FILE_NAMES` in the start directory; more than one, or a missing named file, is a
`ProjectFileError` (defined there, re-exported by `flow_runner`). Tests name project files through
`tests/project_files.py` (`PROJECT_FILE`, `TOML_PROJECT_FILE`), never a literal spelling.

### Flow lifecycle

`FlowLauncher.launch_flow()` is the one procedure every flow run and every producer run goes
through, make's order: bring every prerequisite up to date, then judge this flow against them. Its
stages (each a method; the docstring lists them): **input** (`_input_settings`: validate in
context, apply `--debug`) -> **identity** (`_run_identity`: design hash + `flowrun_hash`, run dir,
locked via `run_lock` until the trace is written) -> **prepare** (construct the flow with its own
*copy* of the input, `init()` (adds no dependency and reads no input) -- runs even for a flow that turns out
fresh, so it must not change a file in its run directory, all of which are outputs) ->
**producers** (`_run_producers`, following the plan; each in a sibling run directory held for
reading until the launch ends) ->
**prepare inputs** (`Flow.prepare_inputs`, after hand-over under producer read leases: register
implicit inputs before freshness and reserve them against delivery; preparation may materialize
board files in managed cache space, never write the flow's run directory) ->
**freshness** (without `rebuild_all`, the default: `trace.check_trace` against what the flow
would consume now; a match reuses the recorded results and skips **run** entirely; `_launch`
itself removes the trace here, before **run**, so nothing vouches for the directory from this
point on) -> **run** (`_launch` records every expected input as the run finds it,
`trace_inputs.snapshot_inputs`, and writes `settings.json`; then `_execute`: `run()`,
`parse_reports()`, `check_results()`) -> **report** (`_report`: artifacts, `results.json`; `_launch` then writes a
fresh `trace.json`, on success, once `_report` has returned).

**Every failure path after a run starts leaves a failure document**: `results.json` with
`success: false`, `error.type`, `error.message` and the run's identity -- a failing `run()`, and a
failing dependency, which the depender's directory reports too (a `FlowDependencyFailure` naming
the dependency and its `results.json`). A flow that fails with no exception and no tool exit
status -- its `parse_reports()`/`check_results()` said so -- gets `error.type = "ReportedFailure"`
and a message from `_execute` (a flow may set its own message first, as `openfpgaloader` does; a
real error is never overwritten), so a depender quotes something.
The two names are two roles, not one thing spelled twice (neither is an exception class; both are
strings in JSON documents): `FlowFailed` is the *verdict* at the top of the `--json` document of
`xeda run` when the requested flow itself ran without raising and did not succeed (a raised
failure names its class, a failed producer is `FlowDependencyFailure`); `ReportedFailure` is a
*cause*, in the node's `results.json`, for this one way of failing (a failing `run()` says its own
exception's name, a failing producer `FlowDependencyFailure`). Keep them distinct. The previous `results.json` is
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
  **origin first**: defaults < project < design < **target** < command line < API, each origin
  composed on its own (`compose_flow_settings`). **The selected target overrides the design, key by
  key**: it is the design's own author saying "for this target, these values", so
  its `flows.nextpnr.board` replaces the design's with no error, while a key it does not write
  stays the design's, and the project's own keys survive both. The loader folds the target into the
  design's mapping, so it is part of the design origin, below `-s` and the API
  (`tests/test_targets.py`, the layer-order tests). **This is not the agreement rule**: agreement
  is between two *nodes* of one graph (`yosys_fpga` against `nextpnr`) naming different values for
  a shared leaf, an error even when a target supplied one side; two *origins* contributing to one
  node are merged by precedence, never an error. **A flow's settings are written in one place, `flows.<flow>`**:
  `nextpnr.yosys` was removed and fails with "`yosys` was removed: use
  `flows.yosys_fpga.<key>`" (`Flow.Settings.removed_settings`; a `<key>` in a replacement names
  each key the removed value gave). **A producer's settings never depend
  on which consumer asked**: there are no consumer-given producer defaults, so `yosys_fpga`
  requested alone and as `nextpnr`'s producer is one configuration, one identity and one run
  (`yosys_fpga` keeps `src` attributes by its own default, since nextpnr's reports cite them).
  `vivado_postsynth_sim` and `vivado_power` are declared too: `synth` and `postsynthsim`
  were removed, and power's simulation controls with them. Each fails with its replacement:
  `vivado_postsynth_sim.synth` names `flows.vivado_synth.<key>`, `vivado_power.postsynthsim` and
  power's former simulation controls name `flows.vivado_postsynth_sim.<key>`, and
  `vivado_power.timing_sim` has none (power switches it on itself). `-s flows.<flow>.key=value` sets any flow of the run (the
  requested flow or one of its declared producers; an unknown flow is an error with
  suggestions); `-s key` and `-s flows.<requested>.key` are one setting (two values for it are an
  error); a `-s` that names the wrong flow suggests the right one. `--remote` follows the same
  rules. `-s` takes space-separated KEY=VALUE items and ends at the next option or the first
  token that is not KEY=VALUE (its key must look like a setting name), so it never swallows the
  design file; `--` ends the options. Local runs, remote runs and producers all use
  `merge_layers`. Declared edges agree shared leaves in the resolver. A producer's `debug`, and a
  `verbose` level above 1, carry over from its consumer (`carry_diagnostics`).
  **Every way to launch a flow takes the same layers.** `_request` (`run`, `plan`, `dse`) and the
  remote runner load the files and pass the project's and the design's sections to
  `FlowLauncher.resolve` as `origins`. A launch that is only handed a built design
  (`run_flow`, `launch_flow`, `Dse.run_flow`, a direct `resolve`) passes none, and `resolve` then
  makes the design's own `flows` sections (the target's folded in) its file origin, below the
  `all_flows_settings` it was handed and the settings: it used to ignore them, so a device
  written only in `flows.vivado_synth` failed `run_flow(VivadoPower, ...)` while `plan` named it.
  Only `run`, `plan` and `--remote` read a project file; a built design names none, so its
  caller hands the project's sections in.
  `tests/test_launch_origins.py` plans a design that writes its device in one section only, then
  resolves and launches it through every door (`run`, `run_flow`, `launch_flow`, `resolve`, the
  command line, `Dse`, and `--remote` in `test_remote_run.py`) and compares the nodes and their
  identities with the plan's.

- **There is one dependency mechanism: declared inputs and outputs.** No flow registers a
  dependency, nests another flow's settings or reads another flow's state: `add_dependency`,
  `resolve_dependency`, `completed_dependencies`, `pop_dependency`, `dependency_settings` and
  `copy_resources` are gone, and `tests/test_one_dependency_mechanism.py` fails if a name of them
  returns anywhere in the package. Every flow goes through the resolver, as a one-node plan when it
  declares nothing (`bsc`, `ghdl_sim`, `dc`, ...); there is no `is_declared` and no `declared`
  key in `list-flows --json` or in a plan node. OpenROAD declares its `netlist` input from
  `yosys.netlist` and an optional `sdc` input (the design's `Sdc` sources), and reads only
  `self.inputs` in `run()`; its platform copies and its
  own `merged.lib` are written in `run()`, so a fresh launch writes no flow output. A flow's
  `required_settings` are the settings a model may not require (`dc`'s `target_libraries`): a
  layer holds only some settings, and validating one that lacks a required field fails, so
  `Flow.Settings` refuses a field without a default (see "Settings"). The
  launcher passes the producers a flow was handed from (`_run_producers`) to
  `trace_inputs.expectation`, which records their `run_id`s (`dependency_runs`). Tests find a
  flow's producers through `tool_utils.producers_of(runner, flow)`.
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
is not a lossless inverse (the removed `open_xc7` -> `OpenXc7` != `OpenXC7`), and relying on that
round-trip used to make it and `yosys_sim` unrunnable. A removed flow's names are refused by
`get_flow_class` before any lookup (`settings_layers.REMOVED_FLOWS`, `check_not_removed`:
"`open_xc7` was removed: use fpga_pack to build, openfpgaloader to program"), and so is its
section in any `flows` table -- a design's, a project's, `-s flows.open_xc7.*`, the API's --
by `merge_flow_sections`, the one place they are all merged; a section for a flow that is merely
unknown (a plugin that is not installed) is still left alone. That function also judges the
shape of every table by one rule (`utils.flows_table_problems`, which the design validator and the
project loader apply as well): the table and each flow's section are mappings, an absent one
(`None`) is empty, and text, a number or a list is a `FlowSettingsError` naming the key and the
origin, an empty list or text included (`-s flows=3`, `flows: []`); only code may give a section
as a list of `KEY=VALUE` text, as `-s` takes it. Naming one flow twice in a table (`ghdl` and
`ghdl_sim`) is a `FlowSettingsError` too. The malformed-input sweep feeds every structural kind
of value to a table and a section from the command line, the API, a design file, a target and a
project file. `xeda scrub` alone takes a removed flow's name
(`FlowChoice(removed=True)`): it only removes directories. `get_flow_class` normalizes dashes, retries
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

### Declared inputs and outputs, and the resolver

Declare files in nested `Inputs(Flow.Inputs)` / `Outputs(Flow.Outputs)` models with `In` / `Out`
(`xeda.flow`, implemented in `flow/io.py`); every field needs a `description`. The annotation is
cardinality: `Path` one, `Path | None` optional, `list[Path]` an ordered nonempty list (an input
list with `In(optional=True)` may be empty). `In` names accepted `SourceType`s and optionally a
canonical default `producer` and its `output`; `Out(enabled_by=...)` names a setting that enables
an optional output. A consumer switches a Boolean setting on; other settings need a valid
nonempty default or an explicit value. A deliverable switch (`vivado_synth`'s `bitstream`) gets
its conventional name, `outputs/<design>.<ext>`, the name the run writes when the setting names a
location: `Flow.enable_output(settings, name, design_name=...)` takes the design's name for it,
so one output has one name however it is asked for
(`test_one_dependency_mechanism.py::naming_problems`, which lists no exception:
`vivado_postsynth_sim`'s `saif`, which a consumer or a timing request asks for, is
`outputs/<design>.saif` too, and `VivadoSim.run` makes the directory; the reviewed tool-input
goldens still say `activity.saif`, which `REVIEWED_RENAMES` maps). The flow chooses
its output paths inside its run directory.

- **One plan drives execution.** `flow_runner/resolver.py` resolves effective settings, input
  origins, switched-on outputs, identities and paths before constructing flows. Settings access gives
  private copies; request context is protected. The launcher checks its internal plan against the
  design, original request and run-root policy, then executes producers first without resolving
  inputs again. External supplied plans are not a supported API. Declared `init()` adds no
  dependencies, reads no inputs and writes no files; pure `check_settings_supported` validates
  targets in planning. `yosys_fpga` declares `netlist` (`netlist_json`) and `netlist_edif` (a flat
  Xilinx synthesis only); `nextpnr` declares input `netlist` and optional output `config` (ECP5
  textcfg, iCE40 asc or Nexus/Xilinx fasm); `fpga_pack` declares input `config` and output
  `bitstream`; `openfpgaloader` declares input `bitstream` and no output. No flow of this graph
  nests a producer's settings.
- **Binding > design source > default producer.** An explicit binding -- a chain adjacency
  (`chains.parse_request`, `a+b`), or `flows.<consumer>.inputs.<input>: producer[.output]` in
  a design or project file, on the command line or through the API -- supplies the input first
  (`bindings.node_bindings`, called by the resolver for each node it reaches); a many input
  takes an ordered list. Otherwise an accepted type in `rtl.sources` supplies it, in source
  order (a `JsonNetlist` skips `yosys_fpga` for `nextpnr`); otherwise the declared default
  producer. Cardinality is checked. A bound input is never pruned by a matching source, and a
  displaced default producer leaves the graph: its settings are unused and take no part in
  shared agreement. Planning logs each configured section of a default producer the run does not
  include, once, whether a source or a binding displaced it (`resolve`'s loop over the unused
  default producers, which also checks their syntax). `inputs` is reserved wiring split out per origin
  before settings composition (`bindings.split_bindings`), never a `Flow.Settings` field or
  part of the design hash. A chain is command-line data: it overrides a file's binding of the
  same input (the plan reports it as `overridden`) and is an error, even when equal, against a
  command-line or API binding of that input. A chain adjacency and a binding judge an
  edge by one predicate, `chains.fitting_outputs` (the types an output can make are a nonempty
  subset of those the input takes, and a many output feeds only a many input). `--remote` and
  `dse` refuse chains and bindings their request reaches; a binding saved for another flow is
  not a refusal. On the command line, `xeda run a+b+c design.yaml` (`cli_utils.ChainChoice`, the
  canonical request text; `FlowChoice` keeps one flow for every other command and says a
  chain is for `xeda run`): the top-level `flow`, results, `--help-settings` and exit status
  are the last flow's; `--json` adds `request` (`introspect.request_info`) and per-node
  `node`/`inputs` (`introspect.inputs_info`), and after a failure the planned nodes never
  entered as `"state": "not run"` (from `FlowLauncher.last_plan`, the plan the run followed).
- **A chain is validated, suggested, listed and completed by one predicate.**
  `chains._check_chain` judges a request (action last, repeat, edge) and
  `validate_chain` appends `Did you mean ...` with whole corrected requests that pass the same
  check: another output of the producer, or stages inserted along required default-producer edges
  (`_default_routes`, bounded and breadth-first; never a search over all flows, never a
  construction of a flow). `edges`/`followers`/`predecessors` are the
  same relation for `list-flows` (`can_follow`/`can_precede`/`target_dependent`, and the
  `required`/`optional` of each input) and for shell completion (`chains.complete_request`,
  `ChainChoice.shell_complete`: the prefix returned as typed, nothing offered after an action,
  a repeat or a flow that declares nothing). `Flow.action_reason` (class metadata) is why a flow can only
  end a chain; the dry run prints it without calling `always_runs()`. A binding naming an input of
  a flow that declares none says so (`bindings.node_bindings`), which is also the tripwire:
  `tests/test_fpga_chains.py`'s refusals of `bsc`/Vivado chains and of `inputs.design`
  must be inverted the day those flows declare I/O.
- **Chain documentation is executable** (`tests/test_chain_documentation.py`). The YAML
  fixtures in `docs/flows.rst`, `docs/design-file.rst` and the packaged agent docs begin with a
  `# <name>.yaml` comment; the test writes each as that file, loads it with `Design.from_file`,
  checks it against `introspect.design_schema()` and its flow sections (`inputs` split out first,
  as the launcher does) against the flows, and runs the documented commands against the real
  FPGA flows (planning needs no tool; execution uses `tests/fake_tools`). New configuration
  snippets are YAML with a `.yaml` name. A test that enumerates `registered_flows` must scope to the product's
  flows (fixture flows leak globally through `Flow.__init_subclass__`).
- **One node per producer, keyed by node identity** (`bindings.NodeKey`, never the flow name
  alone): the resolver reaches each node once, unions every consumer's demand on its outputs
  before its settings are frozen and hashed, and the launcher's completed-run cache is keyed
  the same way, so a producer feeding several inputs or branches runs once. An input's
  `ResolvedInput.references` are its ordered `(node, output)` producers; `binding_origin`,
  `binding_location` and `overridden` only explain.
- **One identity rule.** `bindings.node_identity(settings_hash, origins)`: a node's hash
  (`PlanNode.flowrun_hash`, `flow.flow_hash`, `results.json`'s `flow_hash`, the trace's
  `flowrun_hash`, the hashed directory suffix) is its settings-only hash
  (`flow.flowrun_hash(...)`, kept as `settings_hash`) plus its ordered input origins
  (`bindings.input_origins`: each a producer's identity and output key, or `"source"`), for
  default and explicit edges alike; a flow without a plan node has no origins. The resolver's
  freeze, `_validate_plan` (which recomputes every node and checks the plan's bound inputs
  against the request's bindings), `_run_identity`, claims, results and traces all use it. A
  producer's settings change therefore moves its consumers' identities; where a binding was
  written does not. The trace (`TRACE_FORMAT` 14) records each input's ordered producers
  (`DeclaredInputRecord.references`) and its explanatory origin, and `trace.changed_binding`
  names the reason: "netlist now from __synth.netlist (was yosys_fpga.netlist)", or the
  producer that "has other settings or inputs than in the last run".
- **Shared leaves agree along declared edges.** `fpga`, `board`, `custom_boards_file`,
  `clocks`, `prjxray_db`, `platform`, `corner` and `dont_use_cells` apply where both endpoints
  declare them. Each contribution carries the value it propagates and a key it is compared by
  (`resolver._normalized_leaves`): the same for every leaf but `platform` -- indivisible,
  propagated exactly as given, compared by a location-free projection of its validated model
  (`_platform_key`) -- and `corner`, compared by the corner it selects. Disjoint leaves combine; conflicting values fail with
  both nodes and their real file/section origins. Explicit CLI leaves (`-s key` or
  `-s flows.<node>.key`) override those leaves for the connected group, preserving unrelated
  leaves; API contributions remain a separate highest-precedence origin. A board-aware node's
  `fpga` is derived from its agreed board before `fpga` is agreed (`agree_targets`; the derived
  leaves rank as the node's own board does and are located where the winning board was written,
  `_agree` returning each agreed leaf's origin), so a bundled board alone is the device of every
  node that shares `fpga` with it: `tests/test_board_device.py` sweeps every flow that declares
  `board`, every origin and every position in a chain or default graph. A device written for a
  flow that is not part of the run reaches no node -- a design-wide device is not a feature yet
  -- and the error that a node lacks a required setting names each section that gives it without
  reaching the node (`resolver._unreached`, passed to `check_required_settings`).
- **Outputs are checked records.** `flow_runner/outputs.py` records enabled outputs in
  `results.json`'s `outputs` as `{path, sha}` (ordered lists for list outputs), after checking
  containment, readable files and `wrote_output`; failed output validation uses `MissingOutput`
  and the usual failure identity. An enabled nextpnr config missing after the tool exits raises
  `FlowFatalError` naming its setting/path. Hand-over validates schema, cardinality,
  containment and digest under a verified read lease for new and reused producers alike; reuse
  uses the producer's trace, not current-run write evidence. Declared flow code reads only
  `self.inputs`, never producer settings, directories or artifacts. Traces also record ordered
  declared input names, origins, producer identities and paths; a changed binding invalidates reuse.
- **Planning is read-only.** `FlowLauncher.plan` / `xeda run --dry-run` prints this plan (or JSON
  via `introspect.plan_info`) without tools, run-root changes, markers, locks or deliveries.
  Loading that needs a generator or Git fetch is refused before side effects; a materialized
  `Design` is plannable.
  Freshness is not evaluated, and `--remote` is refused.

Declared flows may narrow input/output types by effective target settings and enable a selected
optional output when a consumer requires it. **An output a consumer switches on changes its
producer's settings, hence its identity**: a request that demands it and one that does not
re-run the producer in turn. So a cheap output is always written, with no switch: `nextpnr`
always writes its `config` (`textcfg`/`asc`/`fasm` name the file and cannot be empty; only an
ECP5 `out_of_context` run has none), and running `nextpnr`, then `fpga_pack`, then `nextpnr` again runs
nextpnr once. Keep `enabled_by` for genuinely expensive outputs. nextpnr selects typed pin constraints by family and
merges typed SDC sources before its `sdc` setting's file. Board fallback is prepared before
freshness; duplicate clock constraints across files and settings fail with their origins.

**Xilinx 7-series goes through openXC7 1.0** (`flows/xilinx.py`, `nextpnr.py`, `fpga_pack.py`):
`nextpnr-himbaechel --device <part as the Project X-Ray database spells it> --chipdb ... -o
xdc= -o fasm= -o placement=`, never `--freq`/`--xdc`/`--fasm`; flow clocks become `create_clock
-period` lines (no `-name`: the backend ignores it with a warning). Tool data is found from the
resolved executable's prefix (`share/nextpnr/himbaechel`, `share/nextpnr/prjxray-db`), never
from an environment variable. `Nextpnr.prepare_inputs` prepares the die's chip database in
`<run root>/.cache/xilinx-chipdb/<identity>/` (`xilinx.prepare_chipdb`: identity by content of
the executables, generator tree and device data; one durable lock per identity; generation in
sibling scratch, validated, renamed; entries immutable and left by scrub; `chipdb` names a file
instead) and registers it as an implicit input, so a hit starts no generator and an unchanged
relaunch runs nothing. Generation and nextpnr's hooks run with `PYTHONDONTWRITEBYTECODE`: the
installation is never written. `lut` is per toolchain and stage (`LUT:STAGE`, `LUT:METHOD`):
nextpnr's is the distinct `(tile, site, A-D)` locations of `SLICE_LUTX` cells in this run's
placement dump, yosys's a mapped-primitive footprint (`yosys_fpga.xilinx_lut_footprint`, every
`RAM<d>X<w>[SD]`, SRL and LUT primitive); neither is certified comparable with Vivado's.
`clock_port` is reported only when the reported domain is itself a top-level port. A failed
nextpnr is reported by the `ERROR:` lines of this run's log (`Nextpnr._failure`: a constraint
error at its origin, a missed timing constraint, else the tool's failure, a `NonZeroExitCode`
whose message ends with the first `ERRORS_SHOWN` distinct error lines: `NonZeroExitCode`'s extra
arguments are shown after its exit code) -- a warning is never the cause. `fpga_pack` packs into `.xeda-pack-*` scratch in its run directory (removing one a
killed run left) and publishes with `replacing_copy` only a nonempty file of a packer that
exited 0. `fpga-as` is given the part's own Project X-Ray directory (`<family>/<part>/part.json`)
when the database has it; else the directory of the lowest speed grade of the same device and
package (`xilinx.locate_part_data`: smallest grade number, a plain grade before its `L` variant,
never another device or package), logged at info level naming both parts. A device and package
are one die with one pinout, so their grade directories are expected to agree and `nextpnr` keeps
the exact grade for timing; a few groups of the installed database do not agree (`part.json`,
`package_pins.csv`, or `required_features.fasm` for some `clg400` grades), so the lookup warns when
the other grades' files in `PINOUT_FILES` (`part.json`, `package_pins.csv`) differ from the chosen
one's. The bitstream's header, which `fpga-as` writes, names the stand-in part. With no directory
of the package at any grade it raises a `FlowFatalError` naming the part, the directory searched
and the other packages' grades. `FpgaPack.init` does all of this, so the error comes before any
producer runs. Known limit: an in-place change of the installed Project X-Ray data alone, with
`fpga-as` unchanged, is not noticed when packing a prebuilt `Fasm` source, and neither is a new
directory for the exact part (a tool's own installed files are never flow inputs; `--rebuild-all`
packs again). A `prjxray_db` the user sets is tracked as a setting's directory, as before.

**openXC7 `nextpnr` builds before `26f5e17a5` (main, 2026-10-07) can write wrong bits with no
error.** The 1.0 release and `3e5c2cdd` (the installer's pin when this was written) have none of
the fixes, all merged between 2026-10-05 and 2026-10-07, each checked only against Vivado's
bitstream ("Not validated: silicon"). By openXC7/nextpnr pull request: 66 an inferred `DSP48E1`
multiply ignores its A operand (issue 39); 70 an initialized `RAM32M`/`RAM64M` is all zeros
(`pack_dram.cc` read `INITA` for `INIT_A`), and a falling-edge `SRL16E`/`SRLC32E` shifts on the
rising edge; 69 `INIT_A/B` and `SRVAL_A/B` of a block RAM are ignored; 72 an `ODDR` on a
tri-state T input is unregistered, and a missing `ODDR` `INIT` starts high; 68 MMCM/PLL registers;
71 a `TMDS_33` input is programmed as `LVDS_25`, `LVCMOS33` `DRIVE 16` as 12 mA, and the `IDDR`
Q3/Q4 starts are wrong; 67 cascaded `RAMB36E1` pairs (64K x 1 and deeper); 78 high-performance
bank pads driven by an `ODDR`/`OSERDESE2`. `26f5e17a5` contains all of them and `3e5c2cdd` none
(`gh api repos/openXC7/nextpnr/compare/<a>...<b>`). `yosys_fpga+nextpnr+fpga_pack` is hit, and
none of it shows in timing or utilization. `docs/flows.rst` and the skill's
troubleshooting say what to use. No code checks the build: raise the floor once an openXC7 release
has all of them. `nextpnr` also ignores `set_property PULLUP true` without a warning and reads
`PULLTYPE PULLUP`; the bundled pin files use neither.

**Vivado implements the netlist `yosys_fpga` writes** (`xeda run yosys_fpga+vivado_impl`;
`flows/vivado/vivado_impl.py`). `yosys_fpga` declares `netlist_edif` (`Edif`, no `enabled_by`, as
`nextpnr.config`: a consumer switches nothing on, so `yosys_fpga+nextpnr` and
`yosys_fpga+vivado_impl` are one yosys identity and one run) and writes it, `write_edif -pvector
bra`, whenever `YosysFpga.Settings.edif_problem()` is None: a Xilinx target, flat as far as the
settings show (`effective_flatten`; no `keep_hierarchy`, no `keep_hierarchy` attribute in
`set_mod_attribute` or `set_attribute`, no `black_box`), not `stop_after: rtl`. Otherwise
`output_types` is `()` and `enable_output` raises the problem's text, so planning refuses the
consumer naming the setting (`flatten`, ...); a non-default `netlist_edif` where none is written is
refused too. What no setting shows (a `(* keep_hierarchy *)` in the HDL, `rtl.attributes`, a listed
`.edf`) is in the netlist: `VivadoImpl.refuse_a_hierarchy` reads it before Vivado starts
(`xeda/edif.py`: an instance that refers, through an external library, to a module the netlist
defines -- how yosys writes a hierarchy) and fails naming the modules. The reader streams the file
in chunks and keeps only the distinct cells and `(cell, library)` pairs, so its memory does not
grow with the instances (`tests/test_edif.py` measures it with `tracemalloc`; the module doc lists
what it holds, and the limits that bound it for text that is no EDIF). The synthesis itself never
fails for a hierarchy: `yosys_fpga+nextpnr` places one. A black box the settings do not make (the
HDL's, `verilog_lib`'s) looks like a primitive in the file, and Vivado stops on it. The four
hazards of a yosys netlist in Vivado are each excluded by construction
(`tests/test_vivado_impl.py`; only the reversed bus gives no message at all, a hierarchy and a
misnamed file fail loudly, and block RAM `x` bits in a Verilog netlist draw one critical warning):
the `-pvector bra` in `write_netlist.{ys,tcl}`; no EDIF unless flat; the netlist staged as
`<rtl.top>.edif` whatever it was called (Vivado finds the top by the file's name; the fake
`link_design` has the same rule); `netlist` takes `Edif` only, since a Verilog netlist drops block
RAM contents with `x` bits. The script is `vivado_impl.tcl` (non-project: `read_xdc`, `read_edif`,
`link_design`, `opt_design`) plus `implementation.tcl`, the tail it shares with
`vivado_alt_synth.tcl`: its `write_checkpoint`/`write_netlist`/`write_timing_netlist` blocks are
`is defined` guards, since `vivado_impl` has no such settings (its only output is `bitstream`, until
stage-typed checkpoint and netlist types exist), and the includer makes `reports/post_place`
(Vivado makes no directory for a report; the fake Vivado fails as it does, `[Common 17-37]`).
`VivadoImplementation` (`vivado_synth.py`) holds what `vivado_synth`, `vivado_alt_synth` and
`vivado_impl` share: the implementation settings, the bitstream's `enable_output` and the timing
and utilization parsing. Real Vivado: `tests/test_vivado_real.py` (`XEDA_TESTS_VIVADO=1`).

Use YAML for new examples, designs, project files and Xeda configuration data. The bundled boards
and platform databases are still TOML; a custom board database (`custom_boards_file`) may be TOML
or YAML, chosen by its suffix and read through the strict loader (`board.read_board_database`).

### Settings

Every flow declares a nested `class Settings(<Base>.Settings)`. Settings are pydantic models
(`XedaBaseModel`) with `extra = forbid`, so an unknown key in a design/CLI override is a hard error -
this is intentional and surfaces as `FlowSettingsError`. CLI `-s key=value` supports dotted
hierarchical keys, which every origin expands with the one function `utils.set_hierarchy`. A key is
a value or a table, never both: `-s timing=true timing.x=1` and `-s timing.x=1 timing=true` are a
`ConflictingKeys` naming both keys, a `XedaException` (the command line reports it) and a
`ValueError` (a validator turns it into the field's error). `design.from_file` and a target's
overlay report it as a `DesignValidationError`, and `dse` as its error document.
`tests/test_key_hierarchy.py` pins the function, and the malformed-input sweep feeds such a pair
to every origin.

**A setting accepts exactly its declared type; there is no implicit conversion.** A number is not
text (`compile_args = ["-j", "8"]`), text is not a list, `True` is not a name. Where a setting's
values really are of several kinds, its type says so and whatever consumes it renders each kind
explicitly: Vivado run properties are `str | int | float | bool`, rendered for Tcl by
`tcl_property_value`. Text that is naturally written as a number is declared per field with the
`Code` type (`xeda.dataclass`): an FPGA's `speed = -1`, `grade`, `generation`. **A `bool` or
`Optional[bool]` field -- of a flow, the design or any nested model -- given text or a number is
an error, whatever it says**, except exactly the text `true`/`false` (what the command line's `-s`
can write; a quoted `"true"` in a file is accepted too, a known consequence): `yes`, `on`, `y`,
`1` are not booleans (`XedaBaseModel._booleans_are_not_text`, one `before`
validator every model shares, reporting per field: `` `yes` is text, not a boolean: write `true` ``).
`dataclass.validation_errors` adds what to write where a YAML 1.1 word (`ncpus: on`) or a number
(`top: 010`, which YAML 1.2 reads as 10) reaches a field that is neither. Never add a lax
conversion back; `try_convert_to_primitives` converts only `true`/`false` for the same reason.
On top of that, `Flow.Settings._normalize_flow_setting` gives every *flow*
setting two conveniences, applied before any field validator runs (by a model `before` validator
on construction and reload, by `Flow.Settings.__setattr__` on assignment):

1. A list setting given as text is comma-separated (`-s xdc_files=a.xdc,b.xdc`; spaces around
   items and empty items are dropped, so `""` is `[]`). A setting that also accepts plain text
   keeps it whole. Text spelled `[...]` is refused, naming `key=` (the empty list) and `key=a,b`
   (`dataclass.comma_separated_items`): `-s flags=[]` used to become the one item `"[]"`.
   Construction reports it as the field's `list_text` error, and an assignment raises the same
   `ValidationError`, so the two cannot differ (`settings_samples.PROBES` holds `[]` and
   `[x, y]` for the sweeps). It is the one rule for a list given as text, so a model nested in a
   flow's settings that takes text for a list calls it too (`CocotbSettings.testcase` and
   `gpi_extra` do) and reports the refusal as `ListLiteralText.validation_error`;
   `validation_errors` words it with the key from the top (`cocotb.testcase=`, the spelling to
   write after `-s`). `tests/test_settings_input.py` walks every list field of every nested model
   and fails one that takes text by another rule.
2. `$PWD`, `$DESIGN_ROOT`, `$DESIGN_DIR` are expanded at every `Path` leaf of the annotation
   (`_expand_path_values`): scalars, `str | Path` unions, list/dict/tuple elements -- in
   `lib_paths` only the path half of each tuple, never the library name.

**The variables and the forms of a location are defined once**, in `utils.py`: `PATH_VARIABLES`
maps each variable xeda gives a setting's path to the validation-context entry holding its value
(`path_variables(context)` is what `Flow.Settings._path_roots`, `Flow.process_path` and
`custom_boards_file` expand with; any other variable comes from the environment), and
`LOCATION_FORMS` (`$PWD/..., $DESIGN_ROOT/..., $DESIGN_DIR/... or an absolute path`) is what every
message that says how to give a location names. A new variable goes into the table, never into a
message. `tests/test_written_paths.py` checks each named form is absolute once expanded, and
`tests/test_delivery.py` delivers a deliverable given each form.

**Values derived from settings are computed where they are used, not stored in settings.** Yosys's
`write_verilog_flags()` / `attributes_to_unset()` read the `netlist_*` switches when the script is
rendered, so a switch set later (as `Yosys.init` does for `netlist_expr`) still takes effect.
The Vivado step tables follow it: `synth_design_options` (`vivado_alt_synth`) and `run_steps`
(`vivado_synth`, `vivado_project`) return copies that hold what the settings derive
(`-mode out_of_context`, `flatten_hierarchy`, the step hooks), and `expand_run_options` copies
the module's strategy table, so a run writes into neither its settings nor a table that the next
run of the process reads (`tests/test_vivado_step_tables.py`). The same goes for anything that
depends on several settings: `Flow.Settings.is_quiet` is `quiet` unless `verbose` or `debug` is
set. A flow setting's *field* validator never reads another
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
the model, and never checked in `run()`. The rule is enforced, not a habit: a `Flow.Settings`
field without a default (`Optional[X]` with no `= None` included) is refused with a `TypeError`
when its class is defined (`Flow.Settings.__pydantic_init_subclass__`; the message names the
field and points here), so no settings model of any flow, a plugin's included, can require a
field. The reason is that a layer (one origin's `flows.<flow>` section, one shared leaf) holds
only some of a flow's settings, and the resolver validates such a part with the flow's real
model before the layers are composed; a required field would fail every part that lacks it. The
field takes a default (`None`, or an empty value) that its validators handle, as `validate_default`
makes them. `FpgaSynthFlow` requires `fpga`; `tests/test_required_settings.py` sweeps every FPGA
flow and every registered flow's model, and `tests/test_required_model_fields.py` shows each way
of giving a required setting (file, project, `-s`, API mapping, instance, section) and the error
when it is given nowhere. What the given settings need is
`Flow.required_settings_for(settings)`, by default `required_settings`, which the launch check and
the resolver's unreached-section note both read. It decides the requirement from the other
settings and is still never the model: `openfpgaloader`'s `required_settings` is empty, and its
hook adds `fpga` only with `write_flash` (openFPGALoader programs a flash through a bridge made for
the part; loading SRAM it detects the device, and the flow passes `--fpga-part` only when the part
is known).

**Every settings field must have a `description=`.** `tests/test_documentation.py` fails otherwise
(its allowlist is empty - all ~520 visible fields are documented). The same test requires each flow
to have its own docstring (not an inherited base-class one, which `xeda list-flows` would show)
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
not the achieved one. `Fmax` is also per tool: nextpnr's and Vivado's timing models differ (one
design with block RAM and DSP paths: nextpnr 429 to 453 MHz, Vivado 232 to 241 MHz, for the same
netlists), so compare it only within one tool.

Declared output records are bookkeeping, like `artifacts`, not `COMMON_RESULT_DESCRIPTIONS`
keys; they are omitted from the printed result table. See `docs/machine-readable.rst`.

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

`run_process` and `Tool.run` take `timeout` (seconds; on expiry the process -- on POSIX its whole
process group -- is stopped and `ProcessTimeout` raised; a Docker container is named and
`docker kill`ed) and `tee` (a file the output is also copied to).

**Every tool log goes through `utils.live_log`**: `tee`, and `stdout=<path>` (`Tool.redirect_stdout`:
Vivado's `<flow>_stdout.log`, DSE). It makes a temporary file beside the name, `os.replace`s it onto
the name at once, **before the tool starts** (a log that cannot be made starts nothing), and writes
every line to that file's own descriptor, line-buffered, so `tail -F` follows a long run. The atomic
replace is what keeps a link at the name -- symbolic or hard, whose inode may be a file outside the
run directory -- from ever being written through: it is replaced as a name, never opened by name for
writing. A failed or timed-out tool leaves the part of its log it wrote. It is the one write that is
not complete-then-rename, since a log is meant to be watched; add no other.

A dockerized tool runs with `--security-opt label=disable` and every mount exactly as given (never
`:z`), so xeda never relabels the user's files; a mount that wants relabeling says so in its own
`Docker.mounts` value.

**A tool that wants a terminal asks for one** (`Tool.pseudo_terminal`, a class constant, true for
`OpenfpgaloaderTool` only). Through a pipe such a tool prints no colors, and its progress bar, drawn
with `\r`, becomes a line for each update with a blank line between (`run_process` reads the pipe
in universal-newline mode, where a lone `\r` ends a line; changing that for every tool would put
`\r` into every log). `run_process(terminal=True)` -- passed by `Tool.execute` when the tool asks
and `console_colors` is on, native runs only -- gives the child a pseudo-terminal (`os.openpty`)
in place of the pipe, but only when there is a `tee` log and xeda's own output is a terminal
(`_stdout_terminal_fd`): in a pipe, a file, CI, `--json` captured by an agent, nothing changes.
`_run_in_terminal` copies the bytes as they arrive to `tool_output_stream()` (colors, in-place
redraws), and to the `tee` log without escape sequences, one line per redraw, no empty lines; the
time limit, the stop hook and the exit status are those of the pipe, and `highlight_rules` do not
apply. The loader's verdict reads the log, so it is the same either way
(`tests/test_proc_utils_terminal.py`, with real pseudo-terminals).

Instantiating `Tool(...)` inside a flow method auto-discovers the calling `Flow` via `inspect.stack`, so
it inherits `dockerized`, `print_commands`, and console-color settings and appends its version info to
`flow.results.tools`. Subclass `Tool` to pin an executable, a default `Docker` image, `minimum_version`
(`Tool.require_minimum_version` raises a `ToolException` that names the version found and the one
needed, with `minimum_version_reason` when the subclass gives one; `derive` runs no constructor, so
a flow that derives a tool with a floor calls it),
and `highlight_rules` (regex -> ANSI, used to colorize tool output) - see `VivadoTool`. Use
`tool.derive("other_exe")` to spawn a sibling executable from the same image/config. **A query
about the tool itself (its version) goes through `Tool.probe_stdout`**, which runs it in a
temporary working directory: a flow creates tools in `init()`, in its run directory and before the
freshness check, where every file is an output, and `vivado -version` writes vivado.jou and
vivado.log where it runs (the fake `vivado` does too).

### Caching and run directories

A run is identified by `design_hash` (`Design.parts_hash(Flow.design_parts)`: the `rtl_hash` of
every flow and the `tb_hash` of one that reads the testbench -- each source's content hash,
type, `standard`, `variant`, and its position in the source order, plus behavior-affecting
RTL/testbench metadata) and the run's hash: `flow.flowrun_hash` (flow name + input settings)
combined with the ordered origins of its declared inputs (`bindings.node_identity`; the
settings-only hash is kept as `settings_hash`). Both are semantic --
they depend on what the inputs mean, not where anything is: moving a whole design never changes
`design_hash`. Every source counts by its path relative to the design root, outside it too
(`../lib/defs.vh`, `Design._source_fingerprint`): a tool can resolve another file from any
source's location, so re-arranging even VHDL or constraint sources changes the identity.
`send_design` keeps sources under the root at their relative place for the same reason.
**`design_parts` is a flow's *direct* scope**: a testbench edit leaves `vivado_synth` and `yosys`
fresh and makes `ghdl_sim` stale, but a `{"rtl"}` flow downstream of a producer that reads `tb`
still runs again with it (the dependency's new `run_id`). It scopes the design hash and the trace's
design files and nothing else: the launcher's registered reads and refused inputs
(`_read_inputs`, `_refuse_inputs_inside`), a remote run's read inputs and `PlanContext.design_hash`
keep the whole design, since they protect the user's files and verify the request. A part wrongly
left out is a stale reuse, one wrongly left in a run for nothing, so a flow that reads `tb` in code
or a template declares it (`SimFlow`, `bsc`, `vivado_project`); `tests/test_design_parts.py`
scans every flow's classes and the templates it renders. `flowrun_hash` writes any path under the design
root or the start directory relative to it (`$DESIGN_ROOT/c.xdc`), and one under xeda's own
installation relative to that (`$XEDA/platforms/...`, `utils.location_roots`: a bundled
platform's files are the same on every installation) -- each root and each path recognized as
written and as resolved (`location_free`), so a place reached through a symbolic link counts the
same either way; a shipped file's identity on a remote is keyed by its resolved path
(`flow.using_path_identities`), since the remote resolves its design root -- the start directory and design
root are validation *context* rather than settings. A parameter's value is its only record: one given as a file
(`{ file = ... }` or `{ path = ... }`) becomes the absolute path the tool is handed, and a path
under the design root counts relative to it (`location_free`, the `flowrun_hash` rule; one outside
the root counts as the location it names); its content is never hashed. `send_design` re-roots such a path for a remote from the value alone. Settings paths count as text; only design sources are read
for content, and no directory's content is ever hashed. The xeda version is deliberately not part
of the hash. Local and remote runs share `flowrun_hash`.

**Runs are make-like by default.** A flow re-runs only when something it consumed or produced
changed since its last successful run; `--rebuild-all` (API `rebuild_all=True`) runs every flow.
By default there is one directory per flow; `--hashed-run-dirs` (`hashed_run_dirs=True`) gives
one per run hash (`<flow>_<flowrun_hash>`: settings and input origins) -- dependencies are always siblings in the same
layout, never nested. A flow that runs logs why (`log.info("Running %s: %s", flow.name,
flow.stale_reason)`); a fresh one logs that it is up to date and its recorded results are shown as
if it had just run.

An option takes a value only when the value is data (a directory, a host); a behavior switch is a
flag. `run`, `dse` and `scrub` read only the environment variables they declare
(`DeclaredEnvvarsCommand`: `XEDA_RUN_ROOT`, `XEDA_DEBUG`, `XEDA_REMOTE`, `XEDA_LOG_LEVEL`,
`XEDA_DETAILED_LOGS`), never an automatic `XEDA_<OPTION>`: a leftover `XEDA_CLEAN=1` would empty
every run directory on every run. `tests/test_option_names.py` is the oracle: each launcher option
of `run` is named as the setting it sets, none is a choice, and no two names differ by a trailing
`s`.

`trace.py`/`trace_inputs.py` implement this. `trace.json`, written into the run directory last and
atomically after a successful run (`write_trace`) and removed before the next run executes
(`remove_trace`), is what makes a directory's freshness self-certifying: its mere presence means
"the last run here completed and succeeded". It records
`flowrun_hash`, `design_hash`, `xeda_version`, a digest of every file of the installed xeda package
(`xeda_code_digest`, once per process: an editable install keeps its version across edits) and of a
plugin flow's own modules (`flow_code_digest`), the programs it started as `FileRecord`s of the
resolved executable (`ProgramRecord.file`: size, mtime, inode change time, inode, content hash --
a program is checked exactly as any other input, and a successful run keeps the previous
trace's record of an unchanged program (`trace_inputs._programs`: `FileRecord.trusted` against that
trace's `outputs_recorded_ns`, never an `unknown` record) instead of hashing the binary again; a
container image is recorded by its ID alone, `ProgramRecord.path`), and every file as a `FileRecord` (`size, mtime_ns, ctime_ns, inode, sha`,
`digest.record_file`): its **inputs** -- the design's files (`design_files`: one walker over the
parts the flow reads, `rtl` and, for `design_parts` with `tb`, `tb`, so a file-valued parameter
counts), every existing file a path-typed setting names
(`setting_files`: nested models too; relative paths under the design
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
entries, `yosys -E`, and files `run()` registers in `Flow.implicit_inputs`, such as a
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
location alone**: every run directory lies under the run root now, and is the run's
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
the run root, the `run_id` of the dependency run it consumed (`dependency_runs`). `trace.check_trace` re-derives all of this
(`trace_inputs.expectation`)
and returns the first mismatch as the stale reason. A file counts as unchanged by its metadata when
`FileRecord.trusted` says so -- size, mtime, inode change time and inode all equal, and the later of
mtime and ctime more than 2s before the record was taken (racy timestamps) -- otherwise by content
hash, so a `touch`, `chmod`, `cp -p` or a branch round-trip costs a hash, not a re-run, while an
edit given back its old mtime is still caught (its ctime moved). A check reads the file-system clock
only before it first reads a file's content, and refreshes the trace (`Freshness.refreshed`) when a
record it read changed or has settled since (`FileRecord.settled_before`), so a racy file is not
hashed at every later check; in a read-only run directory it checks without refreshing.
A fresh launch may therefore rewrite `trace.json`: a test that claims a reuse left a run directory
alone compares `tool_utils.run_outputs_state` (every entry but the reserved names), never a listing
of the whole directory, and forces the refresh with `tool_utils.check_after_the_racy_window`
rather than hoping the machine is slow enough. A dependency
that ran again always makes its depender stale too, even if nothing it declared as an input actually
changed -- there is no cross-edge cutoff on an unchanged output. What is not tracked
(each can make a stale result look fresh; `--rebuild-all` is the escape): what a symbolic link in a
run directory points to, when that is a directory (the link is recorded by its target, never
followed); programs started indirectly (a compiler under `make`, Python packages such as
cocotb); environment variables; files a tool finds on its own without reporting them. A hand edit of
any file a dependency's run left, or a file added there, makes the dependency stale, and its
dependers follow through its new `run_id`.
Pin constraints fetched from a URL are not verifiable either, so a flow using them always runs.

**A generator's re-run decision is content-based too** (`xeda/generation.py`), although it is
made at *design-load* time, before any flow, run directory or trace exists. `process_generation`
asks `judging_generation`, which hashes the generator's configuration **as the design states it**
(never the working directory and whole environment the loader completes it with: a record must be
reusable from another shell, and xeda tracks no environment variable), the content of every file
of `generator.sources` (a directory counts as every file in it, outside the design root too: a
library tree, an editable clone) and the selected direct executable. **A generator is an external
tool, resolved through `PATH`: xeda assumes nothing about its language or environment**, so there
is no `packages` field and no interpreter lookup -- what it reads is what `sources` names, and
what xeda cannot see (a package upgrade) needs `--rebuild-all` or `always_runs`; a generator that
writes into a directory it reads (`__pycache__`) keeps no record on its first generation (its
inputs changed while it ran) and records on its second. That identity names an entry under `<run root>/.cache/generators/`
holding the digest of every source the last generation left (`generated_sources` when the
generator writes only some of `rtl.sources`, else every one of them), written with
`replacing_file` under the entry's own `run_dir_lock`, exactly as `xilinx.prepare_chipdb` keeps a
chip database. On POSIX, a read-only lock on the resolved design-root directory serializes first
generation and differing input identities for that same tree without a sidecar or an early run
root. It does not coordinate separate roots that write to a shared external output; Windows
follows the existing no-interprocess-lock policy. Indirect tools and dependencies remain outside
the executable identity. Metadata is trusted nowhere here: `FileRecord.trusted` needs the time a
record was taken from the file's own file system (`digest.filesystem_time_ns` writes a marker in the
directory it reads), and neither the design's tree nor anything else a generator reads is
xeda's to write in -- so every input is hashed, a `touch`/`chmod`/`cp -p` costs a hash rather than a re-run, and
an edit given back its old mtime is caught. **Where the run root comes from at load time**: the
launcher puts it there, `design.loading_in_run_root(provider)` (one `ContextVar`;
`provider(False)` gives only a root that is already marked), and
`FlowLauncher.load_run_root` is that provider. What cannot be judged runs: `always_runs`, a
generator declaring no `sources`, no run root in sight (a `Design` built
directly), or a run root whose cache cannot be written. A planning load creates, locks and writes
nothing -- it reads an existing record, and still refuses to plan a design that must generate.
`RunDirectory.unlinked(path)` is the one rule naming anything in a cache under a run root (no
symbolic link on the way, not even one that stays inside), shared by the chip databases, the
generator records and the Git dependency clones (see below). `rtl.generator.run_only_if_sources_modified` was removed: use `always_runs`.
**`--rebuild-all`/`--clean` regenerates**, carried to the load by `LoadContext.rebuild_all`, and
records what that generation leaves: that is the escape where something xeda cannot see changed
(a generator's environment is deliberately untracked), since a `touch` no longer forces anything.
**Xeda writes nothing outside its run root while a design loads**: the record is the only thing it
keeps, and it keeps it there. What the *generator* writes in the design's tree is its own business
-- that is what it is for -- so the oracle admits exactly the sources the design declares it
generates and nothing else
(`tests/test_isolation.py::test_a_design_load_that_runs_a_generator_writes_only_the_sources_it_generates`:
the audit hook records no violation of xeda's own, the canary sweep sees only those sources; `tests/test_generator_staleness.py`).

**A Git dependency's clone lies at `<cache>/<host>/<name>`**, the cache being `<run root>/.dependencies`
(named through `RunDirectory.unlinked`), a `local_cache` the user names, or none when the user gives
`clone_dir`. `design.clone_name_parts` makes the two names: the host and its port folded into one
token, and the repository path with the commit (else the branch) folded into one readable token,
then `_` and a 16-digit digest of the whole identity, the repository URL, the branch and the commit.
Folding cannot keep `a/b` and `a_b` apart; the digest does, so references that select different
clones never share a directory, and one reference always has the same. **The user name and password
of a URL (`https://user:token@host/...`) are no part of a name** (the host token is what follows the
last `@` of the authority), since a name shows in paths and logs; the digest still covers the whole
URL, so URLs that differ only in their credentials are cloned apart (the digest protects nothing:
whoever can read the directory's name can read its `.git/config`, where git keeps the URL as it
was given, and the design file and the recorded settings are the user's own copies). Every log
line and error message that prints a URL goes through `design.redacted_url` (`***` for the
credentials), and a reference has one text form, `DesignReference.__repr_args__` (`uri` and
`repo_url` redacted, while the fields keep the URL, which the clone needs): a reference in a
message, a log line, a container or the debug dump of the design (`_shown_dependencies` masks the
string and mapping forms of a dependency too) is masked. **A dependency is the path of a design
file or `git+<url>`**: the base `DesignReference` refuses a URL (a scheme and `//`), naming the
`git+` spelling, instead of reading it as a path that does not exist and printing it.
`tests/test_git_dependencies.py` runs each route that prints with a credentialed URL, and scans
`design.py` for a URL, or a path made from one (`design_path`), formatted into a message without
`redacted_url`. `tests/test_git_dependencies.py`
pins a name: changing the identity re-clones everything, and moves the `design_hash` of every design
with a Git dependency (its sources count by path). A clone is one directory below its host's, so
none lies inside another. A path, branch or commit with a `.` or `..` component, a drive such as
`C:`, a leading `/`, a backslash or a NUL is refused -- each makes a joined path leave the cache, on
Windows or POSIX -- and so is a host with any but the drive (`h:8443` is a host and a port) and an
empty path. `design.clone_location` joins the names below the cache and requires the result to lie
inside it, for a cache under the run root too (where `RunDirectory.unlinked` alone keeps a name
inside the run root, not inside the cache), then applies `unlinked` there. The names are checked
where they name a directory, at load (`GitReference.validate_repo`) and when the clone is located.
A `clone_dir` is used as given: nothing is named from the URL, so nothing is checked, and a URL with
no host (`git@host:path`, `file:///path`, a bare path) loads with it. A `local_cache` is the user's
own directory: only the names and the lexical containment are checked (`os.path.abspath`, so a
cache that is `.` works), and a link is followed.

Dependencies are brought up to date first, then the depending flow is judged. Within one launch, a
run directory is entered at most once: the plan has one node per flow, so a second entry into a
directory is a `FlowFatalError` (`FlowLauncher._claim_run_dir`, an invariant, not a user error). A flow whose `Flow.always_runs()` gives a reason always runs,
keeps no trace and reports that reason: `openfpgaloader` ("it
programs a device"), a flow asked for a fresh random seed ("it draws a new random seed"), nextpnr
with pin constraints from a URL. Seeds are settings with fixed defaults (verilator and cocotb
`random_seed = 1`; `randomize_seed` defaults to false), so a default configuration is reusable. The
base `Flow.always_runs` returns `None`; every override calls `super().always_runs()`. Every launched run
directory is xeda's own, so a flow's working locations can never overlap the directory it reads
its inputs from.

`--clean` empties a flow's run directory before it runs and runs every flow ("make clean, then
make"; it implies `--rebuild-all`). `--post-cleanup`/`--post-cleanup-purge` clean up after the
*requested* flow completes, dependencies included but deferred to the end so a depender can still
read a dependency's files; pruning removes the trace first, so a pruned run is not reused. A
POSIX lock file (`<run dir>.lock`, `run_lock.py`, beside the run directory, never inside it; none
on Windows) serializes concurrent launches of the same run directory; `xeda scrub`
takes the same exclusive lock and retains the durable lock file. **Scrub reads the disk, never
a design file** (`default_runner.scrub_design`, with `scrub_runs` the one-directory form a
launch's `--scrub` uses, so it stays in its own target): `xeda scrub FLOW DESIGN` collects `FLOW`
and `FLOW_<hash>` run directories directly under `<design>` and under each directory below it
that could be a target's (a target name, `design.target_name_problem`, holding none of
`LAUNCH_DOCUMENTS`, so never a run directory) -- one level, found as they are, a target the design
no longer names included -- and `--target T` only those under `<design>/T`; it lists them,
confirms once, then removes each under its own lock (`run_dir_lock(path, run_root)`: the lock file
is `lock_file(path, run_root)`, beside the run directory -- beside what a last component that is a
link inside the run root leads to, so both names share one lock; without a run root the last
component is never resolved -- and a parent, or a link, leading out of the run root or nowhere is
refused before anything is created) and judges it again once the lock is held. A candidate that is
a link to a directory in the run root is locked by that directory (`_lock_path`), which stays one
lock when another scrub has removed the link and then the directory; a link out of the run root or
to nowhere is locked by its own path, which the lock refuses before it makes anything. Scrub removes
the link first, as itself, and then the directory whose lock it holds: with the directory first, a
second scrub that listed the link could ask for it between the two steps, find a link to nowhere,
and fail on the lock's refusal, where it now finds the link gone and skips it. Once the lock
is held, `_lock_path` is asked again: a link retargeted, or replaced by a directory, while scrub
waited no longer leads to the directory whose lock is held, and is refused with a `RunDirectoryError`
(scrub would remove a directory whose lock it does not hold, and a launch running in it could lose
it). What is removed is the locked directory itself, never what the link leads to by then.
**Scrub removes the runs it listed, which are the runs that were confirmed**, so under the lock
(`_remove_confirmed`), in this order: **one that is gone is skipped** (`_is_gone`: `lstat` says
`FileNotFoundError` and nothing else, so a link, a file and an error that cannot tell are
something): another scrub or a purge removed it while this one waited, and the scrub wanted it
gone. One that is no run directory of the flow any more (`_still_a_run_directory`, by
`_is_run_directory`, the one rule that listed it: named for the flow, a directory, and resolving to
a child of the directory it was listed in -- itself or, for a link, a directory beside it, never
one below it or above it; `_run_directories_in` lists nothing in a directory outside the run root)
is refused with a `RunDirectoryError`. **One whose run records changed is kept**: the listing keeps
the state of `results.json` and `trace.json` (`COMPLETION_DOCUMENTS`, `_completion_records`:
identity, size and times, absent counted), and a directory where they differ, or have appeared, was
written to by a launch after the listing. Either a run ended there (a newer run, a failed one), or
a launch of the requested flow found its run fresh and refreshed the trace once the records it had
to read by content settled (`check_trace`, `Freshness.refreshed`; a dependency's launch,
`refresh=False`, writes nothing). Scrub cannot tell the two apart and keeps both: what is there is
no longer what was confirmed. The rest are removed. **Neither an inode number nor an inode change
time says anything about a run directory**: every file a launch adds or removes moves the change
time. The marker a launch writes to read the file-system clock changes no run record, so it keeps
nothing; the trace it may refresh is a run record, and keeps the directory. Gone and kept are no
error: each is said on the console, one line (`kept PATH: its run records changed after the
listing (a run finished or refreshed there)`, `PATH is gone already`) and a summary (`N folders
removed, K kept, G gone already.`), and logged at info level, because `xeda scrub` configures no
logging; `--json` lists them as `kept` and `gone`, and none is in `scrubbed`; two scrubs that listed
the same directory both succeed. A link out of the run root is never followed. A launch's `--scrub` leaves out its own run directory by where it resolves
to (`_run_directories_in`), never by whether it exists: a sibling's scrub may remove it while this
one lists. **What the lock protects, exactly**: scrub never removes a directory while a launch runs
in it (it waits for the launch), and keeps one whose run records a launch wrote while it waited. It
does not protect a run that was complete when scrub listed it. If another launch is about to read that run -- a
consumer that has run its producer and has not yet taken its read lease on the producer's directory
-- scrub removes it, and the consumer's launch fails closed with a `FlowDependencyFailure` (`changed
before acquiring its read lease`) instead of reading a directory that is going: the designed
fail-closed behavior. Do not scrub a flow whose runs other launches are starting to use. `--json`
reports `target`, the `scanned` directories and the `scrubbed`, `kept` and `gone` run directories,
one document on stdout; the lines above go to stderr. Consumers hold verified
shared leases (`flow_runner/run_lock.py`) on completed dependencies through results and trace
writing; changed or uncertain completion evidence in the
exclusive-to-shared acquisition gap refuses hand-over. Same-mode and exclusive-to-shared reentry
retain protection; shared-to-exclusive reentry is refused. Scrub siblings before taking the
current run lock to avoid cross-variant deadlocks. DSE purge also takes the exclusive lock.
`--remote` always mirrors into the hashed layout
(`<flow>_<flowrun_hash>`, the requested node's identity in the plan this side resolved, which
the remote's `flow_hash` must equal; `RemoteRunner.Settings.hashed_run_dirs`, `Literal[True]` as `Dse`'s), so
remote runs of different settings never share a directory, and refuses `--clean` and
`--hashed-run-dirs` (a remote flow always runs fresh). `--rebuild-all` forces local generator
loading before shipping, while the remote flow remains fresh; the remote runner's default clean
setting does not force local generation on ordinary invocations. It also refuses a deliverable
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

**Every path a flow writes has a role**, a `json_schema_extra` marker on the setting's field:
`WORKING` (`xeda.dataclass.WORKING`) for a working location, always a bare name inside the run
directory whatever it is given (`sim_dir`, `bobj_dir`, `impl_folder`, a log path, ...), or
`deliverable(conventional=...)` for a setting the user may give a location, which is then
**delivered** -- copied to that location once the whole launch has finished, while the run itself
always writes the fixed `conventional` name in the run directory (by convention
`outputs/<design>.<ext>`; `flow.output_name`). `dataclass.written_role(model, field)` reads the
marker back; `introspect`'s `writes` key (`"working"`/`"deliverable"`/`None`) exposes it through
`xeda list-settings --json`. `tests/test_written_paths.py`'s `ROLES` is the one table of every
written field of every flow, bsc's four working locations (`bobj_dir`, `info_dir`,
`verilog_out_dir`, `sim_dir`) included; a new written setting needs its role added there. A plain
nested model's path fields (not only a `Flow.Settings`' own) expand `$PWD`/`$DESIGN_ROOT`/`$DESIGN_DIR` too, once,
when the flow's settings are built or a field of theirs is assigned -- `cocotb.results_xml` and
`yosys_sim.cxxrtl.filename` are the settings this covers; a value assigned straight onto the
nested model afterwards is not expanded, and `written_path_problems` reports it as such.

**`xeda.deliver`** makes good on a deliverable setting's location, or `--outputs-to`, once a
launch has finished, never sooner: `ReadInputs` holds every file the launch's flows read (the
design's files, the files given, and every read setting of every flow of the plan, registered by
the requested flow when the launch starts) and every directory such a setting names, with each entry under it --
the trace's own listing (`trace_inputs.register_read_settings`, local and remote alike) -- so a
destination can be none of those files, nor lie in one of those directories, a file there yet or
not (whether a tool reads it cannot be known before the run; `--outputs-to` into one is refused
up front). The requested flow also checks the named deliveries of every flow of the plan when the
launch starts (`FlowLauncher._check_deliveries_ahead`), so a refusal, or the question whether to
replace a file, never comes after the tool of an earlier flow ran. It makes what no answer could
allow first, for every flow: a file of the design or one a setting reads that lies in the flow's
own run directory (`_refuse_inputs_inside`, which each flow checks again at its turn for a file
made since), `--outputs-to` and the deliveries (`Deliveries.refuse`, `check_outputs_to`) -- and a
destination two deliveries name, or one inside another
(`deliver.refuse_shared_destinations`: every destination known before the run -- every named
delivery of the launch and the files `--outputs-to` is expected to deliver, the requested flow's
last-run artifacts (`predicted`) -- with its flow, in the order the flows run, naming both
settings as `flows.<flow>.<key>`; **names are compared as their
file system compares them**, `_compared`: located, and each name casefolded when the directory it
lies in ignores letter case, which `_ignores_case` finds by looking, never by writing -- an entry
of the directory asked for in the other case, else the directory's own name in its parent on the
same device, else its parent's answer, and "keeps case" when nothing can be asked -- so
`Same.out` and `same.out` are one destination on APFS and NTFS and two on ext4). Only then does it
ask the producers' questions, in the order they run, and its own, last: so a launch has asked
everything before any tool runs, before it scrubs older runs (`--scrub`, whose `scrub_runs` also
asks) and before it takes the lock of its run directory. A launch the user declines has removed
nothing, and a question that waits for the user holds up no other launch or scrub of that
directory. Each flow, the requested one included, keeps the `Deliveries` it checked
(`_deliveries_ahead`) and checks again with it
at its turn, which finds the record the first check anchored, so its destination is read once in
a launch; that second check records what it found (`Deliveries.checked`). The object read its
delivery record before the flow's lock was taken, so the turn, once it holds the lock, reads
the record again if its file changed (`Deliveries.reread_record`, by the file's `_state`):
another launch of the run directory may have delivered meanwhile, and this one would otherwise
find its file not xeda's, or write the old record back over the other launch's entry. A yes holds for the
file as it was when asked (`ConfirmedReplacements`, keyed on the file's `_state`): the second
check asks again about a file that changed meanwhile, and `checked` is taken before the question,
so a file edited while the question is open is not replaced. A refusal made before a flow's tool
ran is a `DeliveryError` with `before_run` true (`check_outputs_to`, `refuse`, `check`): the
launch raises it as it is, never as a `FlowDependencyFailure`, leaves the requested flow's
directory untouched and does not list that flow in `launched`, so `--json` reports it
`"not run"`.
`--outputs-to` with a requested flow that writes no outputs -- one with an `action_reason`, the
programmer -- is refused before anything runs, in `_launch` (before the run root and
`_check_deliveries_ahead`), `plan` (dry runs) and the remote runner alike, a `DeliveryError` with
`before_run` true naming the setting whose location delivers the file it reads, written
`=$PWD/<file>`, and every form of a location (`utils.LOCATION_FORMS`)
(`default_runner._refuse_outputs_to_a_programmer`: the producer's `enabled_by` deliverable, else its
deliverable of the output's own name), or the design source it is. Under `--remote` that setting
would deliver on the remote host, so the error names the producer's own request instead
(`xeda run --remote fpga_pack ... --outputs-to DIR`).
Each node notes what it
delivers, with every file's digest, as its own run completes (`Deliveries.collect`, under its run
directory's lock), and compares what it noted with every copy noted before it
(`refuse_shared_destinations` again, in `_defer_delivery`): the artifacts this run adds to
`--outputs-to`'s and a directory output's files are known only now, and a destination two of them
share is refused before the first copy, a `DeliveryError` that is no `before_run` one, since the tools ran. What no
comparison of names sees (a file system that takes two Unicode forms for one name, a link made
during the run) is found by the file: `DeliveredFiles` holds, by inode, what the launch's
deliveries made, and a delivery whose destination is one of them reports that two deliveries name
it (the first stays), never "changed while the run went on". The copies themselves are made in
`_finish_launch`, before the deferred clean-ups, once every flow of the graph has registered its
reads -- a dependency's output could
otherwise replace a file a later sibling or its own depender reads before that depender's `init()`
has even run. An existing file at a destination is replaced without asking only when it is xeda's
own earlier delivery there, unchanged: inode and content digest are what decide -- a same-inode
file holding exactly the delivered bytes is xeda's copy whatever touched it since, while another
inode, or different bytes, fails closed and asks. **Whether the content must be read is decided by
the trust rule, like every other record's** (`deliver._destination_record`): a check that
reads a destination first reads the clock of that destination's own file system
(`deliver._destination_clock`, a marker made and removed in the directory delivery writes its
temporary in, `digest.filesystem_time_ns` -- never the process clock) and anchors the record it
takes to that time (`anchor_ns`, with the `anchor_device` it was read on, beside the fail-closed
`recorded_ns` an older xeda reads); the next check of an unchanged delivery recognizes it by its
metadata and reads nothing, so an unchanged re-delivery of a huge output costs no pass over it.
The anchor is never arithmetic on the record already held: a record is anchored only to a clock
read at a moment that very content was verified, and only when it is really settled before it
(`FileRecord.settled_before`). So the delivery xeda just copied, racy by construction, is read
once, by the first check after it has settled, whose read anchors it (`_copy` then reads
nothing), and never again by a check or a copy; a launch still inside the racy window anchors
nothing, and each of its checks (the one made when the launch starts and the one at the flow's
turn) and its copy read the destination. No clock to read (a read-only directory, a file system
that refuses): no anchor, and every check reads the content. A destination found on another device than its anchor was
read on has that anchor discarded (`deliver._recorded_anchor`), is read once, and is anchored
afresh to the clock of the file system it is on now. See `docs/run-directories.rst`'s "Outputs where you
name them" for the user-facing rules (never onto an input nor into a read directory, never a
directory, never into a run root, `--overwrite-outputs`, the delivery record beside the run
directory).

`tests/test_isolation.py` is the isolation oracle, in four parts:

- **The canary sweep and the audit hook**, exercised together by
  `test_nothing_outside_the_run_root_changes_but_what_was_named`: every registered flow
  (`FLOWS`/`settings_samples.flow_classes()`), launched under stand-in tools, in four scenarios
  (`twice`, `clean`, `purge`, `delivered` -- 100 cases) inside a `World` seeded with a canary file
  at every name a template or xeda itself could write (`CANARIES`: every flow's template
  filenames, `trace.json`, `.xeda.lock`, a Vivado project, `Logs/canary.log`, ...) and a symlink
  out to a sibling `outside/` directory. `_state` snapshots every entry of the design directory's
  *parent* (type, mode, content or link text, a file's modification time) before and after a
  launch; nothing outside the run root may differ but exactly the destinations the launch named
  (a located deliverable, the `outputs_to` directory) and the files delivery wrote there --
  a tool's file beside a delivered one, or a touch-only change, fails it
  (`test_the_sweep_sees_an_extra_file_beside_a_delivered_one`,
  `test_the_sweep_sees_a_touch_only_change`). At the same time, a `sys.addaudithook`
  installed once for the session (`_audit`, live only inside a `watching()` context) records every
  write, create, rename or delete a launch makes (`open` in a writing mode,
  `os.remove`/`rmdir`/`mkdir`/`chmod`/`utime`, `os.rename`/`os.link` -- which also covers
  `os.replace` -- `os.symlink`, `shutil.copyfile`, `shutil.rmtree`, `os.truncate`): a write inside
  the run root is ignored, one under the named delivery destination is allowed only when it comes
  from `xeda/deliver.py` (`_from_deliver`, walking the call stack), anything else is a violation
  -- so a stray write is caught even if the canary sweep's own before/after diff happens to
  miss it. `test_the_oracle_sees_every_change_outside_the_run_root` is the audit hook's teeth test: it
  exercises every one of those calls directly and checks the hook counts them all (CPython audits
  a missing `dir_fd` as `-1`, not `None`, which is why `_placed` treats both as "no descriptor").
  Its limits, stated in the module docstring: tools outside `FAKED` are stubbed, so their real
  writes are not observed; the audit hook sees only the test's own process; paths outside the
  snapshotted parent are not compared -- the opt-in real-tool layers are where to extend it.
- **The read-only tree**: `test_a_launch_needs_nothing_writable_but_its_run_root` makes every
  flow's whole tree read-only except the run root (`_freeze`/`_thaw`) and checks the launch ends
  the same way it does on a writable tree; `test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree`
  repeats it with real `bsc`/Bluesim on the Bluespec `gcd` example. `test_the_command_line_changes_nothing_outside_the_run_root`
  covers the CLI itself (`--clean`, `--post-cleanup`, plain), asserting exit 0 so the oracle cannot
  pass merely because the run failed early, and
  `test_ise_synth_from_the_design_directory_deletes_none_of_its_files` is a
  regression test (ISE used to delete the design's own files from its start directory), with and
  without `xtclsh` on `PATH`.
- **The static scan**: an AST
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
  scan (`test_every_raw_write_by_name_is_reviewed`, with its mutation test) finds
  every raw write by name -- `open` for writing, `write_text`, a copy, a rename, a link -- that
  does not go through `replacing_file`/`replacing_copy`, against `REVIEWED_WRITES`.
  `test_links_a_tool_left_are_never_followed_out_of_the_run_directory` is the tool-made-links
  case: links in, out, dangling, cyclic and at a working location's name, through a relaunch,
  `--clean`, post-cleanup, a purge, `xeda scrub` and deliveries, with the outside tree unchanged.

`tests/conftest.py`'s autouse, session-scoped fixture fails if the checkout's top level, `tests/`,
any example design's own directory, or `.github/` and `tools/` (each with every directory below
it) gained a new entry, lost one, or changed a file (its size or modification time, so a test
that rewrites `.github/workflows/ci.yml` fails too, even with the text it had) since `conftest.py`
was imported -- the snapshot is taken at import, before pytest imports the test modules, so a
module that writes as it is imported (the bytecode of a script it loads) fails the run too. The
exemptions are `tests/__pycache__` (the suite's own imports), names that start with a dot, and a
top-level `xeda_run/`, the latter only when an opt-in
layer (`XEDA_TESTS_VIVADO`/`XEDA_TESTS_DOCKER`/`XEDA_TESTS_EXTERNAL`) is set, since those tests
deliberately work in the checkout's own `xeda_run/` (a container can mount it where the system
temp directory is not). A `__pycache__` in an example's directory is a failure: `test_ghdl.py`
and `test_nvc.py` simulate the examples in place. A test that runs a script of `.github/` or
`tools/` as a module loads its source without bytecode (`tests/test_changelog_fragments.py`,
`tests/test_ci_pins.py`); `tests/test_checkout_guard.py` is the guard's own oracle.

### Other runners

- `flow_runner/dse/` - `Dse` launcher running many flow instances in parallel (`pebble`) under an
  `Optimizer`; `FmaxOptimizer` (`fmax.py`) does the binary/interpolation search for max clock frequency.
- `flow_runner/remote.py` - `RemoteRunner` ships the design over SSH via `execnet`, runs xeda remotely,
  and streams tool stdout/stderr back through a PTY pair. The streaming/PTY behavior is heavily tested
  in `tests/test_remote_streaming.py`; the code injected into the remote (`STREAM_OUTPUT_SETUP`,
  `remote_runner`) must use only the API guaranteed by the checked protocol floor; the streaming
  setup stays stdlib-only. A test pins the worker's xeda imports.
  The design archive `send_design` builds is read by the *remote's* xeda, which forbids unknown
  keys. **Requirement: a remote runs a build with remote protocol support**: release line
  `REMOTE_XEDA_MIN_VERSION = (0, 4, 4)` (including `0.4.4.devN+g...`) and
  `xeda.REMOTE_PROTOCOL_VERSION >= REMOTE_PROTOCOL_MIN_VERSION` (currently 1, the first released
  protocol -- no earlier release carried a marker: canonical resolved settings, relocated read
  inputs with their original path identities, declared output records and checked hand-over,
  current-run evidence for remote simulations, the FPGA build graph with `fpga_pack` and a
  programming-only `openfpgaloader`, the identity rule -- a node's `flow_hash` is its settings
  plus its ordered resolved input origins, the hash `RemoteRunner` names the mirror by and compares
  with the remote's -- the declared Vivado outputs, and `yosys`'s declared netlist with its ASIC
  configuration, a bundled platform counted relative to xeda's installation; a remote
  with no marker or a lower one is refused up front, not failed on a hash mismatch it cannot
  explain; `tests/test_remote_streaming.py` and `tests/test_remote_run.py` pin the refusal).
  `check_remote_xeda` refuses xeda 0.4.3 and development checkouts without the capability with an
  "upgrade the remote xeda" error before anything ships. Version alone does not prove protocol
  support.
  `REMOTE_PROBE` imports the remote interpreter's actual xeda and reports its version, location
  and protocol marker; a missing or broken import is an incompatible install. The streaming setup
  remains stdlib-only. execnet starts `python3` from the *non-login* PATH, so a shadowing checkout
  must be upgraded or removed even if another installed distribution is current.
  **On release**, fold the changelog fragments (see "Conventions and gotchas"), and raise
  `REMOTE_XEDA_MIN_VERSION` to the published release tuple that carries the
  current protocol and retain its protocol marker. The exposed `REMOTE_PROTOCOL_VERSION` and the
  required `REMOTE_PROTOCOL_MIN_VERSION` are raised together once per release cycle, when anything
  remote-visible changed since the last release; pull requests between releases do not bump them
  (development builds are not supported remotes). When they are raised, update the pinned key sets
  of the shipped `rtl`, `tb` and git-reference tables and the nullable-key pins in
  `test_remote_run.py`, and verify archive/source round trips plus the popen remote runs. The
  archive and shipped worker may rely on the API guaranteed by that protocol floor; no 0.4.3
  archive projection or compatibility policy is maintained.
  A failed remote run's artifacts are fetched only if the remote vouches its run wrote them:
  `remote_runner` sends its results, then that list, judged on the remote's own file system (the
  remote flow's `wrote_output`, or, if a worker lacks it, the remote directory's state recorded
  before the run: identity, size, times); `_transfer_artifacts` drops the rest. A file merely
  existing on the remote (an earlier run's) proves nothing, and no clock or file of this side is
  ever compared with the remote's.
- `platforms/` - ASIC PDK descriptions (asap7, nangate45, sky130hd/hs) for OpenROAD/DC;
  `board.py` + `data/boards.toml` for FPGA boards.

**Board names: bundled ones are lower case and found in any letter case; custom ones are exact.**
`data/boards.toml` names every board in lower case (`ulx3s_85f`, `arty_a7_100t`, `basys_3`, ...),
and `board.bundled_boards` refuses a database with any other name, so two names that differ only
in case cannot exist (`tests/test_boards.py`). `get_board_data` lowers the name for a lookup in
the bundled database and looks it up as written in a custom one, and `canonical_board_name` is
what a validated `board` is stored as -- in the validator and in `__setattr__`, since a
`before` validator's write to the assigned field does not stick -- so every spelling is one
setting, one run identity and one plan. The resolver compares two nodes' `board` leaves
ignoring case unless a node of the group names a custom database (`resolver._agree`). A custom
database and every file name are case-sensitive, as written. A board entry's optional
`openfpgaloader_board` is the board's name in openFPGALoader (`--board`, from its `src/board.hpp`;
text when given, and every bundled board has one); `openfpgaloader` passes it as `--board` when
`board` is set and no `cable` is. A `cable` takes precedence: `--cable`, `--fpga-part` when the
part is known, and no board name. A board without an `openfpgaloader_board` adds no `--board`.
With `--board` it gives no `--fpga-part`: the loader knows the board's part, the option would
replace it, and the loader names its flash bridge bitstream (`spiOverJtag_<device><package>.bit.gz`)
by the option as written, so a part given without a board name drops a Xilinx speed grade
(`_loader_part`). The former key `name` is an error naming it
(`WithFpgaBoardSettings._fpga_validate`).

Board-aware settings read their database through `WithFpgaBoardSettings.board_data()`.
`custom_boards_file` replaces the bundled database (which stays TOML) and resolves relative to
the design root. It is TOML or YAML, chosen by its suffix (`board.read_board_database`,
`board_database_format`: `.toml`, `.yaml`, `.yml`, case-sensitive; any other suffix is a
`ValueError` naming the file and the accepted suffixes, checked whenever the setting is given, even
with no `board`). YAML goes through the shared strict loader (`yaml_loader.load_yaml`), and a
parse failure is a `ValueError` naming the file and line; the bundled database is still TOML
(`toml_loads` of the packaged `boards.toml`). A board's local `lpf` resolves relative to its
database file, bundled or custom, whatever its format (`WithFpgaBoardSettings.board_file`, a
context manager: a bundled file may exist on disk only while it is open; nextpnr's board
fallback does the same in `_prepare_board_inputs`). A flow sharing `board` with a board-aware
dependency must also share `custom_boards_file`.

## Conventions and gotchas

- **Help screens are click-extra's, and the theme rides on `context_settings`.** `XedaHelpGroup`
  subclasses `click_extra.Group`; the xeda palette (yellow headings, green options) lives in `XEDA_HELP_THEME` / `HELP_FORMATTER_SETTINGS`
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
- **A hidden option is never suggested.** click suggests the close matches of a mistyped option
  from every option a command has, the hidden ones that only say what replaced a removed option
  (`--xeda-run-dir`, `--cwd`, ...) included. `XedaCommand` (the default `command_class` of
  `XedaHelpGroup`, so `@cli.command` takes it) and `XedaHelpGroup` re-raise click's
  `NoSuchOption` with the matches taken from the visible long options
  (`cli_utils.reraise_suggesting_visible`). A command built with another class loses the rule:
  `tests/test_hidden_options.py` sweeps the whole command tree for both.
- **A log record is never changed for one handler's sake.** Every handler of the process gets
  the same `LogRecord`, so what the detailed logs show of a logger name (`xeda.flow` as `flow`)
  is a formatter of the CLI's own handler (`cli.ShortLoggerNames`), which formats a copy.
  `setup_logger` wraps only the handler it installed, never another party's (pytest's, an API
  user's); `tests/test_cli_logging.py` logs through the CLI's setup with a handler before and one
  after it.
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
  `Field(description=...)` with no default) is a *required* field, which a `Flow.Settings` refuses
  when its class is defined.
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
- **A validator logs nothing above DEBUG.** It runs on every validation -- of the section of a
  flow the run leaves out (the resolver checks their syntax), on every assignment and reload --
  so INFO from it is noise, and for an unused section a report about settings that do not apply
  ("Detected FPGA family" printed just before "openfpgaloader needs `fpga`"). Report a derived
  fact where it is used. `tests/test_model_invariants.py::test_no_validator_logs_above_debug`
  scans every validator in the package.
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
  (a directory xeda chose that lies under its run root -- every launched flow's, now) or
  `RunDirectory.unlaunched` (a flow built directly, not through a launcher: xeda deletes nothing
  there, logged instead). `inside`/`holds` locate a path inside the directory without following a
  symbolic link out of it (a link is itself, its last component never resolved); `writable(path)`
  is what every file xeda writes there itself goes through (`copy_from_template`, the launcher's
  `settings.json`/`results.json`, `Tool.run`'s `env.sh`, and every flow's own writes) -- it removes
  a link at that name first, so the write always makes a regular file, and raises in an
  `unlaunched` directory rather than silently deleting through someone else's link. The file
  itself is then written complete-then-renamed (`utils.replacing_file` / `replacing_copy`: a
  temporary beside the target, `os.replace`d over it only once whole, `copy_mode_from` before the
  commit), so an interrupted write leaves the earlier file; a tool's log alone is renamed onto its
  name first and written after (`utils.live_log`, see "Tool execution"). A path that leads out of
  the run directory -- by `..`, or through a symbolic link a tool may have made -- is refused by
  `inside` with a `RunDirectoryError` naming the link; a link at a path's own name is removed as
  itself, never followed. `remove(*paths)`
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
  run directories" above).
- **Every design source is typed.** `design.SOURCE_SUFFIXES` is the case-sensitive inference
  table. Unknown, ambiguous (`.json`, `.bin`, `.cfg`, `.config`) or mis-cased suffixes need an
  explicit `type`; invalid explicit types fail with suggestions (`source_type_named`). `Data`
  has no automatic HDL frontend; a flow or design may still read it. Append `SourceType` members,
  never reorder the historical ordinals. Script flows declare `reads_sources` and
  `design_parts`, then iterate `sources_read()` in code/templates. Every consumed part
  rejects unsupported `LANGUAGE_TYPES`; other types are deliberately skipped. A design none of
  whose sources the flow reads (an `.edf`-only design, or none at all) is refused too:
  `Flow.check_design_supported` raises `NoReadableSource`, naming each part's sources with their
  types and the types the flow reads. It runs for every planned node, so `nextpnr`'s default
  `yosys_fpga` producer is refused, and the resolver adds which source would replace a producer
  (`a JsonNetlist source would supply nextpnr's netlist and skip yosys_fpga`) when that is all it
  takes: one input of the plan reaches the producer, and by default. A producer that is bound (a
  chain, a saved, command-line or API binding) or that a second input reaches stays in the plan
  whatever the design lists, so its refusal adds nothing (`_replaceable_by_a_source`). A typed
  `JsonNetlist` that displaces the producer plans. **Every flow that reads the design's sources
  declares `reads_sources` and selects with `sources_read()`**, the one selection (it raises a
  `TypeError` for a flow that declares none); `reads_sources = None` means the flow reads only its
  declared inputs (`fpga_pack`, `nextpnr`, `openfpgaloader`, `openroad` -- its `Sdc` sources come
  through its optional `sdc` input, as `nextpnr`'s do -- `vivado_impl` -- its `Xdc` and `Sdc`
  sources come through its optional `constraints` input, its netlist through `netlist` -- and
  `vivado_power`). `tests/test_source_contracts.py` scans each flow's code (its MRO's classes, the
  module-level statements of their modules, and the module-level helpers that code reaches by name,
  through imports and through other helpers: `flow_source_reads`. A helper that the flow never
  calls is not its code: `vivado_impl` shares a module with `vivado_synth` and does not call its
  `constraint_files`. Minus `JUDGING_METHODS`: `check_design_supported` and what it asks) and the
  templates it renders (`test_design_parts.reachable_templates`): a flow with a read of the design's
  sources must declare, a declaring flow must call `sources_read`, and any other read
  (`sources_of_type`, `sim_sources`, `.rtl.sources`, ..., called or taken as a method) must be
  in `REVIEWED_DIRECT_READS` -- except `header_dirs` in a flow that declares both header types
  (it is their include path). `HELPER_MODULES` (every module of `xeda.flows`/`xeda.flow`, and
  `xeda.cocotb`) is held to the same review for code no flow's class holds (the evidence
  readers, cocotb's support). The same file plans every example for every flow its sections name. One source of any type the flow
  reads is enough, a constraint or header file included: an `.edf` with an `.xdc` still plans for
  `vivado_synth`, because requiring a language source would refuse a Tcl-only design whose script
  reads its own RTL. Headers need an actual include/search path, and source type names must never
  become tool commands.
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
  `fail_severity`. cocotb, on every simulator, needs at least one test that ran and none that
  failed (an all-skipped run fails). Verilator is driven by xeda's own C++ main
  (`verilator_main.cpp`), and xeda's `VL_USER_*` hooks (`xeda_hooks.cpp`) record every
  `$finish`/`$stop`/`$error`/`$fatal`/warning and write the end record. A design's own C++ driver
  replaces only xeda's main: the hooks are still linked in and record how it ended
  (`ended_by = "exit"`, with the driver's exit status, plus the recorded events). cocotb replaces the whole mechanism: no hooks, and cocotb's results decide.
  The hooks also make the model's stdout line-buffered, since it is copied through a pipe to
  `sim_dir/sim.log` (block-buffered, a timed-out model's output was lost); under cocotb nothing is
  copied, so its output keeps the terminal. The simulated top (`--top-module`) is `tb.top`, else
  `rtl.top`; with cocotb, `cocotb.cocotb_toplevel` (`tb.cocotb.toplevel`, else `rtl.top`), the
  one rule `Cocotb.env` sets `TOPLEVEL` by. Its settings: `timeout`, `fail_severity`
  (`warning`/`error`/`failure`/`fatal`, default `error`), `random_init` (default false),
  `x_initial`/`x_assign` (`"0"`), `rtl.parameters` applied only when the RTL top is the simulated
  top, and `stop_time` (rejected with cocotb or a design's own driver); minimum Verilator 5.024.
  A report that ends a run is one event, and `ended_by` names its cause: `error` for `$error`, a
  failed assertion and `$stop` (whether it reaches the hooks through `vl_stop_maybe` or `vl_stop`),
  `fatal` for `$fatal` and Verilator's own fatal errors.
  `timing` is off by default: Verilator then ignores `#delay` (a `#100; $finish` ends at time 0),
  and xeda no longer hides its `STMTDLY` warning (a delay on a statement; `ASSIGNDLY`, on an
  assignment, was never hidden, and `INITIALDLY` is never raised), which fails a run only with
  `warnings_fatal`.
  Verilator's makefile stops in a build directory whose path has whitespace ("GNU Make cannot
  build in directories containing spaces"), so `verilator` refuses such a run directory, and a
  `sim_dir`, where it builds; `bsc_sim` refuses it for every simulator, since bsc's link step runs
  its tools through a shell without quoting (`_check_link_paths` stays as the backstop for the
  design's own paths). Both go through `Flow.check_run_directory`, which is pure and judges the
  path alone, before anything is created: the launcher calls it in `_run_identity` for every
  launch, `plan` calls it, and `Dse` calls it once before the search; the `--remote` runner never
  does (the build is the remote's). The run directory is judged by what it leads to (make builds
  in the physical directory), `sim_dir` by its name (the launcher removes a link left at it).
  Another flow with such a limit is a decision, pinned in `tests/test_verilator_run_directory.py`.
  The hooks header goes into the compiler flags by name (`-include xeda_hooks.h`, the model builds
  in `sim_dir`): make splits flags at a space, and reads `#` and `$` in them.
  `SimFlow.check_design_supported` refuses a design whose `tb.sources` holds a source of a
  `design.LANGUAGE_TYPES` language (Verilog, SystemVerilog, VHDL, Bluespec, Chisel) when there is
  no `tb.top` and no cocotb: the simulator would run `rtl.top`, which has no stimulus, and report a
  drained queue. The one exemption is `SimFlow.runs_without_testbench_top(design)`: `ghdl_sim` for
  a VHDL testbench (`ghdl find-top` finds its top), `verilator` and `yosys_sim` for a design with a
  `Cpp` source (`SimFlow.has_cpp_driver`, the one predicate for a design's own C++ driver, which
  runs the model whatever HDL the testbench also holds). Another simulator on that list is a
  decision.
  Every `SimFlow` shares `timeout` (per subprocess invocation containing simulation) and
  `fail_severity` (`warning`/`error`/`failure`/`fatal`, default `error`; `failure` and `fatal`
  share a rank). GHDL, nvc, ModelSim, VCS, Vivado simulation/power, CXXRTL and every accepted
  `bsc_sim` backend use the same verdict and persist `sim.evidence` plus `sim.ended_by`,
  `sim.time`, `sim.time_unit`, `sim.errors` and `sim.warnings`. A recognized `$finish` (including
  VHDL `std.env.finish` and `std.env.stop`), a confirmed requested stop/cycle limit, or a
  supported user-owned C++ driver exit 0 can pass if no event reaches the severity threshold.
  Missing/unknown evidence, silent exit 0 and a drained event queue fail; a nonzero process
  status always fails. GHDL/nvc retain actual stop time, so a sparse stop that misses the
  requested time fails; nvc uses a passive VHPI monitor and requires a C++ compiler for its helper.
  Bluesim `max_cycles` requires the exact measured count and final time. CXXRTL observes a
  user-owned driver's return and RTL assertions; exit 0 is valid even if simulated time is
  unknown, and it rejects `stop_time`. NVC's old
  `exit_severity` was removed; use `fail_severity`. VCS and Questa adapters fail closed when
  native evidence is unrecognized and have not been verified on licensed tools. VCS quiet
  `$finish(0)` and VHDL completion need a native finish diagnostic; a UCLI time checkpoint alone
  does not prove HDL completion. Vivado 2024.2 `xsim` requires source-preserving `elab_debug`;
  explicitly disabling it is rejected so VHDL `std.env.stop` can be distinguished from
  Verilog `$stop`, an error-rank event that fails at the default threshold. The accepted bsc
  backends are Bluesim, Verilator, Icarus, ModelSim, Questa, VCS, vcsi and xsim; `cvc`, `cver`,
  `isim`, `ncverilog` and `veriwell` fail before compilation.
  Icarus's runtime evidence remains a Linux CI gate.
- **ModelSim exits 0 unless told otherwise.** Its `exit` takes the status as `exit -code N` (a
  plain `exit 1` exits 0, and the fake `vsim` mimics that); without `vsim -onfinish stop`, `$finish`
  exits vsim at once with status 0; and a testbench's `$error` or failed assertion never changes
  the status -- `run.tcl` reads `coverage attribute -name TESTSTATUS`, native `runStatus`,
  `$now` ticks and `$resolution`. The adapter requires a matching checkpoint and the runtime
  section of an owned batch `-logfile`, bounded by `echo` markers, and judges recorded events
  at `fail_severity` (default `error`). VHDL `std.env.stop` is accepted with a native break
  from a known VHDL source; a Verilog `$stop` Note fails. `BreakOnAssertion` is set to 4 after loading
  the design so VHDL `failure` does not stop the testbench before its finish. ModelSim reports
  VHDL `failure` and SystemVerilog `$fatal` as the same TESTSTATUS (3), so `fatal` and `failure`
  have the same status threshold. The flow's default image is `chaseruskin/modelsim-intel`
  (ModelSim-Intel Starter 2020.1, amd64, no license), verified against that release.
- **A Vivado project run's outcome is in its properties alone.** `wait_on_run` returns normally
  when the run failed in Vivado 2021.1 and raises an error in 2024.2, so `vivado_synth.tcl`'s
  `xedaWaitOnRun` waits either way, then requires `STATUS` "<step> Complete!" and `PROGRESS`
  "100%" (a failed run reports "<step> ERROR"), records the status (the `status` result) and
  otherwise exits 1 naming the run, its status and `<project>.runs/<run>/runme.log`.
- **`out_of_context` adds one `-mode out_of_context` to `synth_design`, and never replaces a
  mode.** `vivado_synth` and `vivado_project` put it in the run property
  `STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS` (`synth.steps.SYNTH_DESIGN.ARGS.MORE.OPTIONS`),
  `vivado_alt_synth` in the `synth` step. A mode the design already gives is kept once; another
  one is refused before anything runs, by `check_settings_supported`
  (`vivado_synth.out_of_context_conflicts`, a `FlowSettingsError` naming the setting and the
  option), and again where the script is rendered, for a flow built directly. Without
  `out_of_context` the design's own `-mode` goes through. `vivado_synth.tcl` also sets
  `set_synth_properties` after the steps, so with `out_of_context` an entry for that property
  replaces the steps' value, the mode included: it has to carry `-mode out_of_context` itself
  (`property_mode_conflicts`). `vivado_project.tcl` renders no `set_synth_properties`, so only the
  steps count for it (`step_mode_conflicts`). What counts as a mode is what Vivado 2024.2 reads
  (`is_mode_switch`, checked against the real tool): the switch in lower case, or an abbreviation
  of it no other switch shares (`-mod`; `-m` is `max_bram` too), never `-mode=default` or
  `--mode`; the value in either letter case, never abbreviated.
  `tests/test_vivado_out_of_context_mode.py`.
- **Yosys FPGA synthesis options are chosen by the installed yosys release.** The `synth_*`
  passes changed their options across releases (ABC9 became the default in 0.36, Nexus moved to
  `synth_lattice` in 0.59, 0.69 made ABC9 unconditional and dropped `-retime`), so
  `YosysFpga.Settings.synth_command(release)` maps each setting to the option the target's pass
  has in that release, or rejects it -- a setting is never silently dropped. `yosys_release`
  treats an unreadable or newer yosys as `NEWEST_CHECKED_YOSYS`. The oracle is `PASS_OPTIONS` in
  `tests/test_yosys_fpga_flags.py`, read from the passes' sources for every supported release
  from 0.63:
  on a new yosys release, add its option changes there and raise `NEWEST_CHECKED_YOSYS`.
  `tests/test_yosys_fpga_netlist_cells.py` runs `yosys_fpga` with the real yosys for every nextpnr
  target and fails on a generic (`$`) cell nextpnr cannot place, judged as nextpnr's JSON reader
  judges it (`frontend_base.h`: its top module, non-box modules, the skipped `$scopeinfo`/`$print`/
  `$check`, and a pad-driving `$_TBUF_` on ECP5/iCE40). It guards CI's tool bumps against yosys
  regressions such as the `$buf` cells the 2026-09-15 to 2026-10-07 nightlies left for Xilinx and
  Gowin (yosys PR 6174, fixed by 6267).
- **`synth_pass_only` is an option group that reads the design's sources as a bare
  `yosys <files>` does, then runs the target's pass, and it refuses what would make it differ.**
  The reader is still chosen by each source's `type` (xeda's source type is authoritative), so a
  source whose explicit `type` contradicts its suffix is read as that type, not by suffix.
  The full recipe adds Xeda preparation and cleanup around the pass. Pass-only omits those
  Xeda-owned pre- and post-synthesis stages; design parameters, `synth_flags` and explicit ABC9
  script selection still apply in both modes. `Settings.synth_pass_only_conflicts()` (20
  settings, checked during planning and again in `run()` after initialization has folded design
  attributes into settings) refuses the ones that add Xeda stages (`prep`, `pre_synth_opt`,
  `post_synth_opt`, `splitnets`, `post_synth_rename`, `black_box`, `keep_hierarchy`,
  `set_attribute`, `set_mod_attribute`, `clockgate_map`, `rmports`, `stop_after`, `rtl_json`,
  `rtl_verilog`, `rtl_graph`, `sta`, `ltp`) and the ones that change how a source is read: a
  nonempty `read_verilog_flags`, a `systemverilog` other than `default`, and a nonempty
  `read_systemverilog_flags`. Those three are refused **by value, never by whether they were set**
  (`settings.json` writes every field, so a `model_fields_set` rule would behave differently on
  reload; omitting a setting and writing its default are the same) and from settings alone, never
  from the design's sources, so the check stays pure and class-level. The defaults (`-sv`, the
  slang plugin) are Xeda's own and so are refused: the mode needs `read_verilog_flags: []` and
  `systemverilog: default` written (`-s read_verilog_flags=` on the command line: the text `[]`
  is refused). A `.sv` source is then read with the template's own `read_verilog -sv`, as yosys
  does. `READER_SETTINGS` in `tests/test_yosys_recipe.py` holds a decision for every `settings.*`
  the reader templates render, plus the `defines` and `ghdl_args` variables (a new one fails the
  sweep until decided); `use_slang_plugin` is unreachable under the mode (it only gates loading the
  plugin that `systemverilog == slang` selects). `tests/test_yosys_recipe_real.py` shows a plain
  `.v` source (one `-sv` cannot read) and a `.sv` source written by the mode equal, name for name,
  to the native netlist. Comparing with a native command still means matching the installed Yosys
  build and target, source paths and order, parameters, synthesis-pass flags and ABC9 script.
  Source path spelling matters because Yosys embeds it in generated names. No general area/timing
  advantage should be claimed from the mode.

  `flatten` is mode-specific too. Unset on a Xilinx target it is exactly `flatten: true`
  (`Settings.effective_flatten`, used by `synth_command` and both `yosys_fpga_synth` templates):
  xeda flattens before the RTL outputs (`rtl_verilog`, `rtl_json`) and passes `-flatten`, because
  `synth_xilinx` alone keeps the hierarchy and the measured corpus was `flatten: true`.
  `synth_pass_only` leaves it to the pass (False for Xilinx, no option), and the other targets'
  passes flatten on their own, so unset adds nothing there. An explicit value applies in both
  modes. `tests/test_yosys_recipe.py` pins the unset script equal to the `flatten: true` one.

  ABC9 script defaults are mode-specific: with `abc9_script=None`, the full Xeda recipe selects
  `flow3`, while pass-only leaves the synthesis pass's choice in effect. The full recipe also
  derives ABC9 delay from the clock; pass-only does not add that implicit delay. An explicit
  `abc9_script` selects one installed Yosys script (`default`, `default.area`, `default.fast`,
  `flow`, `flow2`, `flow3`, `flow3mfs`) in either mode when ABC9 mapping is enabled. These names
  are checked against Yosys 0.63 and 0.69 constpad files; do not copy the scripts into Xeda.
  Legacy `flow3` remains `Optional[bool]`: `true` selects `flow3` in either mode, `false` leaves
  script selection to the pass, and unset preserves mode-specific defaults. Reject a request that sets both `abc9_script`
  and `flow3`; do not treat `flow3` as a pass-only stage conflict. The one `Settings.abc9_scratchpad()`
  emits these choices, while `synth_command()` emits the target pass's version-specific flags.

  **The clock-derived delay reaches ABC9, and ABC9 often ignores it.** The full recipe writes
  `scratchpad -set abc9.D <period_ps / 1.5>` before the pass. `abc9_exe` reads `abc9.D` (in
  picoseconds) and puts `-D <value>` where the script has `{D}`: the four `&if` calls of `flow3`,
  and the `&if` of the default scripts. The Yosys log shows it (`ABC: + &if -W 300 -D
  6666.666666666668`), on Xilinx, ECP5, iCE40, Nexus and Gowin. ABC takes the value as the required
  time of its LUT mapping, not as a goal it must reach: a target below the least delay it can reach
  is replaced by that delay, which is also what it uses with no target, and ABC says so in the log
  (`ABC: Warning: Cannot meet the target required times (4000.00). Mapping continues anyway.`).
  The netlist is then the one written with the `abc9.D` line deleted. Only a target the logic can
  meet changes the mapping: ABC spends the slack on area, so the mapping can get smaller and
  deeper, or keep its LUT count and change only its mix of LUT sizes (a three-operand 32-bit adder:
  63 LUTs at 2, 8 and 50 ns, LUT3/LUT4/LUT6 32/26/5 at 2 ns and 32/31/0 from 8 ns). Measured with
  Yosys 0.69+156 on picosoc: every period from 2.5 to 8.5 ns writes one netlist, equal to the
  no-delay one (ABC's least delay is 5.7 ns, so it can meet the target from a period of 8.6 ns); 9
  and 10 ns map with 1 and 2 more levels and 27 and 24 fewer LUTs (3,094 and 3,097 against 3,121).
  A design with nothing to trade (macram, whose logic is two levels deep) writes one netlist for
  every period. So a netlist that does not move with `clock.period` is not a lost setting: look for
  `-D` in the log. Add no workaround that scales the delay.

  **Reads affect generated names.** Each `read_verilog` advances Yosys' `autoidx`, and ABC9 maps
  by generated cell names; an extra primitive-library read can therefore change a netlist. The
  target pass's `primitive_libraries()` still describes the libraries it reads internally. It is
  one list for every supported release (`PASS_READS` in `tests/test_yosys_fpga_flags.py`, checked
  by hand against each release's pass); the installed yosys is compared with it by test, both
  directions, for every target, Gowin included.
  `verilog_lib` is a reviewed user read after sources; when it names a file already read by the
  pass, `YosysFpga.verilog_libraries_to_read()` skips that duplicate by file identity. Yosys' `+/...`
  spelling is compared lexically; ordinary paths are compared against the selected Yosys
  installation's data directory (`common.yosys_data_dir`). Resolve `yosys-config --datdir` as a
  sibling of the selected Yosys executable (inside the selected image for Dockerized tools), never
  from an unrelated PATH installation. If the sibling helper or data directory cannot be found,
  fail rather than guessing. Small cell-count differences alone do not establish a quality
  difference. `tests/test_yosys_recipe.py` sweeps file-valued settings so a new one must be
  classified as a refused Xeda stage or a reviewed read; tests compare generated scripts and
  exercise the native result against installed Yosys without claiming every reader/path setup is
  identical by default.

  **Library boxes and `write_json`.** Every `-lib` read leaves boxes in the design, and `-lib`
  keeps the `lib_whitebox` models with their unprocessed `always` blocks, which `write_json`
  refuses ("Module ALU contains processes"). `write_verilog` and `show` skip boxes; `write_json`
  does not. So the RTL stage (`post_rtl`, shared by `yosys`, `yosys_sim` and `yosys_fpga`) writes
  `write_json -selected`, and the default selection holds no box: the RTL outputs describe the
  design's own modules. The FPGA passes end with `blackbox =A:whitebox`, and `yosys_synth` does
  the same before its netlist when `verilog_lib` is set (without it the script is as it was,
  which `tests/test_tool_input_equivalence.py` pins), so the netlist JSON never meets a
  whitebox. A new `write_json` needs one of the two. `post_rtl` blocks end their last command
  with a newline: an includer's `-%}` would otherwise glue the next command to it (the `.tcl`
  form failed in Tcl that way). CI's yosys has no Tcl, so `tests/test_yosys_templates.py`
  renders the three flows' scripts over every combination of RTL outputs and `stop_after`,
  checks each command is a line of its own, and runs the `.tcl` ones under `tclsh` with stub
  commands (`require_tclsh`); each glue fix has a revert that fails it.

  **`stop_after: rtl` is a stop the user asked for, so the run succeeds with the RTL outputs.**
  The two flows that have it (`yosys`, `yosys_fpga`; a flow that gains `stop_after` needs a recipe
  in `FLOWS` of `tests/test_yosys_stop_after.py`) leave every stage after the RTL one out of the
  script, the `.ys` and the `.tcl` template alike (no `exit`: the including template renders
  nothing after `post_rtl`), so the run writes no netlist and no report, and `utilization_report`
  is not registered as an artifact: an artifact listed on a success must exist. What asks for a
  result of those stages is refused when the settings are checked, before anything runs, by the
  setting's value (`common.stop_after_conflicts`, from each flow's `check_settings_supported`):
  `netlist_json` and `netlist_verilog` (both on by default, so the stop needs `-s netlist_json=
  netlist_verilog=`), `netlist_graph`, `write_blif`, `sta` and `ltp`. One rule for the declared
  output (`netlist`, which would fail as a missing output) and the plain artifacts alike. A flow
  that takes the netlist (`nextpnr`, `openroad`) cannot follow a stopped flow: its demand switches
  the netlist back on, so `YosysBase.enable_output` refuses it, and the resolver's message names the
  consumer (`yosys_fpga.netlist is required by nextpnr: ...`) -- the advice to turn the netlist off
  would be circular there, and stays for a stopped flow run alone.
- **Reject unsupported targets before producers run.** Declared flows use the pure class-level
  `check_settings_supported` hook after shared agreement (`nextpnr`'s target/config helpers; `fpga_pack` refuses a family it has
  no packer for).
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
  `XEDA_TESTS_OPENXC7=1` builds an Arty A7-100T design with the real openXC7 toolchain
  (`tests/test_openxc7_real.py`, `tool_utils.require_openxc7`; openXC7's `bin` first on `PATH`,
  run as `python -m pytest` since its `export.sh` also puts its own venv's `pytest` first): the
  bitstream, the same FASM features as the upstream Makefile's commands, a relaunch that runs
  nothing, a second design reusing the one chip database (generated once per session, shared
  by xdist workers under their common temporary directory), and the generator tree unchanged.
  It is its own variable, not `XEDA_TESTS_REQUIRE_TOOLS`: the tox legs have no openXC7. CI runs
  this layer in its own workflow, `.github/workflows/openxc7.yml` (`ubuntu-24.04`): openXC7's
  installer, pinned to one commit (`INSTALLER_REV`), builds yosys, nextpnr-himbaechel and
  `fpga-as` into `~/openxc7`, and `actions/cache` keeps that installation under a key of the
  commit, a `RECIPE` counter and the runner image. The chip database is generated on every run.
  A cache miss installs what the installer needs to build (its list, less Java and pypy3, which
  serve tools this job does not build: the workflow edits them out of the installer's clone), then
  lists the Debian packages that the built tools load (`ldd` of every executable of `bin/`, then
  `dpkg -S`) in the cached prefix, `share/xeda-ci/runtime-packages.txt`; a hit installs that list
  only, not the build dependencies (the mirror of the runners fails some of those downloads).
  The check of the installation runs before the cache is saved, so a broken tree is not cached.
  Raise `RECIPE` when the workflow changes what the cache holds, or when a hit fails because the
  list names a package that is gone. The job runs the Artix-7 cases only (the cache holds that
  family's Project X-Ray data), and it fails when any of them skips: a renamed opt-in variable
  would otherwise leave it green with nothing run. The cases share one run root and each reads
  the chip database of its own fabric (`tests/test_openxc7_real.py` names `xc7a100t`), so their
  order is free. Bump `INSTALLER_REV` in a pull request that runs the workflow, after reading the
  installer's diff: a new yosys or nextpnr can move the counts `tests/test_openxc7_real.py` pins.
  `XEDA_TESTS_ASAP7_PLATFORM` names an asap7 `config.toml` whose liberty files are present (the
  package ships the description only): `tests/test_yosys_asic.py` then checks, with the real
  yosys, that `yosys` alone and `openroad`'s dependency hand abc identical inputs for
  `corner=SS` and end alike (ABC crashes there on `main` too, so that is the accepted outcome).
- **No test programs a device, structurally.** `tests/conftest.py`'s autouse `programmer_guard`
  puts a sentinel `openFPGALoader` first on every test's `PATH` (the fake toolchain goes in front
  of it; child processes and the popen remote worker inherit it) and fails a test that started
  it or whose final `PATH` selects any loader but the fake or the sentinel. Loader tests still
  assert on the fake's call record. Never run a real `openFPGALoader` from a test or a probe.
  The flow starts the loader twice, once for its version (`-V`, capital V: it has no `--version`,
  and the fake rejects that spelling as the real one does, so a flow asking the wrong way records
  an empty version) and once to program, its output kept in `openfpgaloader.log` in the run
  directory (`tee`: each run makes the log anew before the loader starts). The guard keys on identity, never on a count: the fake
  answers both starts and never touches it, while the same query against the sentinel or any other
  loader fails the test like the programming call would (no sentinel answers `-V`: a loader
  reached without the fake is the `PATH` that would program on the next call).
- **A programmer run passes only if the loader's output shows no failure.** openFPGALoader (read
  at v1.1.1) exits 0 after most failures: Xilinx `program_mem` prints the readback `ir: ... done 0`
  and a status-register dump and returns, an unreadable bitstream prints `FAIL` and returns, a flash
  write ignores `SPIInterface::write`'s result; Lattice throws (status 1); Gowin and iCE40 print
  `FAIL`/`Fail` and return. So `Openfpgaloader.parse_reports` judges this run's `openfpgaloader.log`
  (`report_file`: a log this run did not write, or none, fails) with the pure
  `loader_failure(text, target)`, after `_output_lines` drops escape codes and carriage returns: DONE
  low (the register's ID and CRC error fields give the cause), a line ending `FAIL`/`Fail`, and the
  messages of the paths whose result the loader ignores (`program_spi`, the CPLD and PROM
  programmers): a line starting with a key of `FAILURE_PREFIXES` (`Error: `, `Can't program `,
  `FAIL: `, `Verification failed at `) or ending with a key of `FAILURE_ENDINGS` (`Read ID failed`,
  `flash overflow`, `wait: Error`, ...; an end, since a terminal's progress bar has no line end and
  the next message is glued to it). A table value is the file:line of v1.1.1 that prints it, and
  `tests/test_openfpgaloader_verdict.py` sweeps both tables, so a new sign is one entry with its
  citation. `Can't program ... missing device-package information` (a board the loader lists
  without a part, such as `kc705`, with `write_flash`) adds sentences that say to set `cable`; a
  board name still gives no `--fpga-part`. The verdict requires **one success marker, of a Xilinx
  flash write only** (`Openfpgaloader._flash_writes`: `write_flash` on a Xilinx `fpga`, two writes
  with `target_flash: both`): the `Writing` bar at its end followed by `Done`, or `Writing: Done`
  with quiet bars (`_finished_flash_writes`). The loader can stop before it writes and say nothing
  (`SPIFlash::global_unlock` of an SST26VF that stays locked, spiFlash.cpp:1212-1215), and `verify`
  does not report that, since `SPIInterface::write` verifies only after a write that succeeded. The
  module's comment holds the citations and why the bar is the same from v0.13.1 to v1.1.1; the
  tests' bytes come from a program that links the loader's `progressBar.cpp` and `display.cpp` of
  the tag and nothing else (no loader), to a pipe and to a terminal. A later release that prints
  another bar fails every such write, which is the point; the error (`_unwritten_flash`) is built
  from the run (the line of its verbosity, `verbose_level: -1` or `--quiet` giving `Writing: Done`;
  the writes it needs) and from the log (the writes found; what follows the last one). Nothing else
  needs a marker (other families print other words, and a required marker would fail good runs), so
  a failure without one of these signs passes; a nonzero exit keeps its `NonZeroExitCode`. The
  loader must be 0.13.1 or newer (`MIN_OPENFPGALOADER_VERSION`: the first release with the DONE
  readback that says so in `-V`; the tag v0.13.0 has the readback and prints 0.12.1, as its
  CMakeLists names that version): `OpenfpgaloaderTool.minimum_version`, checked by `Tool.__init__`
  from the `-V` query, when `Openfpgaloader.init` makes the tool, so a loader that is too old is
  refused before any producer runs (a plan or dry run starts no loader); the fake loader's
  `XEDA_FAKE_FPGA_LOADER_VERSION` sets what `-V` prints. The failure is the node's `ReportedFailure`
  with the flow's own message in plain sentences and the quoted lines. The file:line citations are
  in the tables and the module's comment: add a sign to a table with its citation and a log in
  `tests/test_openfpgaloader_verdict.py`. The fake loader prints
  `XEDA_FAKE_FPGA_LOADER_STDOUT`/`_STDERR` and exits with `XEDA_FAKE_FPGA_LOADER_STATUS`, and
  rejects an option outside `LOADER_OPTIONS` (the v1.1.1 table, from `src/main.cpp`) as the real
  parser does: a new loader setting needs its option in that table, and
  `test_every_setting_of_the_loader_is_an_option_the_loader_has` sets every setting at once;
  `tests/resources/openfpgaloader/*.txt` are real v1.1.1 logs (a failed Basys 3 load, a good Arty
  load; `.log` is git-ignored). `tests/test_openfpgaloader_verdict.py` also sweeps that every flow
  with an `action_reason` judges its tool's output (`unjudged_actions`).
- **A change that a user can see adds a changelog fragment; nobody edits `CHANGELOG.md`'s
  `[Unreleased]` section**, so no two pull requests touch the same lines. One file per entry,
  `changelog.d/<slug>.<type>.md`: `<slug>` is kebab-case (the pull request number is not known
  yet), `<type>` is `fixed`, `added`, `changed` or `removed`, and the file is one Markdown bullet
  wrapped at 100 columns, each later line indented by two spaces. The entry says what changed for
  a user in one or two short sentences (a breaking change says what to do instead); mechanism,
  evidence and rationale go to the docs, this file or the pull request text.
  `tools/fold_changelog.py` holds the rule for a fragment and the fold.
  `tests/test_changelog_fragments.py` fails a fragment that breaks the rule and a `###` heading
  that a section of `CHANGELOG.md` has twice. Every file in `changelog.d/`, hidden or not, must
  be a fragment, except `.gitkeep` (it keeps the directory) and `.DS_Store` (macOS Finder writes
  it into any folder it shows): those two are `NON_FRAGMENT_FILES` in the tool. **On release**,
  run `python tools/fold_changelog.py vX.Y.Z [--date YYYY-MM-DD]` and commit the result. It adds the
  fragments, sorted by slug, to the end of the matching lists of the `## [Unreleased]` section
  (or of a new section above the newest release, if the file has no `[Unreleased]` section),
  renames that section `## [vX.Y.Z] - <date>`, and deletes the files. It refuses a list of that
  section that holds a line other than a bullet or the continuation of one (a note, a link
  reference), so an entry never lands after such a line. It does not make a new `[Unreleased]`
  section: the fragments are the notes of the next release. The entries written before fragments
  existed stay in `[Unreleased]` and join the first release.
- Formatting: `black` (line-length 100) is enforced on `src/`, `tests/` and `tools/` (`tox -e
  black`); the Pyflakes rules and `PLW0133` (a built-in exception built and never raised) of
  `ruff` (`ruff check --select F,PLW0133 src tests tools`, line-length 120,
  `target-version = "py311"`) are enforced there too, and the rest of `ruff`'s ruleset is not.

YAML is the preferred design/project authoring format; TOML and JSON remain accepted. All YAML
input goes through `yaml_loader.load_yaml`: YAML 1.2 core scalars, string mapping keys and
duplicate-key rejection. Quote string parameters and source paths. Use lowercase `true`/`false`
for booleans; merge keys, recursive aliases and non-core tags are rejected. Keep bundled board
and platform databases in TOML.
