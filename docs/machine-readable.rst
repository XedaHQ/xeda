*******************************************
Machine-readable output
*******************************************

Everything that a script or a coding agent needs from Xeda is available as JSON. This page is the
reference for driving Xeda programmatically.

The rule
========

``--json`` always means that a parseable result goes to stdout.

Commands fall into two kinds, and each kind has its own mechanism.

Query commands print a document. They take ``--format {table,json,jsonl,yaml}``, which defaults to
``table``. ``design-schema`` takes ``--format {json,yaml}`` and defaults to ``json``. ``--json`` is
shorthand for ``--format json``:

.. code-block:: bash

    xeda list-flows --json
    xeda list-settings vivado_synth --format jsonl
    xeda list-results vivado_synth --json
    xeda design-schema
    xeda list-boards --json
    xeda list-platforms --json
    xeda pdk list --json
    xeda list-optimizers --json

``xeda pdk path <platform>`` prints the directory of a platform in the PDK store. With ``--json``, it
gives the files of each role too. If the PDK files are not installed, it fails and the error says
how to install them. ``pdk list`` and ``pdk path`` only look at the files: they start no process
and use no network.

``xeda pdk install <platform>`` downloads the PDK files that a platform pins. It is the only
``pdk`` command that uses the network and writes the PDK store. Every machine-readable format
(``--json``, ``--format json``, ``--format jsonl`` and ``--format yaml``) installs without asking,
as ``--yes`` does, because a program cannot answer a question. Tool output goes to stderr, and
stdout carries one document: ``{"success": true, "directory": ..., "receipt": ...,
"already_installed": ...}``, or ``{"success": false, "error": ...}`` if the install fails. A failed
``orfs`` or ``lambdapdk`` install leaves the store as it was, and it keeps no ``.git`` in it:
several platforms can share the directory of one commit. A failed ``ciel`` install may leave files
of ``ciel`` in the store, so a ``ciel`` store counts as installed for this command only when its
receipt exists. The install refuses a symbolic link inside the store, in ``<root>/xeda``, and fails
with ``"success": false`` before it writes. The root itself may be a link. The install writes only
real directories inside the store, so that it never writes or deletes outside it.

Executional commands do work and print the logs of the tools. They take a plain ``--json`` flag. It
moves tool output, log messages and result tables to stderr, so that stdout carries only the JSON
document:

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --json
    xeda dse vivado_synth examples/vhdl/sqrt/sqrt.yaml --json
    xeda scrub vivado_synth sqrt --json

.. note::
   The table renderings wrap to the terminal width, and long values wrap across lines. The JSON,
   JSONL and YAML renderings never wrap or truncate, so parse those.

Discovering what to run
=======================

``xeda list-flows --json`` returns a list with one object per flow:

.. code-block:: json

    {
      "name": "vivado_postsynth_sim",
      "class": "VivadoPostsynthSim",
      "module": "xeda.flows.vivado.vivado_postsynthsim",
      "qualified_name": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim",
      "description": "Simulate the testbench on the routed netlist `vivado_synth` writes. ...",
      "category": "simulation",
      "supports_cocotb": false,
      "dependencies": ["vivado_synth"],
      "action_reason": null,
      "inputs": ["..."], "outputs": ["..."],
      "can_follow": [], "can_precede": ["..."],
      "settings_class": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim.Settings"
    }

``name`` is the name of the flow, the only name that Xeda accepts for it. ``dependencies`` are
the default producers of the declared inputs of the flow. A design source or a binding can replace a
default producer, so use the resolved plan to see the producers that a request needs.

``inputs`` and ``outputs`` list the declared files of the flow. Each entry has a name, a ``value``,
the accepted source ``types``, a ``cardinality`` (``one``, ``optional`` or ``many``) and a
description. The ``value`` is ``files`` for a declared file. It is ``design`` for the design itself
and then ``types`` is empty: a flow that reads design sources has the input ``design``, and a
converter flow such as ``bsc`` has the output ``design``. An input of the kind ``design`` takes an
output of that kind and nothing else. An input also says:

- whether it is ``required`` (the flow cannot run without it);
- whether it is ``optional`` (an optional list, which may be empty);
- whether it compares ``equivalence`` digests, which only ``trace.json`` holds (see
  ``record.outputs`` below);
- which ``producer`` and ``output`` are its default.

An output names the setting that enables it in ``enabled_by``.

An input can have to come from the same producer as another input of the flow. It then names that
input in ``same_producer_as``. With ``via``, the relation goes one step further: the input comes
from the producer of an input of the flow that makes the ``same_producer_as`` input. For example,
the ``checkpoint`` of ``vivado_power`` comes from the synthesis whose ``netlist_timing`` the
simulation behind ``activity`` read. Both keys are ``null`` for most inputs. Only generated inputs,
those with a producer in the plan, are compared. An input that comes from a source you list, or that
is absent, is not. With ``paired`` true, the two inputs also cannot mix the two origins: both come
from sources you list, or both from one producer.

For example, ``nextpnr`` declares the input ``netlist`` of type ``JsonNetlist``, with the default
producer ``yosys_fpga`` and its output ``netlist``. Sources of an accepted type displace that
default producer.

``can_precede`` lists the flows that can come after this flow in a chain (``xeda run this+other``),
and ``can_follow`` lists the flows that this flow can come after. The direct relations come first.
An entry whose ``through`` is not empty is a link that passes through the flows between the two,
which Xeda plans as it does for the second flow alone. The validator that ``xeda run`` applies
judges both, on declarations alone, so a pair that the validator refuses is never listed. Planning
checks one more thing that declarations cannot show: related inputs of a flow must come from one
producer. A listed pair can therefore still be refused there, with the bindings to add. For example,
``vivado_alt_synth`` is listed before ``vivado_power``, but the ``activity`` input of
``vivado_power`` still comes from ``vivado_synth`` unless you bind the simulation too.

Each entry of these two lists has these keys:

- ``flow`` is the other flow.
- ``output`` is the producer output that the request must name. It is ``null`` when the unqualified
  request is valid.
- ``through`` lists the flows that the link passes through, in chain order. It is ``[]`` for a
  direct link. Every entry has this key, as every element of ``request`` does.
- ``binds`` lists the ``flow``, ``input`` and ``output`` of each binding that the link makes. The
  ``flow`` is the flow whose input the link binds. It is the second flow for a direct link. For a
  link that passes through flows, it can be one of those flows, such as ``nextpnr`` for ``netlist``
  in ``yosys_fpga+fpga_pack``.
