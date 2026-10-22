# CLAUDE.md

Most rules below are enforced by the test named in parentheses; that test is the specification,
so read it before you change the behavior.

Xeda drives simulation, synthesis and implementation flows of commercial and open-source EDA
tools from one declarative design file. Package `src/xeda`, entry point `xeda.cli:cli`. Python
3.11-3.15; pyproject, tox and CI must agree (`tests/test_python_support.py`). For the CLI and
design files, read `.claude/skills/xeda/SKILL.md`. Write new designs and examples in YAML.

## Commands

```bash
uv sync                                   # or: python -m pip install -U --editable .
pytest tests/ -n logical                  # full suite (pytest-xdist); serial: pytest tests/
tox -e mypy,black,ruff,docs               # the quality gates CI runs; all must exit 0
tox                                       # py311..py314-compat + full py315 + mypy, black, ruff
python tools/generate_flow_catalog.py [--check]   # regenerate/check src/xeda/flow_catalog.json
```

- Gates: `mypy src`; `black --check src tests tools` (line length 100); `ruff check --select
  F,PLW0133 src tests tools`. The full `ruff check` ruleset is not enforced: keep new code
  clean, do not mass-fix old findings. Sphinx docs build with warnings as errors.
- CI runs the full suite on 3.15 only, as four parallel jobs (`--shard K/N` keeps the tests whose
  node id hashes to K, `tests/sharding.py`); 3.11-3.14 run `-m "python_compat and not real_tool"`,
  so mark Python or dependency compatibility tests `python_compat`. A test of OS-dependent code
  (locks, process groups, pseudo-terminals, file copy, file-system clocks, links) goes into the
  file list of the `macos` job in `.github/workflows/ci.yml`.
