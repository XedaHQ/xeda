*******************************************
Machine-readable output
*******************************************

Every part of Xeda's interface that a script - or a coding agent - needs to read is available as
JSON. This page is the reference for driving Xeda programmatically.

The rule
========

``--json`` always means *"a parseable result on stdout"*.

Commands split into two kinds, and the distinction is why there are two mechanisms rather than one:

**Query commands** produce a *document*. They take ``--format {table,json,jsonl,yaml}``, defaulting
to ``table``, with ``--json`` as shorthand for ``--format json``:

.. code-block:: bash

    xeda list-flows --json
    xeda list-settings vivado_synth --format jsonl
    xeda list-results vivado_synth --json
    xeda design-schema
    xeda list-boards --json
    xeda list-platforms --json
    xeda list-optimizers --json

**Executional commands** produce a *side effect* plus a stream of tool logs. They take a plain
``--json`` flag, which moves tool output, log messages and result tables to **stderr** so that
stdout carries only the JSON document:

.. code-block:: bash

    xeda run vivado_synth sqrt.yaml --json
    xeda dse vivado_synth --design sqrt.yaml --json
    xeda scrub vivado_synth sqrt --json

.. note::
   The table renderings are wrapped to the terminal width, and long values wrap across lines. The
   JSON, JSONL and YAML renderings never wrap or truncate, so they are what to parse.

Discovering what to run
=======================

``xeda list-flows --json`` returns one object per flow:

.. code-block:: json

    {
      "name": "vivado_postsynth_sim",
      "aliases": [],
      "class": "VivadoPostsynthSim",
      "module": "xeda.flows.vivado.vivado_postsynthsim",
      "qualified_name": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim",
      "description": "Synthesizes & implements the design, then runs ...",
      "category": "simulation",
      "supports_cocotb": false,
      "dependencies": ["vivado_synth"],
      "declared": false, "inputs": [], "outputs": [],
      "settings_class": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim.Settings"
    }

``name`` is the canonical name; ``aliases`` are the other accepted names. ``dependencies`` are
detected statically, so treat them as a reliable hint rather than a guarantee - a flow may add
dependencies conditionally at run time.

``declared`` distinguishes flows with explicit file I/O. Their ``inputs`` and ``outputs`` list
names, accepted source ``types``, ``cardinality`` (``one``, ``optional``, ``many``) and descriptions;
inputs also name ``producer``, ``output`` and ``optional``, outputs their ``enabled_by`` setting.
For example, ``nextpnr`` declares input ``netlist`` of type ``JsonNetlist``, default producer
``yosys_fpga``, output ``netlist``. Sources of an accepted type displace that default producer.
Use the resolved plan for the producers a particular request actually needs.

Discovering what to set
=======================

``xeda list-settings <flow> --json`` returns the flow's full settings, each under the name you can
type as ``-s <name>=<value>``:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "settings_class": "xeda.flows.vivado.vivado_synth.VivadoSynth.Settings",
      "fields": [
        {
          "name": "nthreads",
          "alias": "ncpus",
          "type": "integer",
          "required": false,
          "default": null,
          "description": "Max number of threads",
          "enum": null,
          "writes": null,
          "common": true,
          "declared_by": "xeda.flow.flow.Flow.Settings",
          "json_schema": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": null,
            "description": "Max number of threads",
            "title": "Ncpus"
          }
        }
      ],
      "definitions": {"...": "JSON Schema of the nested types"},
      "json_schema": {"...": "the complete JSON Schema of the settings"}
    }