- ``target_dependent`` is true when a produced or accepted kind is a union that the target selects
  from, so the resolved plan is authoritative. For example, ``nextpnr`` makes the configuration of
  the family of the target.

A flow that declares no inputs or outputs has no relations. Every flow that reads design sources
declares the ``design`` input, which fits only the design output of a converter, such as
``bsc``. ``action_reason`` is static and ``null`` for most flows. It says why a flow, such as a
programmer, can only end a chain.

.. _machine-readable-settings:

Discovering what to set
=======================

``xeda list-settings <flow> --json`` returns the full settings of the flow. Each setting is listed
under the name that you can type as ``-s <name>=<value>``:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "settings_class": "xeda.flows.vivado.vivado_synth.VivadoSynth.Settings",
      "fields": [
        {
          "name": "nthreads",
          "type": "integer",
          "required": false,
          "default": null,
          "description": "Maximum number of threads.",
          "enum": null,
          "writes": null,
          "opaque_keys": false,
          "common": true,
          "declared_by": "xeda.flow.flow.Flow.Settings",
          "tier": "common",
          "group": null,
          "json_schema": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "default": null,
            "description": "Maximum number of threads.",
            "title": "Ncpus"
          }
        }
      ],
      "definitions": {"...": "JSON Schema of the nested types"},
      "json_schema": {"...": "the complete JSON Schema of the settings"}
    }

* ``common`` marks the settings that every flow accepts. ``--no-common`` leaves them out.
  The tier ``"common"`` (see ``tier``) is a different thing: it says how often a user needs
  the setting.
* ``writes`` says whether the flow writes to the path that the setting names. The value
  ``"working"`` is an intermediate location, which the flow always uses as a bare name inside the
  run directory, whatever the value is (``sim_dir`` or a log path). The value ``"deliverable"`` is
  an output that may be given a location, and Xeda then delivers it there once the run finishes
  (``bitstream`` of ``fpga_pack``, ``vcd``). The value is ``null`` when the flow reads the setting
  and does not write it.
* ``opaque_keys`` is true for a setting that holds a dictionary whose keys are names that the tool
  defines or that a netlist gives, such as the run properties of Vivado (``set_synth_properties``)
  or the powers of instances (``ir_drop.instance_power``). A dot in such a key is part of the
  name. For example, ``-s set_synth_properties.STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM=0`` sets the one
  property ``STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM``. For any other setting, a dotted key is a path into
  nested tables.
* ``enum`` lists the permitted values when the setting is limited to a set.
* ``tier`` is ``"common"``, ``"advanced"`` or ``"expert"``: how often a user needs the setting. It
  is for listings and documentation only, and it never changes what Xeda accepts. A setting that
  declares no tier is ``"common"``. The key ``common`` is not the tier: see the bullet above.
* ``group`` is the name of the top-level setting that holds a settings group, for each entry of a
  group and of its sub-groups. It is ``null`` for any other entry, and for the entry of the
  top-level group setting itself.
* ``definitions`` describes the nested setting types (``FPGA``, ``PhysicalClock``, ``RunOptions``
  and others). This key belongs to Xeda's own envelope, and inside ``json_schema`` the same types
  appear under the ``$defs`` key of JSON Schema.
* ``type`` is a short rendering for people and agents, and it is not JSON Schema. An optional
  setting reads as its underlying type (``integer``), while ``json_schema`` carries the literal
  ``anyOf: [..., {"type": "null"}]`` that pydantic emits. A quantity reads as its kind (``time``,
  ``frequency``, ``capacitance``, ``length``, ``voltage``, ``resistance`` or ``power``; see :ref:`quantities`).
  In ``json_schema``, a quantity is ``"type": "string"`` with the kind in the key
  ``x-xeda-quantity``, and the unit that the field is written in in the key ``x-xeda-unit``. The
  schema separates a number from text, and it cannot check the unit in the text.
* ``unit`` is the unit that a quantity setting is written in (``s``), or ``null``. Xeda writes
  the value and the default of the setting in this unit (see :ref:`quantities`). A setting that
  holds quantities in one unit, such as a list of times, has that unit. A setting that holds a
  group or a table of models has ``null``: the fields of the model have their own units.

``fields`` has one entry for each top-level setting. After the entry of a settings group, it has
one entry for each field of the group, depth first in the order of declaration. A sub-group has an
entry of its own, then the entries of its fields. The ``name`` of such an entry is the dotted path
that ``-s`` and YAML accept, such as ``<group>.<subgroup>.<setting>`` (see :ref:`settings groups
<settings-groups>`). ``--tier {common,advanced,expert}`` keeps the entries up to that tier, in
every format. The default keeps all of them. The settings that every flow accepts have the tier
``"common"``, so ``--tier`` never removes them.

``--format jsonl`` prints one field per line. Each line carries its ``flow``, which is convenient
for streaming or grepping.

Discovering what comes back
===========================

``xeda list-results <flow> --json`` lists the metrics that the flow writes to ``results.json`` and
its artifacts (see :ref:`artifact-keys`):

.. code-block:: json

    {
      "flow": "vivado_synth",
      "metrics": [
        {"path": "fmax", "description": "Maximum achievable clock frequency, ...",
         "type": "number", "unit": "MHz"},
        {"path": "lut", "description": "Number of LUTs used.", "type": "integer", "unit": null}
      ],
      "artifacts": [
        {"key": "log", "description": "Output and error output of the main tool of the flow, ...",
         "when": "when the flow starts its main tool"}
      ],
      "note": "..."
    }

``type`` is one of ``number``, ``integer``, ``boolean``, ``text``, ``text_list`` (a list of text),
``counts`` (a table of text to integer), ``numbers`` (a table of text to number) and ``object`` (a
JSON object that the description documents). ``unit`` is a unit text such as ``MHz``, ``ns`` or
``W``. For ``numbers``, it is the unit of every value. A metric in the units of the platform, such
as a cell area in the area unit of the Liberty files, has the unit ``null``, and its description
says so. The keys that every flow reports (``runtime``, ...) are no metrics and are not listed. The
table of ``xeda list-results`` has the columns Path, Type, Unit and Description.

A temperature is in ``degC``. A temperature difference is in ``delta_degC``, and 5 ``delta_degC`` is
5 K. A thermal resistance is in ``K/W``. The simulated time ``sim.time`` is an integer number of
femtoseconds. The counts of cells by type are one ``counts`` metric, ``cell_types``, with the name
of the cell as the key.

