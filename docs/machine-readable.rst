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
    xeda dse vivado_synth --design examples/vhdl/sqrt/sqrt.yaml --json
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
      "aliases": [],
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

``name`` is the canonical name, and ``aliases`` are the other accepted names. ``dependencies`` are
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
- whether it compares ``equivalence`` (see ``results.outputs`` below);
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
          "alias": "ncpus",
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
* ``alias`` is a second accepted name. Both names work.
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
  ``x-xeda-quantity``. The schema separates a number from text, and it cannot check the unit in the
  text.

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

``xeda list-results <flow> --json`` lists the keys that the flow writes to ``results.json`` and the
canonical aliases that Xeda adds:

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

``documented: false`` means that the flow has not declared a ``results_description`` (a flow from a
plugin, for example). Xeda then detects the keys from the source of the flow as a best effort. In
that case, read an actual ``results.json``.

A canonical key names a quantity, not one way to measure it. ``lut`` is reported per toolchain and
per stage. The open-source FPGA flows add ``LUT:STAGE`` (``mapped`` for ``yosys_fpga``, ``placed and
routed`` for ``nextpnr``) and ``LUT:METHOD`` beside it. Counts from different toolchains or stages
are not certified comparable, and this holds in particular for Vivado's utilization report. ``Fmax``
works the same way. Each tool times a design with its own model, so compare ``Fmax`` only between
runs of the same tool (see :ref:`flow-results`).

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

``xeda run <flow> <design> --json`` writes a single JSON object to stdout. In this example,
``nextpnr`` has the producer ``yosys_fpga``, whose recorded run was still valid. The ``textcfg``
setting (the routed ECP5 design, as a Trellis text configuration) was given a location, so Xeda
delivered the file there once the run finished:

.. code-block:: json

    {
      "flow": "nextpnr",
      "design": "blinky",
      "design_file": "/path/to/blinky.yaml",
      "target": null,
      "success": true,
      "results": {"success": true, "Fmax": 212.4, "lut": 45, "...": "..."},
      "run_path": "/path/to/xeda_run/blinky/nextpnr",
      "results_json": "/path/to/xeda_run/blinky/nextpnr/results.json",
      "settings_json": "/path/to/xeda_run/blinky/nextpnr/settings.json",
      "nodes": [
        {"node": "yosys_fpga", "flow": "yosys_fpga",
         "run_path": "/path/to/xeda_run/blinky/yosys_fpga",
         "state": "fresh", "reason": "", "deliveries": [], "inputs": []},
        {"node": "nextpnr", "flow": "nextpnr", "run_path": "/path/to/xeda_run/blinky/nextpnr",
         "state": "ran", "reason": "settings changed: seed",
         "deliveries": [
           {"setting": "textcfg", "from": "/path/to/xeda_run/blinky/nextpnr/config.txt",
            "to": "/home/user/blinky_config.txt", "state": "delivered"}
         ],
         "inputs": [
           {"name": "netlist", "origin": "producer", "producer": "yosys_fpga",
            "output": "netlist", "sources": [],
            "references": [{"node": "yosys_fpga", "output": "netlist"}],
            "binding_origin": null, "binding_location": null, "overridden": [], "reason": null},
           {"name": "constraints", "origin": "none", "producer": null,
            "output": null, "sources": [], "references": [],
            "binding_origin": null, "binding_location": null, "overridden": [], "reason": null},
           {"name": "sdc", "origin": "none", "producer": null,
            "output": null, "sources": [], "references": [],
            "binding_origin": null, "binding_location": null, "overridden": [], "reason": null}
         ]}
      ],
      "request": [{"node": "nextpnr", "flow": "nextpnr", "output": null, "through": []}]
    }

``xeda run`` also takes a chain, such as ``xeda run yosys_fpga+nextpnr+fpga_pack design.yaml``. The
last flow of a chain is the requested flow: ``flow``, ``results``, ``run_path`` and the exit status
are those of that flow. Each earlier flow supplies the compatible required inputs of the next flow
or, when the link passes through flows, the inputs that it feeds in the plan of the next flow. The
``request`` key lists the elements of the request as given, in canonical form. Each element is an
object with ``node``, ``flow``, ``output`` and ``through``, where ``output`` is the ``FLOW.OUTPUT``
qualifier or ``null``, and ``through`` lists the flows that the link to the next element passes
through. A single flow gives a one-element list. Each ``nodes`` entry carries its ``node`` name and
its resolved ``inputs``, which are empty for a flow that declares none. The plan of a dry run uses
the same representation for the inputs (the section on planning, below, describes the fields). When
a producer fails, the planned nodes that were never entered come after the other nodes, with
``"state": "not run"``. A chain that is malformed (an empty element, an unknown flow or output, a
repeated flow, a programmer before the end, or a pair with no compatible output) is a usage error.
It gives one error document and exit status 2, with no ``request``, ``plan`` or ``nodes``. ``xeda
dse`` refuses a chain. ``xeda run --remote`` takes one.