* ``common`` marks the settings every flow accepts. ``--no-common`` omits them.
* ``alias`` is a second accepted name; both work.
* ``writes`` says whether the flow writes to the path this setting names: ``"working"`` (an
  intermediate location, always used as a bare name inside the run directory, whatever it is
  given -- ``sim_dir``, a log path), ``"deliverable"`` (an output that may be given a location,
  which is then delivered there once the run finishes -- ``fpga_pack``'s ``bitstream``, ``vcd``), or
  ``null`` when the setting is read, not written.
* ``enum`` lists the permitted values when a setting is constrained to a set.
* ``definitions`` describes nested setting types (``FPGA``, ``PhysicalClock``, ``RunOptions``, ...).
  This is xeda's own envelope key and keeps its name; inside ``json_schema`` the same types appear
  under JSON Schema's own ``$defs``.
* ``type`` is a short rendering for humans and agents, not JSON Schema. An optional setting reads
  as its underlying type (``integer``), while ``json_schema`` carries the literal
  ``anyOf: [..., {"type": "null"}]`` that pydantic emits.

``--format jsonl`` emits one field per line, each carrying its ``flow``, which is convenient for
streaming or grepping.

Discovering what comes back
===========================

``xeda list-results <flow> --json`` lists the keys the flow writes to ``results.json``, and the
canonical aliases the runner adds:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "documented": true,
      "keys": [
        {"name": "Fmax", "description": "Maximum achievable clock frequency in MHz, ...",
         "common": false, "documented": true}
      ],
      "canonical_aliases": {
        "Fmax": ["Fmax", "f_max", "maximum_frequency"],
        "lut": ["lut", "LUT"],
        "ff": ["ff", "FF"]
      },
      "note": "..."
    }

``documented: false`` means the flow's results have not been curated yet and the listed keys were
detected from its source as a best effort. In that case read an actual ``results.json``.

Validating a design file
========================

``xeda design-schema`` emits the JSON Schema of a design description, which is what to validate
against - or generate from - rather than guessing at the format. It is a **draft 2020-12**
document: nested types live under ``$defs`` and fixed-length tuples use ``prefixItems``. The
``$schema`` key always names the dialect the document actually conforms to, so point a validator
at that rather than assuming a draft.

Running a flow
==============

``xeda run <flow> <design> --json`` writes a single JSON object to stdout. Here ``nextpnr``
depends on ``yosys_fpga``, whose recorded run was still valid, and ``textcfg`` (its routed ECP5
design, as a Trellis text configuration) was given a location, so it was delivered there once the
run finished:

.. code-block:: json

    {
      "flow": "nextpnr",
      "design": "blinky",
      "success": true,
      "results": {"success": true, "Fmax": 212.4, "lut": 45, "...": "..."},
      "run_path": "/path/to/xeda_run/blinky/nextpnr",
      "results_json": "/path/to/xeda_run/blinky/nextpnr/results.json",
      "settings_json": "/path/to/xeda_run/blinky/nextpnr/settings.json",
      "nodes": [
        {"flow": "yosys_fpga", "run_path": "/path/to/xeda_run/blinky/yosys_fpga",
         "state": "fresh", "reason": "", "deliveries": []},
        {"flow": "nextpnr", "run_path": "/path/to/xeda_run/blinky/nextpnr",
         "state": "ran", "reason": "settings changed: seed",
         "deliveries": [
           {"setting": "textcfg", "from": "/path/to/xeda_run/blinky/nextpnr/config.txt",
            "to": "/home/user/blinky_config.txt", "state": "delivered"}
         ]}
      ]
    }

``nodes`` lists every run directory the run touched -- the requested flow's and each
dependency's -- once each, in completion order. Each entry's ``state`` is ``"fresh"`` (the
recorded run was still valid and was reused, without re-running), ``"ran"`` or ``"failed"``;
``reason`` is why it ran (empty for a fresh node), the same text logged as
``Running <flow>: <reason>``. Runs are make-like by default (without ``--rebuild-all``): a fully
fresh run is ``"success": true`` with every node ``"fresh"``. See :doc:`run-directories` for what makes
a node stale. A flow that always runs (``openfpgaloader``, which programs a device, or a flow
asked for a fresh random seed) is never ``"fresh"``; its ``reason`` says why.