A metric names a quantity, not one way to measure it. ``lut`` is reported per toolchain and per
stage. The open-source FPGA flows add ``lut_basis.stage`` (``mapped`` for ``yosys_fpga``, ``placed
and routed`` for ``nextpnr``) and ``lut_basis.method`` beside it. Counts from different toolchains
or stages are not certified comparable, and this holds in particular for Vivado's utilization
report. ``fmax`` works the same way. Each tool times a design with its own model, so compare
``fmax`` only between runs of the same tool (see :ref:`flow-results`).

.. _artifact-keys:

Artifact keys
-------------

``xeda list-results <flow> --json`` lists the artifacts of a flow in ``artifacts``, each as
``{key, description, when}``. ``when`` is the condition under which a run has the key. The table of
``xeda list-results`` shows them under the metrics. A key is lower snake case.

- ``log`` is the output of the main tool of the flow, in ``logs/<program>.log``. Every flow that
  starts a tool has it. For ``nextpnr`` and ``fpga_pack`` the main tool is the program of the
  device family, and for ``dc`` it is the program that ``dc_shell_name`` names.
- ``<name>_log`` is the log of another program that the flow starts, such as ``klayout_log`` for
  ``openroad`` or ``g_log`` for the compiler ``g++``. The name is the program name in lower snake
  case, with the prefix ``tool_`` when it does not start with a letter (``tool_7z_log``).
- ``synth_1_log`` and ``impl_1_log`` are the logs of the two runs that ``vivado_synth`` starts in
  Vivado (``<design>.runs/<run>/runme.log``). The error of a failed run is in its log, so
  ``error.log`` names it, and not ``log``.
- ``simulation_log`` is the output of the simulation program that the run built (``bsc_sim``,
  ``verilator``, ``vcs``, ``yosys_sim``).
- ``waveform`` is a waveform that the simulation wrote.
- A report has the lower snake case of its step and its file name as key, with the extension. The
  Vivado flows keep every report they write: ``route_design_timing_summary_rpt`` is the file
  ``reports/route_design/timing_summary.rpt``, and ``place_design_utilization_xml`` is
  ``reports/place_design/utilization.xml``.

A tool can keep a log of its own, such as the file that ``--log`` gives to nextpnr. That file is a
working file and no artifact: ``log`` already holds the output of the tool.

``error.log`` names the key of the log to read after a failure. For a program that exited with a
nonzero status it is the key of that program, so a failure of KLayout points at ``klayout_log``.
After a Vivado run that did not complete its step, it is the log of that run (``synth_1_log`` or
``impl_1_log``). In every other case it is ``log``, if the run wrote one, and ``null`` if not.

.. _results-json:

The record of a run
-------------------

Each run leaves one record, ``results.json``, in the run directory. It is one JSON object with
the integer ``schema_version`` 1, and every number in it is finite. A reader of a newer schema
version, or of a file without ``schema_version`` (an older Xeda wrote it), does not guess: it
refuses the file. A launch treats such a file as no run at all and runs the flow again.

The record has these fields:

``run_id``
    The id of this execution. It is new for each execution, a failed one included. The trace of the
    run and every consumer of its outputs name the same id.
``xeda_version``, ``flow``, ``design``, ``target``
    The version of Xeda that ran, the name of the flow, the name of the design, and the target (or
    ``null``).
``identity``
    The identity of the run: the hash of its settings, of its design configuration (see
    ``configuration`` below) and of the origins of its inputs.
``configuration``
    What the flow read of the design: for each part it reads (``rtl``, ``tb``), the ``top``, the
    ``parameters`` and the ``defines``, the ``clocks`` and ``attributes`` (``rtl``) or the ``uut``
    and ``cocotb`` (``tb``), and ``language`` for ``rtl``. A flow that reads the sources of the
    design also has ``sources`` for each part, a list of ``{path, type}`` with paths relative to the
    design root (``../`` outside it). A flow that reads a design that a converter made has no
    ``sources``. It has ``converted_from`` (the name of the converter), and the sorted
    ``transforms`` and ``produces`` types of the conversion. The configuration is part of the
    identity of the run.
``outcome``
    ``passed``, ``rejected``, ``failed`` or ``canceled``. It replaces the ``success`` flag of older
    files. Only ``passed`` and ``rejected`` runs are reused.
``error``
    ``null`` for a passed run. Otherwise ``{code, message, node, exit_status, log}``, as
    :ref:`described below <error-codes>`.
``metrics``, ``metric_info``
    The metrics that the flow describes (``xeda list-results``), nested by their dotted path:
    ``sim.ended_by`` is the key ``ended_by`` of the object ``sim``. ``metric_info`` maps each such
    path to its ``description``, ``type`` and ``unit``. A value that is unknown is absent. A
    value that is not finite, or not of its declared type, is no metric: it fails the node, and
    the other metrics stay. The error code is ``invalid_metric``, unless the run already has an
    error, which stays the cause: a rejected run with such a value is ``failed`` with the code
    ``rejected``.
``outputs``
    Every declared output of the flow, by name, as ``{kind, cardinality, names, digests, design}``.
    ``kind`` is the name of the source type, or ``design``. ``cardinality`` is ``one``,
    ``optional`` or ``many``. ``names`` are the files the run wrote, relative to the run
    directory, and ``digests`` are their content digests, in the same order. Both are ``[]`` when
    the run wrote none. ``design`` holds the derived design of a converter.
``artifacts``
    The files that the run wrote for a reader, by key, as names relative to the run directory. A
    key names one file, and the flow documents it (see :ref:`artifact-keys`). A key is present only
    when the run wrote its file. A file outside the run directory is not listed. A declared output
    is never an artifact.
``settings``
    The effective settings of the node, as ``settings.json`` writes them.
``inputs``
    One entry for each declared input: ``name``, ``origin`` (``source``,
    ``producer`` or ``none``), ``producer``, ``output``, ``producer_run_id``, ``producer_run_dir``
    (relative to the run root), ``generation`` and the ``sources`` that the input read, each as
    ``{path, generated, digest, dependency}``. An input of cardinality ``many`` that is bound to
    several producer outputs has one entry for each binding. These entries have the same ``name``,
    in the order of the bindings. Each entry has its own ``producer``, ``output``, ``producer_run_id`` and
    ``producer_run_dir``. A binding that repeats gives an entry that repeats. A reader groups the
    entries by ``name``.
``started``, ``finished``, ``runtime``
    Two times in UTC (ISO 8601) and the duration in seconds.