- The suite is parallel-safe and must stay so (`addopts` has no `-n` on purpose). A test writes
  only under `tmp_path` (opt-in layers: the checkout's `xeda_run/`), takes no fixed name and
  leaves no process-wide change: use `monkeypatch` (a direct `os.environ` write fails,
  `tests/test_environment_isolation.py`), never `chdir`, and restore flow registrations with
  `preserved_flow_registry` (`tests/flow_registry.py`). The checkout guard (`tests/conftest.py`)
  fails a test that changes a file in the checkout.
- Real-tool tests skip when the tool is missing (`require_*` in `tests/tool_utils.py`);
  `XEDA_TESTS_REQUIRE_TOOLS=1` (set in CI) makes a skip fail. Opt-in layers: `XEDA_TESTS_VIVADO`,
  `XEDA_TESTS_DOCKER`, `XEDA_TESTS_EXTERNAL`, `XEDA_TESTS_OPENXC7`, `XEDA_TESTS_OPENROAD`,
  `XEDA_TESTS_YOSYS_TCL` (a yosys with Tcl), `XEDA_TESTS_PDK_ROOT` (a PDK store) with
  `XEDA_TESTS_PLATFORMS` (the bundled platforms it holds) and `XEDA_TESTS_PDK_PROVIDERS` (extra
  `platform:provider` pairs it holds), `XEDA_TESTS_ASIC_QOR` with
  `XEDA_TESTS_ORFS` (an OpenROAD-flow-scripts checkout), `XEDA_TESTS_REMOTE_HOST` (a Linux host
  that `ssh` reaches without a prompt; `tools/remote_test_sshd.py start` runs one in Docker) with
  `XEDA_TESTS_REMOTE_SSH_CONFIG` (its `ssh -F` file).
- `pytest &` in a non-interactive shell ignores SIGINT: a test that interrupts a tool sets
  `DEFAULT_SIGINT` (`tests/test_proc_utils.py`) and waits for a ready signal, never a sleep.

## Run directories

A run directory is `<run root>/<design>[/<target>]/<flow>[_<run hash>]` (the hash only with
`--hashed-run-dirs`; run root `./xeda_run/`). A dependency's run directory is a sibling, never
nested. Launch and `xeda scrub` share one rule, `run_dir.run_directory_problem`
(`tests/test_run_directory_rule.py`). No symbolic link lies between the run root and a run
directory (design, target, mirror `@host`): `run_dir.link_on_way` (`tests/test_run_root_links.py`).
User-facing rules: `docs/run-directories.rst`.

## Architecture

- `Design` (`design.py`, `rtl` and `tb`; targets fold in at load, `tests/test_targets.py`),
  `Flow` (`flow/flow.py`; flows in `flows/<tool>/`, bases `SimFlow`, `SynthFlow`,
  `FpgaSynthFlow`, `AsicSynthFlow`) and `Tool` (`tool.py`, native or Docker) are decoupled.
- `FlowLauncher` (`flow_runner/default_runner.py`) holds the launch skeleton; each stage is a
  module of free functions (`launch_*.py`, `plan_freshness.py`, `scrub.py`) that never imports
  `default_runner` (`tests/test_launcher_modules.py`). Patch a name on the module whose code
  looks it up (`tests/test_patch_targets.py`). `resolver.py` plans every run before any flow is
  built. `xeda/introspect.py` is the one source of metadata for the CLI, docs and skill.
- `--remote` (`xeda/remote/`) runs the whole launch on the host through one OpenSSH master per
  launch (no option that weakens authentication or host keys); both ends run the same provisioned
  code, so there is no version check (`tests/test_remote_client.py`, `tests/test_remote_agent.py`).

### Flow lifecycle

`launch_flow` runs every flow and producer: input settings -> identity (hashes, run directory, lock)
-> the design's converter, if any -> build the flow from deep copies (of the derived design),
`init()` -> producers -> `prepare_inputs()` -> freshness (`trace.check_trace`; a match reuses the
results) -> `run()`, `parse_reports()`, `check_results()` -> `results.json` -> `trace.json`, last
and only for a passed or rejected run (`tests/test_rejected_verdicts.py`).

- Never modify the input settings or the design; the flow gets copies
  (`tests/test_flow_design_isolation.py`). A launch leaves no process-wide state
  (`tests/test_isolation.py` sweeps it, `tests/test_shared_state.py` has its offenders).
- `init()` runs even for a fresh flow: no dependency, no input read, no file written. A file it
  or `prepare_inputs()` registers in `Flow.implicit_inputs` needs a `locate_prepared_inputs()`
  counterpart, since `--dry-run` never calls `init()` (`tests/test_prepare_inputs.py`,
  `tests/test_dry_run_freshness.py`).
- A refusal that needs only settings and design goes in the pure class methods
  `check_settings_supported` / `check_design_supported` (call `super()`), never in `init()`,
  `prepare_inputs()` or a constructor (`tests/test_refusals_at_planning.py`).
- Read every report or log through `Flow.report_file(path)`, which ignores a file this run did
  not write (`tests/test_stale_reports.py`).
- A failure after a run starts leaves `results.json` with `success: false`, `error.type` and
  `error.message` (`tests/test_failure_results.py`). `ReportedFailure` is a node's cause;
  `FlowFailed` is the verdict of `xeda run --json`. Only a rejection (`Flow.reject` in
  `check_results()`; `parse_reports()` only reads, `tests/test_verdict_placement.py`) keeps
  the trace and is reported again unchanged; every other failure keeps none.
- `Flow.always_runs()` returns a reason or `None`; every override calls `super()`. A simulation
  passes only on evidence that it ended (`judge_evidence`, `tests/test_sim_evidence.py`).

### Declared inputs and outputs (the only dependency mechanism)

- Declare files in nested `Inputs(Flow.Inputs)` / `Outputs(Flow.Outputs)` with `In` / `Out`
  (`flow/io.py`), each with a `description`; the annotation is the cardinality (`Path`,
  `Path | None`, `list[Path]`). A flow reads only `self.inputs`, never another flow's settings,
  directories or results (`tests/test_one_dependency_mechanism.py`, `tests/test_isolation.py`).
- Use `Out(enabled_by=...)` only for expensive outputs: a consumer that switches an output on
  changes the producer's identity. Write cheap outputs always.
- An input comes from an explicit binding (chain `a+b` or `flows.<flow>.inputs.<input>`), else a
  matching design source, else the default producer. A new edge between netlist or checkpoint
  types needs `REVIEWED_EDGES` (`tests/test_stage_types.py`). A netlist, checkpoint or database
  `SourceType` has one row in `artifact_kinds.KINDS`, and a flow declares a type only for what its
  settings write; `output_types` narrows an output to `()` for the file it does not write
  (`tests/test_artifact_kinds.py`).
- `In(DESIGN)` / `Out(DESIGN)` is the design kind: a flow that reads design sources has a `design`
  input. Only a flow that defines `design_conversion` (and then declares one design output) is a
  converter: the resolver plans it as the default producer of a flow that cannot read the
  sources, and a binding follows the rule of a chain. A design output without a conversion is
  allowed and is never a producer of a `design` input. A generated source counts by its content,
  type and name within the converter's output, never by its path
  (`tests/test_design_edges.py`, `tests/test_generated_names.py`).
- A node's identity is its settings hash plus its ordered input origins
  (`bindings.node_identity`). A producer's settings never depend on which consumer asked.
- **Shared leaves agree along declared edges.** `fpga`, `board`, `custom_boards_file`, `clocks`,
  `prjxray_db`, `platform`, `corner`, `dont_use_cells`, `platform_file`, `liberty`, `pdk_provider`,
  `pdk_version`, `pdk_root`, `hierarchical` and `wire_rc` (`SHARED_SETTINGS`) must agree where both
  endpoints declare them. Keep these names first, in order (`tests/test_documentation.py`).
- YAML snippets in `docs/` and the skill run as tests (`tests/test_chain_documentation.py`); a
  new snippet is YAML that starts with `# <name>.yaml`.

### Adding or changing a flow

- Add the class to the import list and to `__all__` in `xeda/flows/__init__.py`
  (`tests/test_flow_registry.py`). After a change to flows, aliases, I/O, settings models, or
  `action_reason`, run `python tools/generate_flow_catalog.py` and commit the catalog
  (`tests/test_flow_catalog.py`).
- OpenROAD flows: every OpenROAD process (`openroad` runs two or three) starts with the restart
  header; state the `.odb` stores has one owner (`openroad_place`); the header re-applies the rest
  (`tests/test_openroad_templates.py`).
- Each flow needs its own docstring, a `results_description` from `describe_results(...)` (`{}`
  if none) and `description=` on every settings field (`tests/test_documentation.py`).
- `Flow.executables` lists every program the flow may start, helpers included; `xeda remote check`
  reads it (`tests/test_flow_executables.py` scans `Tool(...)` and `.derive(...)` calls).
- A flow that reads design sources declares `reads_sources` and iterates `sources_read()`
  (`None`: declared inputs only). Declare `design_parts` (with `tb` if code or a template reads
  it) and `design_source_types` completely; a missing part is a stale reuse
  (`tests/test_source_contracts.py`, `tests/test_design_parts.py`). Compare
  `src.type is SourceType.X`, never text. Append `SourceType` members, never reorder.
- Every written setting has a role: `WORKING` (a name in the run directory),
  `deliverable(conventional=...)` (the run writes that name; `xeda/deliver.py` copies it to the
  given location after the launch) or `waveform(<ext>)`; add it to `ROLES` in
  `tests/test_written_paths.py`. The identity counts the name the run writes, never the
  destination (`tests/test_deliverable_identity.py`).
- A setting that names where installed data lives (`pdk_root`) carries `LOCATOR`: no part of the
  identity, never traversed; the flow registers the exact files (`tests/test_locator_settings.py`).
- A provider with a layout of its own (`lambdapdk`) gives its own `files` and `values` in its
  residue entry. Every `effective_residue` call passes the provider argument explicitly: the
  selected provider, or `None` for the default provider of the layers
  (`tests/test_lambdapdk_provider.py`).

### Settings and pydantic

- `class Settings(<Base>.Settings)` is a pydantic 2 `XedaBaseModel`: `extra = forbid`,
  `validate_assignment`, `validate_default=True` (never opt out; the validator must accept the
  default). Import `field_validator` / `model_validator` from `xeda.dataclass`, never from
  pydantic, with `@classmethod` under each.
- Every `Flow.Settings` field needs a default (`Optional[X] = None`; else a `TypeError` at class
  creation). A setting the flow cannot run without goes in `Flow.required_settings`, never on the
  model or in `run()` (`tests/test_required_settings.py`). No custom `__init__`; user input goes
  through `from_input`.
- A flow's code or templates read every setting its model has, group leaves included, or the model
  refuses it; a setting no flow reads is removed, not kept (`tests/test_unread_settings.py`).
- Many tool options go in `SettingsGroup` models (one per command or pass): each leaf has a
  `tier(...)` and a description, flags render from `FLAGS` by `render_flags` (one call per group),
  and `extra_args` is checked at planning against `EXTRA_FLAGS` (`tests/test_settings_groups.py`).
- A removed setting fails with "`<name>` was removed:" and either the setting to use or, for
  `NoReplacement`, why there is none ("...; delete it"). The entry is in `removed_settings` of the
  class that had the field (`tests/test_settings_a_flow_does_not_use.py`).
- A setting accepts exactly its type; never add an implicit conversion. A boolean takes only
  `true`/`false`; list text is comma-separated and `[...]` text is refused; `Code` is for
  number-like text (`tests/test_settings_input.py`, `tests/test_malformed_input.py`).
- A physical measurement is a quantity type from `xeda.units` with a unit, never a bare number;
  every other numeric setting carries one marker (`count`, `ratio`, `percent`, `library_units`,
  `data_size`); the test's `NOT_MIGRATED_YET` table, which only shrinks, lists what is left
  (`tests/test_physical_quantities.py`).
- Validators are idempotent (assignment and `settings.json` reload re-run them), log nothing
  above DEBUG, and never read another field (`info.data`) (`tests/test_model_invariants.py`).
  Format for a tool in the template, not in a validator.
- A `mode="before"` model validator runs on every assignment and its writes stick: find the
  assigned field with `info.field_name if info.data is None else None` and change nothing else.
  A subclass's `before` validators run first. Copy a nested model instance before normalizing.
- A field holding a subclass needs `SerializeAsAny[...]`. Read state with `utils.model_state()`,
  not `__dict__`. Write JSON through `utils.json_encodable`; a non-pydantic type defines
  `as_json_value()`. Call `invalidate_cached_properties()` after `model_copy(update=...)`.
- Settings layers merge in one place (`settings_layers.merge_layers`), and every entry point
  takes the same layers (`tests/test_launch_origins.py`); precedence: `docs/flows.rst`.

### Writes, caching and isolation

- Runs are make-like; hashes are semantic and location-free; writes are judged by file identity.
- Code digests (`flow_runner/code_digest.py`) list and check every file at each launch, by the
  trace's record rule (`digest.record_file`), with names relative to the code root
  (`tests/test_code_digest.py`); a flow class without a source file
  always runs, through `Flow.always_runs`, and gets no digest.
- Every write in a run directory goes through `RunDirectory` (`xeda/run_dir.py`):
  `writable(path)`, then `utils.replacing_file` / `replacing_copy`; delete with `remove`,
  `clear`, `delete`. Tool logs go through `utils.live_log` only. Never write, delete or follow
  through a symbolic link: a tool may leave links anywhere in its run directory.
- A launch writes nothing outside the run root but deliveries (a design's generator writes its
  declared sources itself); caches live under the run root and are named through
  `RunDirectory.unlinked`. `tests/test_isolation.py` checks it (canary sweep, audit hook,
  read-only tree); a new delete or raw write fails until it is added to `REVIEWED_PY_DELETIONS`,
  `REVIEWED_SCRIPT_DELETIONS` or `REVIEWED_WRITES`.
- Generators and Git fetches run only after the declarative checks pass
  (`tests/test_checks_before_generation.py`); print a URL only via `design.redacted_url`.

### Templates, tools and the CLI

- Tool scripts are Jinja2 templates in `templates/` beside the flow module (`StrictUndefined`,
  inherited by subclasses). Add a new template extension to `[tool.setuptools.package-data]`.
- User text reaches a script only through a filter, never raw or through `eval`: Tcl `tcl_word`,
  `tcl_quote`, `tcl_list`; yosys `read_path`, `path`, `verbatim_path` (`tests/test_tcl_paths.py`,
  `tests/test_yosys_templates.py`).
- On POSIX, `run_process` starts every tool as the leader of its own session, so a stop (Ctrl-C,
  SIGTERM, SIGHUP, a time limit) ends its whole group; start every tool through it
  (`tests/test_proc_utils.py`).
- Create a `Tool` inside a flow method (it finds its flow); query its version only with
  `Tool.probe_stdout`, which runs outside the run directory.
- Fake tools: `tool_utils.use_fake_tools(monkeypatch)` runs Tcl scripts under `tclsh` and
  records tool commands (`fake_calls`, `XEDA_FAKE_TOOL_FAIL`). A new fake is a symlink to
  `tests/fake_tools/fake_tool.py` plus a `fake_tools` entry. No test starts a real
  `openFPGALoader` (`programmer_guard`). The fake `ssh`, `uv` and `curl` are in
  `tests/fake_tools/remote/`, on a `PATH` only through the remote fixtures.
- `--json` puts exactly one JSON document on stdout; tool output goes to stderr, so a process
  started outside `run_process` must honor `proc_utils.tool_output_redirect()`. Every failure,
  usage errors too, emits `{"success": false, "error": ...}` (`tests/test_cli_json_contract.py`).
- The library never prompts: an unanswered question answers "no". Only `cli.py` asks, through a
  callback it passes (`confirm_overwrite`, `confirm_scrub` / `scrub_design(confirm=)`);
  `--remote --scrub` is refused at planning (`tests/test_remote_client.py`). A prompt call
  elsewhere in `src/xeda` fails `tests/test_scrub_no_prompt.py`.
- CLI: two parameters never share a destination; commands use `XedaCommand`
  (`tests/test_hidden_options.py`); options read only declared environment variables; a behavior
  switch is a flag (`tests/test_option_names.py`). Compare help text via `click.unstyle(...)`.

## Changelog

- A user-visible change adds `changelog.d/<slug>.<fixed|added|changed|removed>.md`: a kebab-case
  slug, one Markdown bullet wrapped at 100 columns (later lines indented two spaces), one or two
  short sentences for users (`tests/test_changelog_fragments.py`). Never edit `CHANGELOG.md` by
  hand: on release, `python tools/fold_changelog.py vX.Y.Z` folds the fragments into it.
