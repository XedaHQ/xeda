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
      "description": "Simulate the testbench on the routed netlist `vivado_synth` writes. ...",
      "category": "simulation",
      "supports_cocotb": false,
      "dependencies": ["vivado_synth"],
      "action_reason": null,
      "inputs": ["..."], "outputs": ["..."],
      "can_follow": [], "can_precede": ["..."],
      "settings_class": "xeda.flows.vivado.vivado_postsynthsim.VivadoPostsynthSim.Settings"
    }

``name`` is the canonical name; ``aliases`` are the other accepted names. ``dependencies`` are the
default producers of the flow's declared inputs. A design source or a binding can replace one, so
use the resolved plan for the producers a request needs.

A flow's ``inputs`` and ``outputs`` list its declared file I/O: their
names, accepted source ``types``, ``cardinality`` (``one``, ``optional``, ``many``) and descriptions;
inputs also say whether they are ``required`` (the flow cannot run without one) and whether they are
``optional`` (an optional list, which may be empty), and name their default ``producer`` and
``output``; outputs name their ``enabled_by`` setting. An input that has to come from the same
producer as another input of the flow names it in ``same_producer_as``. With ``via`` the relation
goes one step further: the input has to come from the producer of that input of the flow that makes
the ``same_producer_as`` input (``vivado_power``'s ``checkpoint`` comes from the synthesis whose
``netlist_timing`` the simulation behind ``activity`` read). Both are ``null`` for most inputs.
Only generated inputs, those with a producer in the plan, are compared: an input that comes from
a source you list, or that is absent, is not.
For example, ``nextpnr`` declares input ``netlist`` of type ``JsonNetlist``, default producer
``yosys_fpga``, output ``netlist``. Sources of an accepted type displace that default producer.
Use the resolved plan for the producers a particular request actually needs.

``can_precede`` lists the flows that can come directly after this one in a chain
(``xeda run this+other``) and ``can_follow`` those it can come directly after. Both are judged by the
validator ``xeda run`` applies, on declarations alone, so a pair that the validator refuses is
never listed. Planning checks one more thing that declarations cannot show: related inputs of a flow
must come from one producer. A listed pair can still be refused there, with the bindings to add
(``vivado_alt_synth`` is listed before ``vivado_power``, whose ``activity`` still comes from
``vivado_synth`` unless you bind the simulation too). Each entry has ``flow``, ``output`` (the producer output the
request must name, ``null`` when the unqualified request is valid), ``binds`` (the ``input`` and
``output`` pairs the adjacency binds) and ``target_dependent``: true when a produced or accepted
kind is a union that the target selects from (``nextpnr`` makes the configuration of the target's
family), so the resolved plan is authoritative. A flow that declares no I/O has no relations, and
``action_reason`` (static, ``null`` for most flows) says why a flow, such as a programmer, can
only end a chain.

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

A canonical key names a quantity, not one way of measuring it. ``lut`` is reported per toolchain
and per stage: the open-source FPGA flows add ``LUT:STAGE`` (``mapped`` for ``yosys_fpga``,
``placed and routed`` for ``nextpnr``) and ``LUT:METHOD`` beside it. Counts from different
toolchains or stages are not certified comparable, with Vivado's utilization report in particular.
``Fmax`` is the same: each tool times a design with its own model, so compare it only between
runs of the same tool (see :ref:`flow-results`).

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
            "binding_origin": null, "binding_location": null, "overridden": []},
           {"name": "constraints", "origin": "none", "producer": null,
            "output": null, "sources": [], "references": [],
            "binding_origin": null, "binding_location": null, "overridden": []},
           {"name": "sdc", "origin": "none", "producer": null,
            "output": null, "sources": [], "references": [],
            "binding_origin": null, "binding_location": null, "overridden": []}
         ]}
      ]
    }

