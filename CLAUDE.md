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
tox                                  # CI matrix: py311-py314 + mypy + black + ruff
tox -e mypy                          # mypy --install-types --non-interactive src - currently clean
tox -e black                         # black --check --diff src tests (line-length 100) - clean
ruff check src tests                 # .ruff.toml, line-length 120
```

`jsonschema` is a test-only dependency (in the `dev` group and in tox), used to check that the
published design schema agrees with the loader.

`mypy src`, `black --check src tests` and the Pyflakes rules (`ruff check --select F src tests`, the
`tox -e ruff` env) all pass; keep them that way. The full `ruff check` ruleset reports many
pre-existing findings (mostly `UP006`/`UP007` PEP-585/604 annotations and `RUF012`) and is not
enforced. Don't mass-fix those; keep new code clean.

Most tests use `tests/fake_tools/`, but some end-to-end tests drive genuinely installed tools
(`test_ghdl.py`, `test_nvc.py`, `test_verilator.py`, `test_yosys.py`, `test_openroad.py`'s yosys
synthesis, the GHDL half of `test_remote_run.py`, parts of `test_cli_structured_output.py`).
Those **skip** when the tool is missing or installed-but-broken, via the probes in
`tests/tool_utils.py` (`require_ghdl()`, `require_yosys_ghdl_plugin()`, ...). Setting
`XEDA_TESTS_REQUIRE_TOOLS=1` turns those skips into failures; CI sets it, so a tool vanishing
from CI cannot look like a pass. `test_remote_run.py` and `test_dse_run.py` run
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
through, in named stages (each a method; the docstring lists them): **input** (`_input_settings`:
validate in context, apply `--debug`) -> **identity** (`_run_identity`: design hash + `flowrun_hash`,
run dir) -> **reuse** (`_previous_results`) -> **prepare** (construct the flow with its own *copy* of
the input, `init()`, write `settings.json`) -> **dependencies** (`_run_dependencies`, recursing) ->
**run** (`_execute`: `run()`, `parse_reports()`) -> **report** (`_report`).

- **The input settings are never modified.** The launcher keeps them (they are what the run is
  hashed by and recorded as `settings.json`'s `flow_settings`); the flow gets a deep copy as
  `self.settings`, which `__init__`/`init()`/`run()` may complete with resolved paths, derived
  options and outputs (recorded as `effective_flow_settings`, as of the end of the run).
- **Neither is the design.** It is hashed before the flow runs, recorded beside that hash and
  handed on to the dependencies, so the flow gets a deep copy as `self.design`; nothing a flow
  does to it reaches anyone else (`tests/test_flow_design_isolation.py`). Still, a flow computes
  what it derives from the design where it uses it (a template global such as `top_is_vhdl()`)
  rather than editing it.
- A flow's settings come from layers merged key by key (`flow_runner/settings_layers.py`):
  defaults < project `flows.<flow>` < design `[flows.<flow>]` < `-s` < API overrides. Local runs,
  remote runs and dependencies all use `merge_layers`. Under the field holding a declared
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
With `--cached-dependencies`, a dependency whose `settings.json` records matching hashes and whose
`results.json` reports success is skipped and its results/artifacts reused.
`--incremental` (default) drops the design hash from the path so repeated runs reuse one directory.

### Other runners

- `flow_runner/dse/` - `Dse` launcher running many flow instances in parallel (`pebble`) under an
  `Optimizer`; `FmaxOptimizer` (`fmax.py`) does the binary/interpolation search for max clock frequency.
- `flow_runner/remote.py` - `RemoteRunner` ships the design over SSH via `execnet`, runs xeda remotely,
  and streams tool stdout/stderr back through a PTY pair. The streaming/PTY behavior is heavily tested
  in `tests/test_remote_streaming.py`; the code injected into the remote (`STREAM_OUTPUT_SETUP`,
  `remote_runner`) must stay dependency-free and only use long-stable xeda API - a test asserts this.
  The design archive `send_design` builds is read by the *remote's* xeda, which forbids unknown
  keys, so it must stay loadable by `REMOTE_XEDA_MIN_VERSION`. **Requirement: a remote runs the
  latest published xeda or newer** (currently 0.4.0), and `check_remote_xeda` refuses anything
  older. On each release, raise `REMOTE_XEDA_MIN_VERSION` and `test_remote_run.py`'s
  `RELEASED_RTL_KEYS`/`RELEASED_TB_KEYS` to the new release; until then the archive and the
  shipped `remote_runner` may rely on nothing newer than it. `REMOTE_PROBE` reports
  which xeda the remote interpreter imports (execnet starts `python3` from the *non-login* PATH).
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
  every tool command recorded. PDK files and xeda's own run-directory paths are left raw.
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
  them on `PATH`; `tool_utils.fake_calls(run_dir)` reads what the scripts ran. To fake a new tool:
  add a symlink, add an entry to the `fake_tools` dict (the option or argument naming its script,
  for `RunTcl`), and use `use_fake_tools`.
- **Real proprietary tools and containers are opt-in layers**, skipped unless their variable is set
  (and then failing on what they need): `XEDA_TESTS_VIVADO=1` runs Vivado flows on tiny designs
  (`tests/test_vivado_real.py`, `vivado` on PATH); `XEDA_TESTS_DOCKER=1` runs flows `dockerized`
  in their default images (`tests/test_dockerized.py`), skipping one whose image is not present
  locally -- a test never pulls. Both work under the checkout's `xeda_run/` (or
  `XEDA_TESTS_WORK_DIR`), which a container can mount where the system temp directory is not.
- Formatting is inconsistent by design: `black` (line-length 100) is enforced on `src/` only; `ruff`
  (line-length 120, `target-version = "py311"`) checks the whole repo.
