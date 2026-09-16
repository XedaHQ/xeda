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

    xeda run vivado_synth sqrt.toml --json
    xeda dse vivado_synth --design sqrt.toml --json
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
      "settings_class": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim.Settings"
    }

``name`` is the canonical name; ``aliases`` are the other accepted names. ``dependencies`` are
detected statically, so treat them as a reliable hint rather than a guarantee - a flow may add
dependencies conditionally at run time.

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
          "common": true,
          "declared_by": "xeda.flow.flow.Flow.Settings",
          "json_schema": {"title": "Ncpus", "type": "integer"}
        }
      ],
      "definitions": {"...": "JSON Schema of the nested types"},
      "json_schema": {"...": "the complete JSON Schema of the settings"}
    }

* ``common`` marks the settings every flow accepts. ``--no-common`` omits them.
* ``alias`` is a second accepted name; both work.
* ``enum`` lists the permitted values when a setting is constrained to a set.
* ``definitions`` describes nested setting types (``FPGA``, ``PhysicalClock``, ``RunOptions``, ...).

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
against - or generate from - rather than guessing at the format.

Running a flow
==============

``xeda run <flow> <design> --json`` writes a single JSON object to stdout:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "design": "sqrt",
      "success": true,
      "results": {"success": true, "Fmax": 459.55, "lut": 4123, "...": "..."},
      "run_path": "/path/to/xeda_run/sqrt/vivado_synth",
      "results_json": "/path/to/xeda_run/sqrt/vivado_synth/results.json",
      "settings_json": "/path/to/xeda_run/sqrt/vivado_synth/settings.json"
    }

On failure the document carries an ``error`` object alongside any available results, and the exit
status is non-zero:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "design": "sqrt.toml",
      "success": false,
      "results": {},
      "error": {
        "type": "FlowSettingsError",
        "message": "FlowSettingsError: 1 error validating VivadoSynth.Settings\n   extra fields not permitted: no_such_setting (value_error.extra)"
      }
    }

``error.type`` names the exception class, which is stable enough to branch on:
``FlowSettingsError`` (a bad setting), ``FlowNotFoundError`` (a bad flow name),
``ExecutableNotFound`` (the tool is not installed), ``NonZeroExitCode`` (the tool failed),
``DesignValidationError`` (a bad design file), ``FlowFailed`` (the flow ran but reported failure),
``NoSuccessfulRun`` (a DSE search found no successful candidate), ``FlowFatalError``, and
``FlowException``.

Exit status
===========

``0`` on success, non-zero on failure, for every command. ``xeda run`` fails when
``results.success`` is false; ``xeda dse`` fails when the exploration found no successful run.

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

    design = Design.from_file("sqrt.toml")
    runner = DefaultRunner("xeda_run")
    flow = runner.run("vivado_synth", design, flow_settings=["clock_period=5.0"])
    if flow and flow.results.success:
        print(flow.results.Fmax)

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