``xeda run`` also takes a chain, ``xeda run yosys_fpga+nextpnr+fpga_pack design.yaml``: its
last flow is the requested one (``flow``, ``results``, ``run_path`` and the exit status are
that flow's), and each preceding flow supplies the next one's compatible required inputs. The
document's ``request`` lists the elements as given, canonically -- ``{"node", "flow",
"output"}`` each, ``output`` being the ``FLOW.OUTPUT`` qualifier or ``null`` -- one element for
a single flow. Each ``nodes`` entry carries its ``node`` name and its resolved ``inputs`` (empty
for a flow that declares none) in the representation of the plan (the fields are under "Planning
a run" below). When a producer fails, the planned nodes that were never entered are listed
after the others with ``"state": "not run"``. A malformed chain (an empty element, an unknown
flow or output, a repeated flow, an edge that does not fit) is a usage error: one error
document, exit status 2, no ``request``, ``plan`` or ``nodes``. Chains are local requests:
``--remote`` and ``dse`` refuse them.

``design`` is always the design's name: the ``name`` in its design file, or the name you gave for
a design of a project (``--design-name``). ``design_file`` is the absolute path of the design file
you named, or ``null`` when you named none (a design of a project, or no design at all). A failure
before the design is read has no name to report for a design file, so its ``design`` is ``null``
and ``design_file`` still says which file it was. Every ``run`` and ``dse`` document carries both,
a failure's, a dry run's and a ``--remote`` run's too.

``target`` is the name of the design's selected target (see :ref:`targets`), or ``null`` for a
design without targets. Every ``run`` and ``dse`` document carries it, a failure's too, and so
does the ``plan`` of a dry run.

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
``from`` the file in the run directory, ``to`` where it was copied (or moved: see
:doc:`run-directories`), and ``state`` either ``"delivered"`` (the file was written at the
destination, as a new copy or by a move) or ``"unchanged"`` (the destination already held exactly
that file). It is ``[]`` for a node with nothing to deliver, and a fresh node still lists its
deliveries: delivery follows a node's outcome, not whether its tool ran.

Declared output records appear as ``results.outputs`` in the CLI document and as the top-level
``outputs`` key in ``results.json``.
Each enabled output actually produced maps its name to ``{"path": ..., "sha": ...}``; a list
output maps to an ordered list of those records. ``path`` is absolute, inside the producer's run
directory; ``sha`` is its 32-character content digest, not a timestamp. Recording checks readable
files, containment and current-run writes. Hand-over checks record schema, cardinality,
containment and content under a verified producer read lease, whether the producer ran or was
reused. A failure to record an output uses ``MissingOutput`` and the normal failure identity; a
consumer's failed hand-over check is a ``FlowDependencyFailure``. Flow-specific checks can fail
earlier: ``nextpnr`` raises ``FlowFatalError`` naming an enabled configuration setting/path that
its tool did not write. Disabled outputs are omitted.
``outputs`` is bookkeeping, like ``artifacts``, rather than a ``list-results`` metric; it is
omitted from the printed result table.

On failure the document carries an ``error`` object alongside any available results, and the exit
status is non-zero. ``nodes`` still lists every node that ran before the failure, and the one that
failed; it is ``[]`` when the error happened before any flow could run (a bad design file, an
unknown flow or setting):

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
      "nodes": []
    }

``error.type`` names the failure type -- usually an exception class, and stable enough to
branch on either way:
``FlowSettingsError`` (a bad setting), ``FlowNotFoundError`` (a bad flow name),
``ExecutableNotFound`` (the tool is not installed), ``NonZeroExitCode`` (the tool failed),
``DesignValidationError`` (a bad design file), ``NoSuccessfulRun`` (a DSE search found no
successful candidate), ``FlowDependencyFailure`` (a producer the requested flow needs failed, or
its completed output could not be verified), ``FlowFatalError``, and ``FlowException``. The one name that is
not an exception class is ``FlowFailed``, the run's verdict: the requested flow
itself ran to its end without raising and did not succeed (its own cause is in its
``results.json``, below). A flow that raises is named by its exception class, never by
``FlowFailed``. Four more come from run-directory and delivery isolation:

* ``RunRootError`` -- the run root (``--run-root``/``XEDA_RUN_ROOT``) cannot be used: it holds
  files and carries no marker Xeda created, or it lies where Xeda cannot write. Names the
  directory and the fix (move it aside, or mark it yourself).
* ``RunDirectoryError`` -- a run directory itself cannot be used: it would lie outside its run
  root, or a write inside it would go through a symbolic link Xeda does not control.
* ``DeliveryError`` -- an output named by a setting or ``--outputs-to`` could not be delivered:
  the run did not write the file the setting expected, the destination is an input of the launch,
  a directory, or inside a run root, or it changed after it was checked. It is also the refusal
  of ``--outputs-to`` for a requested flow that writes no outputs (a programmer), made before
  anything runs and naming what delivers the file it reads.
* ``OutputExistsError`` -- a subclass of ``DeliveryError``: the destination holds a file that is
  not Xeda's own unchanged earlier delivery, and replacing it was not confirmed. Rerun with
  ``--overwrite-outputs``, or answer the prompt at an interactive terminal.