``dependencies``
    The design's resolved dependencies, in order. Each entry has ``repository`` (the Git repository
    URL without a user name or password, or ``null`` for a local dependency), ``reference`` (the
    branch or tag as written, or ``null`` for a pinned commit, ``HEAD`` or a local dependency),
    ``commit`` (the resolved Git commit, or ``null`` for a local dependency), and ``design_file``
    (the dependency's design-file path).
``inherited_variables``
    A list of inherited variables; empty for now.

The record names no file of the run by an absolute path. The run root and the run directory
follow from where the file is. The old keys ``success``, ``run_path``, ``design_hash``, ``flow_hash``, ``settings_hash``,
``timestamp``, ``tools`` and ``design`` as a result key are gone. In Python,
``xeda.records.read_record(run_dir, run_root=...)`` returns the record as an immutable
``RunRecord``, and ``RunRecord.to_dict()`` returns the document.


Validating a design file
========================

``xeda design-schema`` prints the JSON Schema of a design file. Validate against it, or generate
from it, instead of guessing the format. The schema is a draft 2020-12 document: nested types live
under ``$defs`` and fixed-length tuples use ``prefixItems``. The ``$schema`` key always names the
dialect that the document actually conforms to, so point a validator at that dialect and do not
assume a draft. The schema describes the forms that the loader accepts. For example, ``sources`` and
``clocks`` take one item in place of a list of one. It does not describe the settings of a flow:
every ``flows.<flow>`` is an open table. Take the schema of those settings from
``xeda list-settings <flow> --json`` (``json_schema``).

Running a flow
==============

``xeda run <flow> <design> --json`` writes one JSON object to stdout. It has exactly these keys:

``schema_version``
    The integer 1.
``outcome``
    How the launch ended: ``passed``, ``rejected``, ``failed``, ``canceled`` or ``detached``. It is
    ``null`` for a request that started no launch (see below).
``error``
    ``null`` when the launch passed. Otherwise the cause of the outcome, as ``{code, message,
    node, exit_status, log}`` (see :ref:`error-codes`).
``launch_id``
    The id of the launch, or ``null`` for a request that started no launch.
``spec``
    What you asked for: ``{flow, design, design_file, target, settings}``. ``flow`` is the text of
    the request, such as ``yosys_fpga+nextpnr``. ``design`` is the name of the design. ``design_file``
    is the absolute path of the design file that you named, or ``null`` when you named none (a design
    of a project). ``target`` is the name given with ``--target`` (see :ref:`targets`), or ``null``.
    ``settings`` is what ``-s`` gave, as a table of settings by flow name.
``roots``
    The nodes that you asked for. For ``xeda run`` this is the last flow of the request.
``nodes``
    One entry for every node of the plan, in plan order, producers first.

In this example, ``nextpnr`` has the producer ``yosys_fpga``, whose recorded run was still valid.
The ``textcfg`` setting (the routed ECP5 design, as a Trellis text configuration) was given a
location, so Xeda delivered the file there once the run finished:

.. code-block:: json

    {
      "schema_version": 1,
      "outcome": "passed",
      "error": null,
      "launch_id": "5d1f0c1b0d3e4f36a2f5a0c5f4f7a9c1",
      "spec": {
        "flow": "nextpnr",
        "design": "blinky",
        "design_file": "/path/to/blinky.yaml",
        "target": null,
        "settings": {"nextpnr": {"seed": "3"}}
      },
      "roots": ["nextpnr"],
      "nodes": [
        {"name": "yosys_fpga", "flow": "yosys_fpga", "outcome": "passed",
         "disposition": "reused", "reason": "up to date", "blocked_by": null, "elapsed": 0.0,
         "run_dir": "/path/to/xeda_run/blinky/yosys_fpga", "deliveries": [],
         "record": {"schema_version": 1, "outcome": "passed", "...": "..."}, "error": null},
        {"name": "nextpnr", "flow": "nextpnr", "outcome": "passed", "disposition": "ran",
         "reason": "settings changed: seed", "blocked_by": null, "elapsed": 4.2,
         "run_dir": "/path/to/xeda_run/blinky/nextpnr",
         "deliveries": [
           {"key": "textcfg", "destination": "/home/user/blinky_config.txt", "delivered": true}
         ],
         "record": {"schema_version": 1, "outcome": "passed",
                    "metrics": {"fmax": 212.4, "lut": 45, "...": "..."}, "...": "..."},
         "error": null}
      ]
    }

The exit status follows ``outcome``: 0 for ``passed``, 1 for every other outcome, and 2 for a request
that started no launch. A Ctrl-C before the launch started also has no outcome, and its exit status
is 1.

``xeda run`` also takes a chain, such as ``xeda run yosys_fpga+nextpnr+fpga_pack design.yaml``. The
last flow of a chain is the requested flow: it is the only entry of ``roots``, and the outcome is
that of the launch for that flow. Each earlier flow supplies the compatible required inputs of the
next flow or, when the link passes through flows, the inputs that it feeds in the plan of the next
flow. ``nodes`` lists the producers of the request too, also the ones that the request did not name.
``xeda dse`` refuses a chain. ``xeda run --remote`` takes one.

A ``nodes`` entry has one key for each field of the ``NodeResult`` of the Python API, with the same
name and meaning:

``name``, ``flow``
    The name of the node in the plan and the name of its flow.
``outcome``
    How the node ended:

    - ``passed``: the flow ran, or Xeda reused its recorded run, and the checks passed.
    - ``rejected``: the checks of the flow rejected the results. The metrics stay in the record.
    - ``failed``: the flow raised an error, its tool failed, or a report or an output was missing.
    - ``canceled``: the node was running when the launch was canceled.
    - ``blocked``: a producer of the node did not pass, so the node did not run. ``blocked_by`` names
      that producer.
    - ``skipped``: the launch stopped before the turn of the node.
    - ``detached``: a remote launch that keeps running on the host had not ended the node.
``disposition``
    What this launch did with the node: ``ran``, ``reused`` (the recorded run was up to date, and Xeda
    did not run it again; only a ``passed`` or ``rejected`` run is reused), ``shared`` (a sweep
    executed it once for several points) or ``not_run``.
``reason``
    Why the node ran, was reused, was blocked or was not reached. For a node that ran, this is
    the text that the log shows as ``Running <flow>: <reason>``. For a rejected run that Xeda
    reported again, it is the message of the rejection.
``blocked_by``
    The name of the producer whose outcome blocked the node, or ``null``.
``elapsed``
    The seconds that this launch spent on the node. It is ``0.0`` unless the node ran.
``run_dir``
    The run directory of the node.
``deliveries``
    Every output that the node copied to a location that the settings, ``--outputs-to`` or the
    ``outputs_to`` of the design's generator named, once the whole launch finished (see "Outputs where you name them" in :doc:`run-directories`).
    Each entry is ``{key, destination, delivered}``. ``key`` is the setting (or ``--outputs-to``,
    or ``generator`` for a copy of a generated source) that asked for the delivery. ``delivered`` is false when Xeda refused the delivery or it failed,
    when the launch did not pass, and for an artifact of ``--outputs-to`` that a symbolic link leads
    out of the run directory, which Xeda skips (see :doc:`run-directories`). The list is ``[]`` for
    a node with nothing to deliver. A node
    that is up to date still lists its deliveries, because delivery depends on the outcome of the
    node, whether or not its tool ran.
``record``
    The ``results.json`` of the run, as :ref:`described above <results-json>`, or ``null`` for a node
    that did not run (``blocked``, ``skipped`` or ``detached``) or whose record could not be stored.
    The metrics of a run are in ``record.metrics``, for example ``.nodes[-1].record.metrics.fmax``.
``error``
    Why the node did not pass, or ``null`` for a node that passed or was skipped. It has the keys of
    the ``error`` of the document. For a node that ran, it is the error of its ``record``. A
    ``blocked`` node has the code ``producer_failed``. A node that failed before its run started has
    no ``record``, so this is the only place that names the cause.

The ``outcome`` of the document is the outcome of the cause of the launch, in this order:

1. A node that is ``detached`` makes the outcome ``detached``.
2. A node that is ``canceled``, or the launch error ``canceled``, makes it ``canceled``. Ctrl-C
   cancels a launch: the document still lists every node, a node that ended keeps its outcome,
   and the node that was running is ``canceled``.
3. A launch-level error (a failed delivery, a failed fetch of a remote launch) makes it ``failed``.
4. Otherwise each root has a cause: the root itself, or, for a blocked root, the producer that failed
   or was rejected. The outcome is the worst of the causes, in the order ``failed``, ``rejected``,
   ``skipped``, ``passed``.

``error`` is the error of that cause. It has ``node: null`` for a launch-level error. When a
producer fails, the outcome is ``failed``, ``error`` is the error of the producer, and each consumer
that the failure stopped is ``blocked``. The ``record`` of the producer holds the same error.

Runs are make-like by default (without ``--rebuild-all``), so a launch where everything is up to
date has ``"outcome": "passed"`` and every node ``"reused"``. See :doc:`run-directories` for what makes
a node stale. A flow that always runs is never ``"reused"``. Its ``reason`` says why.
``openfpgaloader``, which programs a device, is one such flow, and so is a flow that is asked for a
fresh random seed. A node that failed is not reused: the next launch runs it again.

The declared outputs of a run are in ``record.outputs``. Each declared output maps its name to
``{kind, cardinality, names, digests, design}``. ``names`` are the files of the output, relative to
the run directory, and ``digests`` are their content digests in the same order. The equivalence
digests that a consumer compares are in ``trace.json`` only.

Xeda checks the output records at two points:

- When it records an output, Xeda checks that the files are readable, that they lie in the run
  directory and that this run wrote them.
- When it hands an output over, Xeda checks the schema of the record, the cardinality, the
  containment and the content, under a verified read lease on the producer. This is true whether the
  producer ran or was reused.

A failure to record an output uses ``missing_output`` and the normal failure identity. A failed
hand-over check of a consumer is a ``producer_failed``. A flow can also fail earlier with its
own check. For example, ``nextpnr`` fails with ``reported_failure`` and names the configuration setting
and the path of an enabled configuration that its tool did not write.

A request that starts no launch
-------------------------------

Xeda refuses an invalid request before it runs any node: a bad setting, a bad design file or
project file, an unknown design, a malformed chain, a refused delivery, or a mistake in the command
line. The document then has ``"outcome": null``, ``"launch_id": null``, ``"roots": []`` and
``"nodes": []``. Its ``error`` says why, and the exit status is 2:

.. code-block:: json

    {
      "schema_version": 1,
      "outcome": null,
      "error": {
        "code": "unknown_setting",
        "message": "sed: Extra inputs are not permitted",
        "node": "nextpnr",
        "exit_status": null,
        "log": null
      },
      "launch_id": null,
      "spec": {
        "flow": "nextpnr",
        "design": "blinky",
        "design_file": "/path/to/blinky.yaml",
        "target": null,
        "settings": {"nextpnr": {"sed": "1"}}
      },
      "roots": [],
      "nodes": []
    }

An error in the command line itself has ``spec: null``, because Xeda could not read the command
line as a request. A flow that does not exist has the code ``flow_not_found``, a malformed chain has
its chain code (``invalid_chain``, ``repeated_flow``, ``no_edge`` and others), and a design file or
project file that does not exist has ``design_file_not_found`` or ``project_file_not_found``. Any
other mistake of the command line has the code ``usage_error``: an unknown option, no design, and
the options that need each other. For example, ``--remote`` refuses ``--post-cleanup``, and
``--keep-running`` and ``--fetch-all`` need ``--remote``. Click prints the same exit status 2 without
``--json``.

A Ctrl-C while Xeda loads the design or plans the request, before the launch starts, also gives a
document with ``"outcome": null`` and no nodes. Its error code is ``canceled``, and the exit status
is 1.

A binding that cannot be applied (an unknown input or producer, a binding for a flow that declares
no inputs, or a chain together with a command-line or API binding of the same input) is a settings
error. It names the input and the place where the binding was written.

.. _error-codes:

Error codes
-----------

An ``error`` is ``{code, message, node, exit_status, log}``. The ``code`` is lower snake case, never
the name of a class, and stable enough to branch on. The set of codes is open: a later release can
add codes, so keep an unknown code as text. ``message`` is the text for a person, without the name of a class in front of it (the log line has
the name). ``node`` names the
node that the error belongs to, or is ``null`` for an error of the whole launch. ``exit_status`` is
the exit status of the tool for ``nonzero_exit``. ``log`` is the key of the artifact to read for the cause (see :ref:`artifact-keys`), or ``null``.

The code of an error of a node, in its ``record.error`` and in the top-level ``error`` when it is
the cause:

``nonzero_exit``, ``timeout``, ``executable_not_found``
    The tool failed, took too long, or is not installed.
``reported_failure``
    The flow raised an error of its own, or its reports or checks did not pass although the tool
    exited with status 0. A flow can give a message of its own. For example, ``openfpgaloader`` says
    what its loader reported and quotes the lines.
``missing_report``
    A report that the flow asked for does not exist, or is a previous run's, not this run's. The
    message names the files.
``missing_output``
    The flow passed its own checks, but an enabled declared output is absent, unreadable, outside
    the run directory or not written by this run.
``missing_generated``
    A source that the generator of the design should write, and that the flow reads, matches no file
    in the output of the generator. The message names the entry of ``rtl.sources``.
``invalid_metric``
    A metric is not finite or not of its declared type. The other metrics stay.
``rejected``
    The checks of the flow judged the results negative after its reports were read.
``producer_failed``
    A producer failed, or Xeda could not verify its completed output. The message quotes the
    producer, its code and its message.
``canceled``
    The node was running when the launch was canceled.
``storage_error``
    A file or directory error of the operating system, or a path that leaves the run directory,
    ended the node. The record may be missing.
``internal_error``
    A bug of Xeda.

The code of an error of the whole launch (``node`` is ``null``): ``generator_failed``,
``delivery_failed`` (Xeda could not deliver an output that a setting, ``--outputs-to`` or the
``outputs_to`` of a generator named), ``cleanup_failed`` (see below), ``canceled``, and, for
``--remote``, ``remote_failed``, ``remote_fetch_failed`` and
``remote_detached``. ``no_successful_run`` is the error of ``xeda dse``.

The code of an error of a request that started no launch is the code of the setting, design,
project, chain or run root problem. For a bad setting: ``unknown_setting``, ``required_setting``,
``invalid_quantity``, ``invalid_value``, ``boolean_text``, ``list_text``, ``conflicting_keys``,
``removed_setting``, ``removed_alias`` (another spelling of a name that stays), ``unsupported_setting`` and others. For a chain: ``invalid_chain``,
``repeated_flow``, ``no_edge``, ``unknown_output`` and others. Others are ``invalid_design``,
``design_file_not_found``, ``unknown_target``, ``target_required``, ``unknown_design``,
``design_name_required``, ``use_design_name``, ``several_project_files``, ``invalid_project``,
``invalid_local_settings`` (a private settings file is invalid; the message names the file),
``removed_alias`` and ``removed_key`` (a design or project key that Xeda refuses; the message names the key to write),
``fetch_failed`` (planning could not resolve or fetch a Git dependency; ``field`` names it),
``dependency_cycle`` (the dependencies of a design lead back to a design on the same chain; the
message names the chain),
``flow_not_found``, ``invalid_run_root``, ``delivery_refused`` and ``usage_error``.

``generator_failed`` says that the generator of the design failed or could not be started or
judged. It comes before any node runs, so every node is ``skipped``, and Xeda leaves no output of
the generator behind. With ``delivery_failed`` and ``cleanup_failed``, every node keeps the outcome
it had.
``cleanup_failed`` says that every requested flow passed, and then the clean-up after the run
(``--post-cleanup`` or ``--post-cleanup-purge``) failed. The message names the run directory and the
cause. A launch that has another cause already keeps it: a failed delivery comes before a failed
clean-up, and a requested flow that did not pass keeps its own error. Xeda only logs the clean-up
failure then.

Other documents of executional commands
---------------------------------------

The document of ``xeda run --remote ... --json`` is the document of a run, as above. The
``run_dir`` of each node, and the paths in its record, name directories and files in the mirror, the
directory ``<run root>/@<host>`` that holds the fetched files. ``design_file`` is your local design
file, and ``launch_id`` is the id of the launch on the host. A failure of the remote step itself
(a lost connection or an error while it makes the environment) gives the document of a launch with
the outcome ``failed`` and the error code ``remote_failed``. If the launch ended and Xeda could not
fetch or deliver its result, the code is ``remote_fetch_failed`` or ``delivery_failed``, and the
nodes keep their records. Ctrl-C gives the outcome ``canceled`` and the code ``canceled``.

The document of a launch that Xeda stopped following (``--keep-running``, then Ctrl-C or a lost
connection) has the outcome ``detached``, the error code ``remote_detached`` and the id of the launch
in ``launch_id``. Every node is ``detached``, and the launch keeps running on the host.

The document of a remote dry run is the document of a local dry run with these keys: ``remote``,
``mirror``, and ``uploaded``, the list of input files that the dry run sent to the host. Each node
of ``plan`` has the ``run_path`` that the run would use in the mirror, and ``remote_run_path``, the
directory on the host.

``xeda remote attach HOST [LAUNCH_ID] --json`` prints the document of the finished run. The other
commands of ``xeda remote`` print one document each. A failure is ``{"success": false, "error": {"type": ..., "message": ...}}`` with exit status
1.

``check``
    ``success``, ``host``, ``platform`` (``system``, ``machine``, ``libc``, ``libc_version``),
    ``xeda`` (the version), ``python`` (``version``, ``implementation``, ``executable``),
    ``environment`` (``key``, ``code``, ``prefix``, ``lock_file``, ``lock``), ``disk`` (``path``,
    ``free`` and ``total`` in bytes), ``kill_user_processes`` (``yes``, ``no`` or ``unknown``),
    ``executables`` (``found``, a table of name and path, and ``missing``, a list), ``docker``
    (``found`` and ``path``) and ``warnings``.

``clean``
    ``success``, ``host``, ``removed`` and ``kept``. Each entry has ``kind`` (``env``, ``code`` or
    ``workspace``) and ``path``, and an entry of ``kept`` has a ``reason``. ``--json`` removes
    without asking.

``cancel``
    ``success``, ``host``, ``launch`` and ``survivors``, the number of processes that SIGKILL did
    not stop. ``success`` is false when ``survivors`` is not zero.

``xeda dse ... --json`` prints ``flow``, ``design``, ``design_file``, ``target``, ``optimizer``,
``success`` and ``best``. The value of ``best`` is ``null`` or an object with the ``settings``,
``results``, ``timestamp`` and ``run_path`` of the best run. When the exploration found no
successful run, the document has an ``error`` of type ``NoSuccessfulRun``, and the exit status is 1.

Removing run directories
========================

``xeda scrub <flow> <design> --json`` removes the previous run directories of a flow for one design
(see :doc:`run-directories`) and writes a single JSON object to stdout. The listing, the
confirmation prompt, the lines about the directories that scrub kept or found gone, and the links it
skipped go to stderr, with the rest of its output:

.. code-block:: json

    {
      "success": true,
      "flow": "vivado_synth",
      "design": "sqrt",
      "target": null,
      "run_root": "/path/to/xeda_run",
      "scanned": ["/path/to/xeda_run/sqrt"],
      "scrubbed": ["/path/to/xeda_run/sqrt/vivado_synth_0123456789abcdef"],
      "kept": ["/path/to/xeda_run/sqrt/vivado_synth_fedcba9876543210"],
      "gone": [],
      "skipped": []
    }

``target`` is the ``--target`` that you gave, or ``null``. The five lists hold paths:

* ``scanned`` holds the directories that scrub searched for run directories of the flow.
* ``scrubbed`` holds the run directories that scrub removed. The list is empty if scrub found none,
  or if you did not confirm.
* ``kept`` holds the run directories that scrub listed and did not remove, because their run records
  (``results.json`` and ``trace.json``) changed after the listing. A run finished there, or a launch
  found its run up to date and refreshed its trace. What is there is no longer what you confirmed.
* ``gone`` holds the run directories that scrub listed and that were not there any more when its
  turn came, because another scrub or a purge removed them first. This is what scrub was asked to
  do, so it is not an error.
* ``skipped`` holds the links that are named like a run directory of the flow and that scrub did not
  list, because they do not lead to a run directory of the flow beside them (see
  :doc:`run-directories`). Scrub removed neither the link nor what it leads to. It says why on
  stderr, with one line for each link.

No path is in more than one of ``scrubbed``, ``kept``, ``gone`` and ``skipped``. A failure writes
``{"success": false, "flow": ..., "design": ..., "target": ..., "error": {"type": ..., "message":
...}}`` and exits with status 1. The ``error.type`` is ``RunDirectoryError`` for a design or target
name that is not a name, or for a run directory that scrub refuses to remove (one that is no longer
a directory, for example). It is ``RunRootError`` for a run root that scrub cannot use.

Exit status
===========

Every command exits with 0 on success and with another value on failure. ``xeda run`` exits with 0
when the ``outcome`` is ``passed`` and with 1 for every other outcome (``rejected``, ``failed``,
``canceled`` or ``detached``). It exits with 2 for a request that started no launch: an invalid
setting, design, project or chain, and a mistake in the command line. ``xeda dse`` fails when the
exploration found no successful run. A failed exploration and a failed ``scrub`` exit with status 1.
A usage error of any other command exits with status 2.

Planning a run
==============

``xeda run <flow> <design> --dry-run --json`` prints the resolved plan and runs nothing. Its
document is not the run document. It has the keys ``flow``, ``design``, ``design_file``,
``target``, ``success``, ``dry_run`` (``true``), ``request`` and ``plan``, and each plan node names
its run directory with ``run_path``. The next command is for a design named ``blinky`` whose RTL
top is ``blinky``. It also shows a consumer that switches an optional output of its producer on:

.. code-block:: bash

    xeda run nextpnr blinky.yaml -s fpga.part=iCE40HX1K-TQ144 flows.yosys_fpga.netlist_json= --dry-run --json

.. code-block:: json

    {
      "flow": "nextpnr", "design": "blinky", "design_file": "/path/to/blinky.yaml",
      "target": null, "success": true, "dry_run": true,
      "request": [{"node": "nextpnr", "flow": "nextpnr", "output": null, "through": []}],
      "plan": {
        "requested": "nextpnr",
        "target": null,
        "nodes": [
          {"name": "yosys_fpga", "flow": "yosys_fpga",
           "run_path": "/path/to/xeda_run/blinky/yosys_fpga", "flowrun_hash": "...",
           "settings_hash": "...",
           "state": "stale", "reason": "no completed previous run",
           "inputs": [], "switched_on": ["netlist"], "action_reason": null,
           "input_types": {}, "output_types": {"netlist": ["JsonNetlist"], "netlist_edif": []}},
          {"name": "nextpnr", "flow": "nextpnr",
           "run_path": "/path/to/xeda_run/blinky/nextpnr", "flowrun_hash": "...",
           "settings_hash": "...",
           "state": "stale", "reason": "no completed previous run",
           "inputs": [{"name": "netlist", "origin": "producer", "producer": "yosys_fpga",
                       "output": "netlist", "sources": [],
                       "references": [{"node": "yosys_fpga", "output": "netlist"}],
                       "binding_origin": null, "binding_location": null, "overridden": [], "reason": null},
                      {"name": "constraints", "origin": "none", "producer": null,
                       "output": null, "sources": [], "references": [],
                       "binding_origin": null, "binding_location": null, "overridden": [], "reason": null},
                      {"name": "sdc", "origin": "none", "producer": null,
                       "output": null, "sources": [], "references": [],
                       "binding_origin": null, "binding_location": null, "overridden": [], "reason": null}],
           "switched_on": [], "action_reason": null,
           "input_types": {"netlist": ["JsonNetlist"], "constraints": ["Pcf"], "sdc": ["Sdc"]},
           "output_types": {"config": ["IceAsc"]}}
        ]
      }
    }

``nodes`` is ordered with the producers before the consumers and the requested flow last.
``run_path`` and ``flowrun_hash`` identify each planned run. ``--hashed-run-dirs`` adds the usual
16-character hash suffix. ``flowrun_hash`` is the identity of the node. It combines the settings
(``settings_hash``, the hash of the flow name and the input settings alone), the design
configuration (the ``configuration`` of its record) and the origin of each input, in order: the
identity and output of a producer, or the place and type of the source files of the design. The same
flow with the same settings therefore has another identity when the top or a parameter of the design
is another, or when another producer, another output or another producer configuration feeds it.
Where a binding was written (a file, the command line or a chain) changes nothing.

Each input has these keys:

- ``origin`` is ``producer``, ``source`` or ``none``.
- ``references`` lists, in order, every node and output that supplies the input. A list input can
  have several.
- ``producer`` and ``output`` are those of the first reference, or ``null`` for an input that comes
  from sources or is absent.
- ``sources`` is an ordered list of source paths.
- ``binding_origin`` is ``null`` for sources and default producers. Otherwise it says where the
  binding was given: ``"file"`` (``flows.<flow>.inputs.<input>`` in a design or a project),
  ``"cli"``, ``"api"`` or ``"chain"``.
- ``binding_location`` names that place, and ``overridden`` lists the saved bindings that it
  replaced.
- ``reason`` explains an edge that Xeda chose on its own because the design needs a conversion
  (for example, the design has Bluespec sources, which ``yosys`` does not read). It is ``null``
  for every other input.

An explicit binding supplies an input first, then the sources of the design, and then the default
producer.

``switched_on`` names the optional outputs that Xeda enabled because a consumer needs them. It does
not list every output that the settings already enable. ``input_types`` and ``output_types`` report
the effective types of each declaration after the settings of the nodes agree. For example, iCE40
selects ``Pcf`` pin constraints and the ``IceAsc`` configuration. The static flow catalog lists the
full vocabulary of declared types.

Each node also says what a launch would do with it. ``state`` is one of these:

- ``fresh``: the launch reuses the recorded run.
- ``stale``: the launch runs the node.
- ``unknown``: nothing can be said without running something.
- ``rejected again``: the checks of the node rejected its last run, and nothing it consumes
  changed. The launch reports that rejection again and does not run the tool.
- ``blocked``: a producer of the node is ``rejected again`` or ``blocked``. The launch stops at
  that producer.

``reason`` says why:

- For a stale node, it is the text that the launch logs after ``Running <flow>:``. Examples are ``no
  completed previous run``, ``input changed: <file>``, ``settings changed: <names>``, ``every flow
  runs (--rebuild-all)`` and ``it draws a new random seed``.
- For an unknown node, it says what stops the answer.
- For a rejected again node, it is the message of the rejection.
- For a blocked node, it names the producer: ``<producer> was rejected in its last run``, or
  ``<producer> is blocked`` when that producer is itself blocked.
- For a fresh node, it is empty.

A node is unknown in these cases:

- It depends on a producer that would run (``depends on yosys_fpga``). The answer is the recorded
  outputs of that run, which the launch has to make first.
- Xeda would have to look up the container image of its last run (``its container image is checked
  when it runs``).
- A prepared input does not exist yet, such as a chip database that no run has generated.
- A launch would refuse its settings in the context of the design, or could not hand over a recorded
  output of a producer.

The dry run uses the rules of a launch. A node that it reports as ``fresh`` or ``stale`` is what the
launch does. ``--rebuild-all`` and ``--clean`` make every node stale once its prepared inputs pass.
A chip database cache that is corrupt is refused first, so the node is unknown. The reason is the
one that a launch records too, and a flow that always runs keeps its own reason.

A dry run does not call ``init()``, so ``stale`` means that a launch does not reuse the node. The
launch runs it, unless the node needs the run itself to be refused: a tool that is not on ``PATH``,
installed data that is missing, or an input that a producer hands over. Only a launch finds those
problems.

Planning builds each flow as the launch does, with a run directory that it never writes, to ask
whether the flow always runs. A flow also refuses what its settings and the design rule out alone,
in the pure hooks ``check_settings_supported`` and ``check_design_supported``, and planning calls
them. Invalid settings, conflicts between shared settings, missing required inputs, impossible
targets and any other combination that these hooks refuse produce the usual run document, with
the ``outcome`` ``null`` (no launch started) and an ``error``.

Planning changes no marker, lock, result or delivery, and it writes to the run root only the
storage of the Git dependencies that it fetches (see :ref:`git-dependencies`). It refreshes no
trace, starts no process and probes no tool. If the declarations of the design already rule out the request, Xeda
refuses it first, before a generator runs (see :ref:`generators`). Planning never runs a generator.
With ``--remote``, the host makes the plan (see :doc:`remote`).

Planning a chain
----------------

Xeda plans and runs a chain in the same way. This example uses the ``routed_demo.yaml`` of
:ref:`flow-chains`:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack routed_demo.yaml --dry-run --json

``request`` is the chain as a list of ``{node, flow, output, through}`` elements. It is structural
and never the joined text. ``through`` lists the flows between an element and the next one. It is
empty for a direct link and for the last element. Every node of ``plan.nodes`` has its resolved
``inputs``. The next document is abridged: it shows only the wired inputs of each node, and none of
the paths and hashes:

.. code-block:: json

    {
      "flow": "fpga_pack",
      "success": true,
      "dry_run": true,
      "request": [
        {"node": "yosys_fpga", "flow": "yosys_fpga", "output": null, "through": []},
        {"node": "nextpnr", "flow": "nextpnr", "output": null, "through": []},
        {"node": "fpga_pack", "flow": "fpga_pack", "output": null, "through": []}
      ],
      "plan": {
        "requested": "fpga_pack",
        "nodes": [
          {"name": "yosys_fpga", "inputs": []},
          {"name": "nextpnr", "inputs": [
            {"name": "netlist", "origin": "producer", "producer": "yosys_fpga",
             "output": "netlist", "binding_origin": "chain",
             "references": [{"node": "yosys_fpga", "output": "netlist"}]}
          ]},
          {"name": "fpga_pack", "inputs": [
            {"name": "config", "origin": "producer", "producer": "nextpnr",
             "output": "config", "binding_origin": "chain",
             "references": [{"node": "nextpnr", "output": "config"}]}
          ]}
        ]
      }
    }

In this example, the chain replaced both bindings that the file saved (``overridden`` names them and
their sections). A request for a single flow has a one-element ``request``. In the run document of
a launch that failed, a node that Xeda planned and never entered has the disposition ``not_run``,
and the outcome ``blocked`` or ``skipped``. A chain that Xeda refused before it was planned (a
usage error) has ``"spec": null`` and empty ``roots`` and ``nodes`` in its run document.

Using Xeda as a library
=======================

The same information is available in a process, without a shell. The functions of
``xeda.introspect`` return plain data that can be serialized as JSON:

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

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    runner = DefaultRunner("xeda_run")
    flow = runner.run("vivado_synth", design, flow_settings=["clock.period=5ns"])
    if flow and flow.record and flow.record.passed:
        print(flow.record.metric("fmax"))

A ``Design`` and every value in it is read-only. An assignment raises an error, and a list or
table in a design cannot change.

To inspect a plan without constructing or running flows:

.. code-block:: python

    from xeda.introspect import plan_info

    plan = runner.plan("nextpnr", design, flow_settings=["fpga.part=iCE40HX1K-TQ144"])
    print(plan_info(plan))

``plan_info`` returns the same plain data as the ``plan`` key of ``--dry-run --json``. A
materialized ``Design`` avoids loading and generation. The settings snapshots of a plan are private
copies, and its captured request context is protected. A launcher validates its own internal plans,
and a plan that you supply from outside is not a supported library interface.

Notes for coding agents
=======================

* Prefer ``--json`` to parsing the tables. Table output wraps to the terminal width.
* Discover the settings and do not guess them. ``xeda list-settings <flow> --json`` lists every
  accepted setting. An unknown setting is a hard error, not a warning.
* Setting names and flow names are exact. A second spelling or a near miss is an error that
  suggests the right name.
* ``xeda run --json`` gives both the record of each run (its metrics are in
  ``.nodes[-1].record.metrics``) and the run directory of each node, so you never need to guess
  where the output landed.
* The producers of a flow run automatically. Run the flow you want, not the chain that leads to it.
  Set a producer through its own flow, for example ``-s flows.yosys_fpga.flatten=true``.
* ``xeda run`` is make-like by default. A run with nothing changed runs nothing, and ``nodes`` says
  so for each flow. Force everything to run with ``--rebuild-all``.