``deliveries`` lists every output that node's flow copied to a location the settings or
``--outputs-to`` named, once the whole launch finished (see :doc:`run-directories`'s "Outputs
where you name them"): ``setting`` is the setting (or ``--outputs-to``) that asked for it,
``from`` the file in the run directory, ``to`` where it was copied, and ``state`` either
``"delivered"`` (a new copy was written) or ``"unchanged"`` (the destination already held exactly
that file). It is ``[]`` for a node with nothing to deliver, and a fresh node still lists its
deliveries: delivery follows a node's outcome, not whether its tool ran.

Declared output records appear as ``results.outputs`` in the CLI document and as the top-level
``outputs`` key in ``results.json``.
Each enabled output actually produced maps its name to ``{"path": ..., "sha": ...}``; a list
output maps to an ordered list of those records. ``path`` is absolute, inside the producer's run
directory; ``sha`` is its 32-character content digest, not a timestamp. Recording checks readable
files, containment and current-run writes. Hand-over checks record schema, cardinality,
containment and content under a verified producer read lease, whether the producer ran or was
reused. Output-record validation failures use ``MissingOutput`` and the normal failure identity.
Flow-specific checks can fail earlier: ``nextpnr`` raises ``FlowFatalError`` naming an enabled
configuration setting/path that its tool did not write. Disabled outputs are omitted.
``outputs`` is bookkeeping, like ``artifacts``, rather than a ``list-results`` metric; it is
omitted from the printed result table.

On failure the document carries an ``error`` object alongside any available results, and the exit
status is non-zero. ``nodes`` still lists every node that ran before the failure, and the one that
failed; it is ``[]`` when the error happened before any flow could run (a bad design file, an
unknown flow or setting):

.. code-block:: json

    {
      "flow": "vivado_synth",
      "design": "sqrt.yaml",
      "success": false,
      "results": {},
      "error": {
        "type": "FlowSettingsError",
        "message": "FlowSettingsError: 1 error validating VivadoSynth.Settings\n   Extra inputs are not permitted: no_such_setting (extra_forbidden)"
      },
      "nodes": []
    }

``error.type`` names the exception class, which is stable enough to branch on:
``FlowSettingsError`` (a bad setting), ``FlowNotFoundError`` (a bad flow name),
``ExecutableNotFound`` (the tool is not installed), ``NonZeroExitCode`` (the tool failed),
``DesignValidationError`` (a bad design file), ``FlowFailed`` (the flow ran but reported failure),
``NoSuccessfulRun`` (a DSE search found no successful candidate), ``FlowFatalError``, and
``FlowException``. Four more come from run-directory and delivery isolation (D21):

* ``RunRootError`` -- the run root (``--run-root``/``XEDA_RUN_ROOT``) cannot be used: it holds
  files and carries no marker Xeda created, or it lies where Xeda cannot write. Names the
  directory and the fix (move it aside, or mark it yourself).
* ``RunDirectoryError`` -- a run directory itself cannot be used: it would lie outside its run
  root, or a write inside it would go through a symbolic link Xeda does not control.
* ``DeliveryError`` -- an output named by a setting or ``--outputs-to`` could not be delivered:
  the run did not write the file the setting expected, the destination is an input of the launch,
  a directory, or inside a run root, or it changed after it was checked.
* ``OutputExistsError`` -- a subclass of ``DeliveryError``: the destination holds a file that is
  not Xeda's own unchanged earlier delivery, and replacing it was not confirmed. Rerun with
  ``--overwrite-outputs``, or answer the prompt at an interactive terminal.

Exit status
===========

``0`` on success, non-zero on failure, for every command. ``xeda run`` fails when
``results.success`` is false; ``xeda dse`` fails when the exploration found no successful run.

Planning a run
==============

``xeda run <flow> <design> --dry-run --json`` prints the resolved plan without running anything.
For a design named ``blinky`` whose RTL top is ``blinky``, this command also demonstrates a
consumer switching its producer's optional output on:

.. code-block:: bash

    xeda run nextpnr blinky.yaml -s fpga.part=iCE40HX1K-TQ144 flows.yosys_fpga.netlist_json= --dry-run --json

.. code-block:: json

    {
      "flow": "nextpnr", "design": "blinky.yaml", "success": true, "dry_run": true,
      "plan": {
        "requested": "nextpnr",
        "nodes": [
          {"name": "yosys_fpga", "flow": "yosys_fpga", "declared": true,
           "run_path": "/path/to/xeda_run/blinky/yosys_fpga", "flowrun_hash": "...",
           "inputs": [], "switched_on": ["netlist"],
           "input_types": {}, "output_types": {"netlist": ["JsonNetlist"]}},
          {"name": "nextpnr", "flow": "nextpnr", "declared": true,
           "run_path": "/path/to/xeda_run/blinky/nextpnr", "flowrun_hash": "...",
           "inputs": [{"name": "netlist", "origin": "producer", "producer": "yosys_fpga",
                       "output": "netlist", "sources": []},
                      {"name": "constraints", "origin": "none", "producer": null,
                       "output": null, "sources": []},
                      {"name": "sdc", "origin": "none", "producer": null,
                       "output": null, "sources": []}],
           "switched_on": [],
           "input_types": {"netlist": ["JsonNetlist"], "constraints": ["Pcf"], "sdc": ["Sdc"]},
           "output_types": {"config": ["IceAsc"]}}
        ]
      }
    }

``nodes`` is ordered with producers before consumers and the requested flow last. ``run_path``
and ``flowrun_hash`` identify each planned run; ``--hashed-run-dirs`` adds the usual 16-character
hash suffix. Each input's ``origin`` is ``producer``, ``source`` or ``none``. ``producer`` and
``output`` name another node's output, or are ``null`` for source/absent inputs; ``sources`` is
an ordered list of source paths. ``switched_on`` names optional outputs enabled because a
consumer needs them, not every output already enabled by its settings. ``input_types`` and
``output_types`` report each declaration's effective types after target agreement; for example,
iCE40 selects ``Pcf`` pin constraints and ``IceAsc`` configuration. The static flow catalog
reports the full declared type vocabulary.

An undeclared flow appears as a node with ``declared: false`` and unknown runtime dependencies:
planning does not call its ``init()``. Freshness and always-run decisions are not evaluated.
Invalid settings, shared-setting conflicts, missing required inputs and impossible targets
produce the usual ``success: false`` / ``error`` document.

Planning changes no run roots, markers, locks, results or deliveries and probes no tools.
Loading that needs a generator or a Git dependency fetch is refused before those side effects;
materialize sources first, or pass an already materialized ``Design`` to ``DefaultRunner.plan``.
``--dry-run --remote`` is refused.

Using Xeda as a library
=======================

The same information is available in-process, without shelling out. ``xeda.introspect`` returns
plain JSON-serializable data:

.. code-block:: python

    from xeda.introspect import (
        flows_info, settings_info, results_info, design_schema,
        boards_info, platforms_info, optimizers_info,
    )

    print(flows_info())                     # every flow
    print(settings_info("vivado_synth"))    # one flow's settings
    print(results_info("vivado_synth"))     # one flow's result keys

To run a flow:

.. code-block:: python

    from xeda import Design, DefaultRunner

    design = Design.from_file("sqrt.yaml")
    runner = DefaultRunner("xeda_run")
    flow = runner.run("vivado_synth", design, flow_settings=["clock.period=5.0"])
    if flow and flow.results.success:
        print(flow.results.Fmax)

To inspect a plan without constructing or running flows:

.. code-block:: python

    from xeda.introspect import plan_info

    plan = runner.plan("nextpnr", design, flow_settings=["fpga.part=iCE40HX1K-TQ144"])
    print(plan_info(plan))

``plan_info`` returns the same plain data as ``--dry-run --json``'s ``plan`` key. Passing a
materialized ``Design`` avoids loading/generation; the plan's settings snapshots are private
copies and its captured request context is protected. Launchers validate their own internal
plans; externally supplied plans are not a supported library interface.

Notes for coding agents
=======================

* Prefer ``--json`` over parsing the tables: table output is wrapped to the terminal width.
* Discover, do not guess. ``xeda list-settings <flow> --json`` is the complete list of accepted
  settings; an unknown setting is a hard error, not a warning.
* Setting names are exact, but flow names are forgiving: dashes, underscores and the CamelCase
  class name all resolve, and a near-miss suggests alternatives.
* ``xeda run --json`` gives both the parsed results and the paths to the run directory, so there is
  no need to guess where output landed.
* A flow's ``dependencies`` run automatically; run the flow you want, not the chain leading to it.
  Reach a dependency's settings through a nested key, e.g. ``-s nextpnr.yosys.flatten=true``.
* ``xeda run`` is make-like by default: re-running with nothing changed re-runs nothing, and
  ``nodes`` says so per flow. Force everything to run with ``--rebuild-all``.