Every node's own ``results.json`` is a failure document too, and its ``error.type`` is more
specific than the top-level document's: the top-level ``FlowFailed`` is the run's verdict (the
requested flow itself ran and did not succeed), while a node names the cause. A producer's failure
is never ``FlowFailed``: the producer's own ``results.json`` names its cause, and the flows
downstream of it say ``FlowDependencyFailure`` and quote it (``dependency yosys_fpga failed:
...``), as does the top-level ``error`` of a chain whose last flow was never reached. A flow that
raises names its exception class (``NonZeroExitCode``, ``FlowFatalError``, ...). Two causes belong
to a flow that finished without raising, and the top-level document of such a flow says
``FlowFailed``:

* ``ReportedFailure`` -- ``"`<flow>` reported failure: its reports or checks did not pass"`` --
  a failure that a non-throwing ``parse_reports()`` or ``check_results()`` reported: the tool
  exited 0 and the reports or checks the flow reads say otherwise. A flow can give a message of
  its own: ``openfpgaloader`` says what its loader reported and quotes the lines.
* ``MissingOutput`` -- the flow passed its own checks, but an enabled declared output is absent,
  unreadable, outside the run directory or not written by this run (see ``outputs`` above).

A missing enabled output can also be caught earlier, by the flow itself: ``nextpnr`` raises
``FlowFatalError`` naming the configuration setting/path its tool did not write, which is a raised
error and not a ``MissingOutput``.

A chain that is malformed or does not fit (an empty element, an unknown or repeated flow,
a programmer before the end, a pair with no compatible output) is a ``UsageError`` with exit status 2
and no ``request``; a binding that cannot be applied (an unknown input or producer, a binding for
a flow that declares no inputs, a chain and a command-line or API binding of the same input) is a
settings error naming the input and where the binding was written.

Removing run directories
========================

``xeda scrub <flow> <design> --json`` removes a flow's previous run directories for one design
(see :doc:`run-directories`) and writes a single JSON object to stdout. The listing, the
confirmation prompt and the lines about the directories scrub kept or found gone, and the links it
skipped, go to stderr, with the rest of its output:

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

``target`` is the ``--target`` that was given, or ``null``. The five lists hold paths:

* ``scanned`` -- the directories scrub searched for run directories of the flow.
* ``scrubbed`` -- the run directories it removed. The list is empty if scrub found none, or if you
  did not confirm.
* ``kept`` -- the run directories it listed and did not remove, because their run records
  (``results.json`` and ``trace.json``) changed after the listing: a run finished there, or a
  launch found its run fresh and refreshed its trace. What is there is no longer what was
  confirmed.
* ``gone`` -- the run directories it listed that were not there any more when its turn came:
  another scrub or a purge removed them first. That is what scrub was asked to do, so it is no
  error.
* ``skipped`` -- the links named like a run directory of the flow that scrub did not list,
  because they do not lead to a run directory of the flow beside them (see :doc:`run-directories`).
  Scrub removed neither the link nor what it leads to. It says why on stderr, one line for each.

No path is in more than one of ``scrubbed``, ``kept``, ``gone`` and ``skipped``. A failure writes
``{"success": false, "flow": ..., "design": ..., "target": ..., "error": {"type": ...,
"message": ...}}`` and exits with status 1. ``error.type`` is ``RunDirectoryError`` for a design or
target name that is not a name, or a run directory scrub refuses to remove (one that is no longer
a directory, for example), and ``RunRootError`` for a run root it cannot use.

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
      "flow": "nextpnr", "design": "blinky", "design_file": "/path/to/blinky.yaml",
      "target": null, "success": true, "dry_run": true,
      "request": [{"node": "nextpnr", "flow": "nextpnr", "output": null}],
      "plan": {
        "requested": "nextpnr",
        "target": null,
        "nodes": [
          {"name": "yosys_fpga", "flow": "yosys_fpga",
           "run_path": "/path/to/xeda_run/blinky/yosys_fpga", "flowrun_hash": "...",
           "settings_hash": "...",
           "inputs": [], "switched_on": ["netlist"], "action_reason": null,
           "input_types": {}, "output_types": {"netlist": ["JsonNetlist"], "netlist_edif": []}},
          {"name": "nextpnr", "flow": "nextpnr",
           "run_path": "/path/to/xeda_run/blinky/nextpnr", "flowrun_hash": "...",
           "settings_hash": "...",
           "inputs": [{"name": "netlist", "origin": "producer", "producer": "yosys_fpga",
                       "output": "netlist", "sources": [],
                       "references": [{"node": "yosys_fpga", "output": "netlist"}],
                       "binding_origin": null, "binding_location": null, "overridden": []},
                      {"name": "constraints", "origin": "none", "producer": null,
                       "output": null, "sources": [], "references": [],
                       "binding_origin": null, "binding_location": null, "overridden": []},
                      {"name": "sdc", "origin": "none", "producer": null,
                       "output": null, "sources": [], "references": [],
                       "binding_origin": null, "binding_location": null, "overridden": []}],
           "switched_on": [], "action_reason": null,
           "input_types": {"netlist": ["JsonNetlist"], "constraints": ["Pcf"], "sdc": ["Sdc"]},
           "output_types": {"config": ["IceAsc"]}}
        ]
      }
    }