``design`` is always the name of the design: the ``name`` in its design file, or the name you gave
for a design of a project (``--design-name``). ``design_file`` is the absolute path of the design
file that you named, or ``null`` when you named none (a design of a project, or no design at all). A
failure before Xeda reads the design has no name to report for a design file. Its ``design`` is then
``null``, and ``design_file`` still says which file it was. Every ``run`` and ``dse`` document
carries both keys: the document of a failure, of a dry run and of a ``--remote`` run too.

``target`` is the name given with ``--target`` (see :ref:`targets`), or ``null`` without it. This
holds for an unknown name too. Every ``run`` and ``dse`` document carries it, the document of a
failure too, and so does the ``plan`` of a dry run.

``nodes`` lists every run directory that the launch touched, once each and in the order of
completion. This includes the run directory of the requested flow and of each producer. Each entry
has a ``state``:

- ``"fresh"``: the recorded run was still valid, and Xeda reused it without running it again.
- ``"ran"``: the flow ran and succeeded.
- ``"failed"``: the flow ran and failed. Its checks can have rejected the result on this launch.
  A problem with what its producers handed over can also have stopped it.
- ``"rejected again"``: the checks of the flow rejected its last run, and nothing it consumes
  changed. Xeda reported that rejection again without running the flow. ``reason`` is the message
  of the rejection. The launch in which the checks reject the run gives ``"failed"``.
- ``"blocked"``: a producer of the flow failed, was rejected or is blocked, so the flow did not
  run. ``reason`` is ``<producer> did not succeed``.

The ``error`` of the document depends on what stopped the launch. If the requested flow itself was
rejected, ``error.type`` is ``FlowFailed``, on the launch that ran it and when Xeda reports the
rejection again. If a producer stopped the requested flow, the requested flow is ``"blocked"`` and
``error.type`` is ``FlowDependencyFailure``.

For a node that is ``"ran"`` or ``"failed"``, ``reason`` says why the flow ran: it is the text that
the log shows as ``Running <flow>: <reason>``. It is empty for a node that is ``"fresh"``. For a
node that is ``"rejected again"`` or ``"blocked"``, the flow did not run, and ``reason`` is the
text described above. Runs are make-like by default
(without ``--rebuild-all``), so a launch where everything is up to date has ``"success": true`` and
every node ``"fresh"``. See :doc:`run-directories` for what makes a node stale. A flow that always
runs is never ``"fresh"``. Its ``reason`` says why. ``openfpgaloader``, which programs a device, is
one such flow, and so is a flow that is asked for a fresh random seed.

``deliveries`` lists every output that the flow of the node copied to a location that the settings
or ``--outputs-to`` named, once the whole launch finished (see "Outputs where you name them" in
:doc:`run-directories`). Each entry has these keys:

- ``setting`` is the setting (or ``--outputs-to``) that asked for the delivery.
- ``from`` is the file in the run directory.
- ``to`` is the place where Xeda copied the file, or moved it (see :doc:`run-directories`).
- ``state`` is ``"delivered"`` (Xeda wrote the file at the destination, as a new copy or by a move)
  or ``"unchanged"`` (the destination already held exactly that file).

The list is ``[]`` for a node with nothing to deliver. A node that is up to date still lists its
deliveries, because delivery depends on the outcome of the node, whether or not its tool ran.

The records of declared outputs appear as ``results.outputs`` in the CLI document and as the
top-level ``outputs`` key in ``results.json``. Each enabled output that the flow actually produced
maps its name to ``{"path": ..., "sha": ...}``. A list output maps to an ordered list of such
records. ``path`` is absolute and lies inside the run directory of the producer. ``sha`` is the
32-character content digest, not a timestamp. A record of a type that defines one also has
``equivalence``. This is the digest of the file without what its type calls insignificant (for a
JSON netlist, the ``src`` attributes). An input that asks for it compares this digest when ``sha``
differs. Disabled outputs are omitted.

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

``outputs`` is bookkeeping, like ``artifacts``, and not a ``list-results`` metric. The printed
result table omits it.

On a failure, the document carries an ``error`` object beside any available results, and the exit
status is not zero. ``nodes`` still lists every node that ran before the failure and the node that
failed. It is ``[]`` when the error happened before any flow could run, such as a bad design file or
an unknown setting:

.. code-block:: json

    {
      "flow": "vivado_synth",
      "design": "sqrt",
      "design_file": "/path/to/sqrt.yaml",
      "target": null,
      "success": false,
      "results": {},
      "error": {
        "type": "FlowSettingsError",
        "message": "FlowSettingsError: 1 error validating VivadoSynth.Settings\n   Extra inputs are not permitted: no_such_setting (extra_forbidden)"
      },
      "request": [{"node": "vivado_synth", "flow": "vivado_synth", "output": null, "through": []}],
      "nodes": []
    }

``error.type`` names the type of the failure. It is usually the class of an exception, and it is
stable enough to branch on either way:

- ``FlowSettingsError``: a bad setting.
- ``ExecutableNotFound``: the tool is not installed.
- ``NonZeroExitCode``: the tool failed.
- ``DesignValidationError``: a bad design file.
- ``DesignNotSpecified``: you named no design, and no project file provides one.
- ``NoSuccessfulRun``: a design-space exploration found no successful candidate.
- ``FlowDependencyFailure``: a producer that the requested flow needs failed, or Xeda could not
  verify its completed output.
- ``FlowFatalError`` and ``FlowException``.

``FlowFailed`` is the one name that is not an exception class. It is the verdict of the run: the
requested flow ran to its end without raising and did not succeed. The cause is in its
``results.json`` (see below). A flow that raises is named by its exception class and never by
``FlowFailed``.

Four more types come from the isolation of run directories and deliveries:

* ``RunRootError``: the run root (``--run-root`` or ``XEDA_RUN_ROOT``) cannot be used. It holds
  files and has no marker that Xeda created, or it lies where Xeda cannot write. The message names
  the directory and the fix: move it aside, or mark it yourself.
* ``RunDirectoryError``: a run directory cannot be used. It would lie outside its run root, or a
  write inside it would go through a symbolic link that Xeda does not control.
* ``DeliveryError``: Xeda could not deliver an output that a setting or ``--outputs-to`` named. The
  run did not write the file that the setting expected, or the destination is an input of the
  launch, a directory, or inside a run root, or the destination changed after Xeda checked it. This
  type is also the refusal of ``--outputs-to`` for a requested flow that writes no outputs (a
  programmer). Xeda makes that refusal before anything runs, and the message names what delivers the
  file that the flow reads.
* ``OutputExistsError``: a subclass of ``DeliveryError``. The destination holds a file that is not
  Xeda's own unchanged earlier delivery, and nobody confirmed the replacement. Run again with
  ``--overwrite-outputs``, or answer the prompt at an interactive terminal.

An error in the command line itself is a usage error. This covers an unknown flow, an unknown
option, a design file that does not exist and a malformed chain. Xeda prints one error document with
the class of the click error as ``error.type`` (``BadParameter``, ``NoSuchOption``, ``UsageError``)
and exits with status 2. Such a document has no ``request`` or ``nodes``. Xeda applies several
refusals before it starts a run, and they use the same document with ``UsageError``. For example,
``--remote`` refuses ``--post-cleanup``, and ``--keep-running`` and ``--fetch-all`` need
``--remote``. A binding that cannot be applied (an unknown input or producer, a binding for a flow
that declares no inputs, or a chain together with a command-line or API binding of the same input)
is a settings error. It names the input and the place where the binding was written.

The ``results.json`` of every node is a failure document too. Its ``error`` has a stable ``code``,
a ``message``, the ``node``, the ``exit_status`` of the tool (for ``nonzero_exit``) and a ``log``
(the artifact to read, or ``null``). The ``code`` is lower snake case and never the name of a
class. The top-level ``FlowFailed`` is the verdict of the run, and a node names the cause. A
failure of a producer is never ``FlowFailed``. The ``results.json`` of the producer names its
cause, and the flows after it say ``producer_failed`` and quote the producer, its code and its
message (``producer yosys_fpga failed (nonzero_exit): ...``). The top-level ``error`` of a chain
whose last flow was never reached says the same. A flow that raises has a code for the kind of
failure: ``nonzero_exit``, ``timeout``, ``executable_not_found``, ``reported_failure`` (also for
``FlowFatalError``), ``producer_failed`` or ``internal_error`` (a bug of Xeda). A run that was
judged negative after its reports were read says ``rejected``. Three more causes belong to a flow
that finished without raising. The top-level document of such a flow says ``FlowFailed``:

* ``reported_failure``: ``"`<flow>` reported failure: its reports or checks did not pass (see the log
  and its run directory)"``. A ``parse_reports()`` or ``check_results()`` that does not raise
  reported the failure: the tool exited 0, and the reports or checks that the flow reads say
  otherwise. A flow can give a message of its own. For example, ``openfpgaloader`` says what its
  loader reported and quotes the lines.
