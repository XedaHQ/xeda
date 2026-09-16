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
pytest tests/                        # full test suite
pytest tests/test_vivado.py::test_vivado_synth_py -s -v   # single test
tox                                  # CI matrix: py311-py314 + mypy + black
tox -e mypy                          # mypy --install-types --non-interactive src - currently clean
tox -e black                         # black --check --diff src (line-length 100) - currently clean
ruff check src tests                 # .ruff.toml, line-length 120
```

`jsonschema` is a test-only dependency (in the `dev` group and in tox), used to check that the
published design schema agrees with the loader.

`mypy src` and `black --check src` both pass as of now; keep them that way. `ruff check src tests`
reports many pre-existing findings (mostly `UP006`/`UP007` PEP-585/604 annotations and `RUF012`) - it is
*not* enforced: `envlist` names a `ruff` env but tox.ini has no `[testenv:ruff]`, so `tox -e ruff` just
runs pytest. Don't mass-fix those; keep new code clean.

Most tests use `tests/fake_tools/`, but four end-to-end tests drive genuinely installed tools
(`test_ghdl.py`, `test_nvc.py`, `test_verilator.py`, `test_yosys.py`). They **skip** when the tool is
missing or installed-but-broken, via the probes in `tests/tool_utils.py` (`require_ghdl()`,
`require_yosys_ghdl_plugin()`, ...). Setting `XEDA_TESTS_REQUIRE_TOOLS=1` turns those skips into
failures; CI sets it, so a tool vanishing from CI cannot look like a pass. Tests that exercise
cocotb-based example designs need `pip install -r examples/requirements.txt`.

Running flows manually:

```bash
xeda list-flows                      # all registered flows
xeda list-settings vivado_synth      # settings schema for a flow
xeda list-results vivado_synth       # result keys a flow writes to results.json
xeda design-schema                   # JSON Schema of a design file
xeda list-boards / list-platforms / list-optimizers
xeda run vivado_synth examples/vhdl/sqrt/sqrt.toml -s clock_period=5.0 impl.strategy=Debug
xeda dse vivado_synth --design <file>  # parallel design-space exploration (Fmax search)
xeda scrub <flow> <design_name>      # remove previous run dirs
```

Flow runs land under `./xeda_run/` (configurable via `--xeda-run-dir` / `XEDA_RUN_DIR`). The exact
layout depends on two options - hashes appear **only** with `--cached-dependencies`:

| options | path |
| --- | --- |
| *(default)* | `<design>/<flow>/` |
| `--cached-dependencies` | `<design>/<flow>_<flowrun_hash>/` |
| `--cached-dependencies --no-incremental` | `<design>_<design_hash>/<flow>_<flowrun_hash>/` |

`--no-incremental` on its own keeps the same path but backs up or removes the existing directory
first. Each run dir gets `settings.json` and `results.json`, plus `reports/`, `outputs/`,
`checkpoints/`.

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
both the schema and `Design.from_file`, so the two cannot drift.

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

`FlowLauncher.launch_flow()` drives: construct flow -> `init()` -> recursively launch dependency flows ->
`run()` -> `parse_reports()` -> collect `results`.

- `init()` (not `__init__`) is where a flow registers dependencies via
  `self.add_dependency(DepFlowClass, dep_settings, copy_resources=[...])`. Deps run in nested run dirs
  and completed instances are available as `self.completed_dependencies` / `self.pop_dependency(Cls)`.
  Example: `VivadoPostsynthSim` depends on `VivadoSynth`; `Nextpnr` depends on `YosysFpga`.
- `run()` generates scripts and invokes tools. `parse_reports()` populates `self.results`;
  `self.results.success` decides pass/fail. Helpers: `parse_report_regex()`, `parse_regex()`,
  `parse_xml()` (`utils.py`).

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
integration keyed on `cocotb_sim_name`), `SynthFlow` (adds `clock_period` / `clocks` with
`PhysicalClock` reconciliation against `design.rtl.clocks`), and its `FpgaSynthFlow` (adds `fpga: FPGA`)
/ `AsicSynthFlow` specializations.

### Settings

Every flow declares a nested `class Settings(<Base>.Settings)`. Settings are pydantic models
(`XedaBaseModel`) with `extra = forbid`, so an unknown key in a design/CLI override is a hard error -
this is intentional and surfaces as `FlowSettingsError`. Fields typed `Path`/`Optional[Path]` get
`$PWD`, `$DESIGN_ROOT`, `$DESIGN_DIR` expanded by a catch-all validator. CLI `-s key=value` supports
dotted hierarchical keys.

**Every settings field must have a `description=`.** `tests/test_documentation.py` fails otherwise
(its allowlist is empty - all ~520 visible fields are documented). The same test requires each flow
to have its own docstring (not an inherited base-class one, which `xeda list-flows` used to show)
and to declare `results_description`.

### Results

Flows document the keys they write to `results.json` via a class-level `results_description`, built
with `describe_results(*shared_keys, **flow_specific)` from `xeda.flow`. Shared keys come from
`COMMON_RESULT_DESCRIPTIONS` in `flow/flow.py`; `describe_results` raises `KeyError` for a key that
is in neither, so it cannot silently invent a description. A flow that reports nothing beyond the
common keys declares `results_description = {}` explicitly. `xeda list-results <flow>` renders it.

After `parse_reports`, the runner calls `flow.add_canonical_result_aliases()`, which **additively**
copies flow-specific keys to canonical names per `Flow.results_canonical_aliases`
(`Fmax` <- `f_max`/`maximum_frequency`, `lut` <- `LUT`, `ff` <- `FF`). Nothing is renamed or removed.
Note `clock_frequency` is deliberately *not* aliased to `Fmax` - it is the constrained frequency,
not the achieved one.

### Templates

Tool scripts (TCL/SDC/XDC/YS/...) are Jinja2 templates in a `templates/` directory next to the flow
module. `Flow._create_jinja_env` builds a `ChoiceLoader` over `PackageLoader`s for the flow's own module
*and its base classes' modules*, so a subclass inherits its parent's templates. Undefined variables are
errors (`StrictUndefined`). `self.copy_from_template("x.tcl", **ctx)` renders with `settings`, `design`,
and `artifacts` in scope. New template file extensions must be added to
`[tool.setuptools.package-data]` in `pyproject.toml` or they won't ship in the wheel.

### Tool execution

Instantiating `Tool(...)` inside a flow method auto-discovers the calling `Flow` via `inspect.stack`, so
it inherits `dockerized`, `print_commands`, and console-color settings and appends its version info to
`flow.results.tools`. Subclass `Tool` to pin an executable, a default `Docker` image, `minimum_version`,
and `highlight_rules` (regex -> ANSI, used to colorize tool output) - see `VivadoTool`. Use
`tool.derive("other_exe")` to spawn a sibling executable from the same image/config.

### Caching and run directories

`launch_flow` computes `design_hash` (from `rtl_hash` + `tb_hash`) and `flowrun_hash` (from flow name +
settings) via `semantic_hash`. With `--cached-dependencies`, a dependency whose `settings.json` records
matching hashes and whose `results.json` reports success is skipped and its results/artifacts reused.
`--incremental` (default) drops the design hash from the path so repeated runs reuse one directory.

### Other runners

- `flow_runner/dse/` - `Dse` launcher running many flow instances in parallel (`pebble`) under an
  `Optimizer`; `FmaxOptimizer` (`fmax.py`) does the binary/interpolation search for max clock frequency.
- `flow_runner/remote.py` - `RemoteRunner` ships the design over SSH via `execnet`, runs xeda remotely,
  and streams tool stdout/stderr back through a PTY pair. The streaming/PTY behavior is heavily tested
  in `tests/test_remote_streaming.py`; the code injected into the remote (`STREAM_OUTPUT_SETUP`,
  `remote_runner`) must stay dependency-free and only use long-stable xeda API - a test asserts this.
- `platforms/` - ASIC PDK descriptions (asap7, nangate45, sky130hd/hs) for OpenROAD/DC;
  `board.py` + `data/boards.toml` for FPGA boards.

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
  is probed rather than assumed, which is what keeps cocotb 2.0 working. When a cocotb upgrade
  breaks every simulation at once, compare `Cocotb.env()` against `cocotb_tools/runner.py` first.
- **pydantic v1 is pinned** (`>=1.10.22,<2.0`). Use `validator` / `root_validator` / `ModelField` /
  `.dict()` / `.json()`, not v2 APIs. Import them from `xeda.dataclass` (which re-exports and adds
  `XedaBaseModel`, `XedaBaseModelAllowExtra`), not directly from `pydantic`.
- `XedaBaseModel.Config` sets `validate_assignment`, `arbitrary_types_allowed`, and
  `keep_untouched=(cached_property,)`. After `model.copy(update=...)`, call
  `invalidate_cached_properties()` - stale `cached_property` values are a recurring bug source (see
  `Tool.derive`).
- Fields ending in `_` (e.g. `design_root_`, `flow_settings_`, `runner_cwd_`) are internal and marked
  `hidden_from_schema=True`; they are excluded from user-facing settings docs.
- Most tests use fake EDA tools: `tests/fake_tools/` holds symlinks (`vivado`, `quartus_sh`,
  `xtclsh`, `dc_shell`) to `fake_tool.py`, a click-based stub that dispatches on `Path(__file__).stem`
  and writes canned reports (`tests/fake_tools/resource/fake_vivado_reports`). To fake a new tool: add a
  symlink, add an entry to the `fake_tools` dict, and have the test prepend `tests/fake_tools` to `PATH`.
- Formatting is inconsistent by design: `black` (line-length 100) is enforced on `src/` only; `ruff`
  (line-length 120, `target-version = "py311"`) checks the whole repo.