``nodes`` is ordered with producers before consumers and the requested flow last. ``run_path``
and ``flowrun_hash`` identify each planned run; ``--hashed-run-dirs`` adds the usual 16-character
hash suffix. ``flowrun_hash`` is the node's identity: its settings (``settings_hash``, the hash
of the flow name and input settings alone) together with where each input comes from, in order
-- a producer's own identity and output, or the design's sources. So the same flow with the
same settings has another identity when another producer, another output or another producer
configuration feeds it, and where a binding was written (file, command line, chain) changes
nothing. Each input's ``origin`` is ``producer``, ``source`` or ``none``. ``references``
lists, in order, every node and output that supplies the input (a list input may have several);
``producer`` and ``output`` are the first one's, or ``null`` for source/absent inputs;
``sources`` is an ordered list of source paths. An input is supplied by an explicit binding
before the design's sources, and by those before its default producer. ``binding_origin`` is
``null`` for sources and default producers, else where the binding was given: ``"file"`` (a
design's or project's ``flows.<flow>.inputs.<input>``), ``"cli"``, ``"api"`` or ``"chain"``;
``binding_location`` names that place and ``overridden`` the saved bindings it replaced. ``switched_on`` names optional outputs enabled because a
consumer needs them, not every output already enabled by its settings. ``input_types`` and
``output_types`` report each declaration's effective types after target agreement; for example,
iCE40 selects ``Pcf`` pin constraints and ``IceAsc`` configuration. The static flow catalog
reports the full declared type vocabulary.

Planning does not call a flow's ``init()``. Freshness and always-run decisions are not evaluated.
Invalid settings, shared-setting conflicts, missing required inputs and impossible targets
produce the usual ``success: false`` / ``error`` document.

Planning changes no run roots, markers, locks, results or deliveries and probes no tools.
A request that the design's declarations already rule out is refused first, before a generator
runs (see :ref:`generators`). Loading that needs a generator or a Git dependency fetch is then
refused before those side effects;
materialize sources first, or pass an already materialized ``Design`` to ``DefaultRunner.plan``.
``--dry-run --remote`` is refused.

Planning a chain
----------------

A chain is planned (and run) the same way. With the ``routed_demo.yaml`` of :ref:`flow-chains`:

.. code-block:: bash

    xeda run yosys_fpga+nextpnr+fpga_pack routed_demo.yaml --dry-run --json

``request`` is the chain as a list of ``{node, flow, output}`` elements -- structural, never the
joined text -- and every node of ``plan.nodes`` has its resolved ``inputs``. Abridged (only each
node's wired inputs, and none of the paths and hashes):

.. code-block:: json

    {
      "flow": "fpga_pack",
      "success": true,
      "dry_run": true,
      "request": [
        {"node": "yosys_fpga", "flow": "yosys_fpga", "output": null},
        {"node": "nextpnr", "flow": "nextpnr", "output": null},
        {"node": "fpga_pack", "flow": "fpga_pack", "output": null}
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

Here the chain replaced both bindings the file saved (``overridden`` names them and their
sections). A request of a single flow has a one-element ``request``. After a failure the nodes
that were planned but never entered are listed with ``"state": "not run"``. A delivery that
Xeda refuses before the tool of a producer runs leaves the requested flow in that state too. The
``request`` and ``nodes`` keys are absent from the document of a chain that was refused before it
was planned (a usage error).

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
  Set a dependency through its own flow, e.g. ``-s flows.yosys_fpga.flatten=true``.
* ``xeda run`` is make-like by default: re-running with nothing changed re-runs nothing, and
  ``nodes`` says so per flow. Force everything to run with ``--rebuild-all``.