* ``missing_report``: the reports of the flow could not be read, because a report it asked for
  does not exist or is a previous run's, not this run's. The message names the files.
* ``missing_output``: the flow passed its own checks, but an enabled declared output is absent,
  unreadable, outside the run directory or not written by this run (see ``outputs`` above).

``nextpnr`` can catch a missing enabled output earlier. It raises an error that names the
configuration setting and the path that its tool did not write. This is a raised error with the code
``reported_failure``, and not a ``missing_output``.

Other documents of executional commands
---------------------------------------

The document of ``xeda run --remote ... --json`` is the document of a run, as above, with these
differences. It has the key ``remote``, which holds the text of ``--remote``, and the key
``mirror``, the directory ``<run root>/@<host>`` that holds the fetched files. ``run_path``,
``results_json``, ``settings_json`` and the ``run_path`` of each ``nodes`` entry name directories
in the mirror, and the paths in ``results`` are paths in the mirror. ``design_file`` is your local
design file. A failure of the remote step itself (a lost connection or an error while it makes the
environment) gives the failure document of a run, with ``remote`` and without ``mirror``.

The document of a launch that Xeda stopped following (``--keep-running``, then Ctrl-C or a lost
connection) is a failure with ``error.type`` ``RemoteDetached``, and the key ``launch``, an object
with the ``id`` of the launch and the ``host``. The launch keeps running on the host.

The document of a remote dry run is the document of a local dry run with these keys: ``remote``,
``mirror``, and ``uploaded``, the list of input files that the dry run sent to the host. Each node
of ``plan`` has the ``run_path`` that the run would use in the mirror, and ``remote_run_path``, the
directory on the host.

``xeda remote attach HOST [LAUNCH_ID] --json`` prints the document of the finished run, with the
key ``launch`` (``id`` and ``host``). The other commands of ``xeda remote`` print one document
each. A failure is ``{"success": false, "error": {"type": ..., "message": ...}}`` with exit status
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

Every command exits with 0 on success and with another value on failure. ``xeda run`` fails when
``results.success`` is false, and ``xeda dse`` fails when the exploration found no successful run. A
failed run or exploration, and a failed ``scrub``, exit with status 1. A usage error exits with
status 2.

Planning a run
==============

``xeda run <flow> <design> --dry-run --json`` prints the resolved plan and runs nothing. The next
command is for a design named ``blinky`` whose RTL top is ``blinky``. It also shows a consumer that
switches an optional output of its producer on:

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
(``settings_hash``, the hash of the flow name and the input settings alone) with the origin of each
input, in order: the identity and output of a producer, or the sources of the design. The same flow
with the same settings therefore has another identity when another producer, another output or
another producer configuration feeds it. Where a binding was written (a file, the command line or a
chain) changes nothing.

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
targets and any other combination that these hooks refuse produce the usual document with ``success:
false`` and an ``error``.

Planning changes no run root, marker, lock, result or delivery. It refreshes no trace, starts no
process and probes no tool. If the declarations of the design already rule out the request, Xeda
refuses it first, before a generator runs (see :ref:`generators`). Loading that needs a generator or
the fetch of a Git dependency is refused before those side effects. Materialize the sources first,
or pass an already materialized ``Design`` to ``DefaultRunner.plan``. With ``--remote``, the host
makes the plan (see :doc:`remote`).

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
their sections). A request for a single flow has a one-element ``request``. After a failure, the
nodes that Xeda planned and never entered are listed with ``"state": "not run"``. A delivery that
Xeda refuses before the tool of a producer runs leaves the requested flow in that state too. A chain
that Xeda refused before it was planned (a usage error) has no ``request`` and no ``nodes`` in its
document.

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

    design = Design.from_file("examples/vhdl/sqrt/sqrt.yaml")
    runner = DefaultRunner("xeda_run")
    flow = runner.run("vivado_synth", design, flow_settings=["clock.period=5ns"])
    if flow and flow.results.success:
        print(flow.results.Fmax)

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
* Setting names are exact. Flow names are forgiving: dashes, underscores and the CamelCase class
  name all resolve, and a near miss suggests alternatives.
* ``xeda run --json`` gives both the parsed results and the paths to the run directory, so you never
  need to guess where the output landed.
* The producers of a flow run automatically. Run the flow you want, not the chain that leads to it.
  Set a producer through its own flow, for example ``-s flows.yosys_fpga.flatten=true``.
* ``xeda run`` is make-like by default. A run with nothing changed runs nothing, and ``nodes`` says
  so for each flow. Force everything to run with ``--rebuild-all``.
