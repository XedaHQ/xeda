***********************
Use Xeda from Python
***********************

Everything that ``xeda run`` does is available from Python. The command line is a client of the
same API. This page explains the main objects and the rules that you need to use them.
:doc:`api/index` lists every name, signature and field. The `notebooks
<https://github.com/XedaHQ/xeda/tree/main/notebooks>`_ show the same code with real output.

Three modules are public: ``xeda``, ``xeda.errors`` and ``xeda.introspect``. Everything else is
internal and can change in any release (see :doc:`stability`). Do not import an internal module.

The examples run from the root of the Xeda repository, with the example designs in ``examples/``.
They use the ``vivado_synth`` flow, but any flow works the same way: change the flow name and the
settings. To see which flows, settings and metrics exist, use ``xeda.introspect`` (see `Find a
flow, its settings and its metrics`_).

Overview
========

A launch has four steps:

1. Load a design: ``Design.load(path)``.
2. Say what to run: ``Spec(flow, design, settings=...)``. The ``flow`` is a flow name or a chain
   such as ``"yosys_fpga+nextpnr"``.
3. Run the spec in a workspace: ``Workspace(run_root).run(spec)``. The workspace holds the run root
   and the options of your session.
4. Read the answer: a ``Result``. It has an outcome, an error and one ``NodeResult`` for each node
   of the launch. ``result.record`` is the ``RunRecord`` of the flow you asked for.

.. code-block:: python

    # quickstart.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})

    result = Workspace("xeda_run").run(spec)
    print(result.outcome)                    # passed
    record = result.record
    print(record.metric("fmax"), record.metric_info["fmax"].unit)    # 459.5 MHz
    print(record.run_dir)                    # where the files of this run are

The module-level function ``xeda.run(spec, run_root="xeda_run")`` does the same in one call. It
makes a workspace for the length of the call. Use a ``Workspace`` when you make several calls with
the same options. The run root is, in this order: the one you give; else ``local_settings.run_root``
when you pass ``local_settings`` and it has one; else ``xeda_run`` in the current directory at the
call.

The text of ``repr(result)`` is for people, and it can change in any release. A script reads
``result.outcome``, ``result.error`` and ``result.to_dict()``. ``Outcome`` is a string enumeration:
``result.outcome == "passed"`` is true.

The objects are read-only values, except the workspace. A ``Workspace`` keeps no state of a launch.
A ``Plan`` is what a launch would do, and a ``RunRecord`` is the stored description of one run (the
``results.json`` file of a run directory). A ``Project`` and a ``LocalSettings`` hold the settings
of a project file and of one user (see `Projects and private settings`_).

The result and record classes (``Result``, ``NodeResult``, ``RunRecord``, ``Plan``, ``Event`` and
the others that Xeda builds for you) take keyword arguments only. ``Spec(flow, design)`` and
``Remote(host)`` keep their one or two positional parameters; their other parameters are keyword
only too. The first parameter of ``Workspace.run`` and ``xeda.run`` is positional only and is named
``request``.

Outcomes and errors
===================

A tool failure is an outcome. A wrong request is an exception.

* A launch that starts always returns a ``Result``. A tool that fails, a timeout, a missing
  executable, a rejected timing result, a failed producer and a cancel are outcomes of nodes, not
  exceptions.
* A spec that is not valid raises a subclass of ``SpecError`` before anything runs. No tool and no
  generator has started.
* ``KeyboardInterrupt`` is not an outcome. It propagates after Xeda stops the tool and releases its
  locks.

``result.outcome`` is ``passed``, ``rejected``, ``failed``, ``skipped``, ``canceled`` or
``detached``. In a chain, it is the outcome of the cause: if the first flow fails, the launch is
``failed`` and the next node is ``blocked`` (``blocked`` is an outcome of a node only).
``result.passed`` is true only for ``passed``. A ``rejected`` run is not passed: the tool ran, but
a result does not meet a condition, for example a timing report that does not meet the clock. Its
metrics stay, so you can see why.

``result.error`` is ``None`` for a pass. Otherwise it is an ``ErrorInfo`` with a stable ``code``, a
``message``, the ``node`` that caused it, the ``exit_status`` of a failed tool and the artifact
key of its ``log``. The codes are in :doc:`machine-readable`.

``result.nodes`` has one ``NodeResult`` for each planned node, in plan order. A node has an outcome,
a disposition (``ran``, ``reused``, ``not_run``, or ``shared`` in a sweep), a ``reason``, the
``elapsed`` time, its ``record``, its ``error`` and its ``deliveries``. ``node.passed`` is true
only for the outcome ``passed``. ``name`` identifies the node in the plan, and ``flow`` is the
canonical name of its flow.

A duration is text with a unit, always in seconds: ``node.elapsed`` is ``"1.25 s"``, and it is
``"0 s"`` for a node that this launch did not run. ``RunRecord.runtime`` has the same form. It is
the duration of the recorded execution, which stays when a later launch reuses the run. A point in
time is an aware UTC ``datetime`` (``RunRecord.started_at``, ``RunRecord.finished_at`` and
``Event.occurred_at``). In a JSON document it is ISO 8601 text in UTC.

``result.record`` raises ``RecordError`` (code ``missing_record``) when the requested node has no
record: it was blocked, skipped or detached, or Xeda could not store the record. To test for it,
use ``result.nodes[name].record is not None``.

``print(result)`` shows the outcome, the cause and one entry for each node. For a missing tool in a
two-node chain, it prints the text below. A bare ``result`` in Jupyter shows the same text. The
layout is for people to read and it can change: read the fields of the ``Result`` in code.

.. code-block:: text

    Result: failed
      cause: yosys_fpga [executable_not_found]: Executable 'yosys' was not found.
      yosys_fpga: failed (ran)
        record: /runs/fifo/yosys_fpga/results.json
      nextpnr: blocked (not_run); blocked by yosys_fpga

In a test or a notebook, use ``result.require_passed()``. It returns the result when the launch
passed. Otherwise it raises ``RunFailedError``, and ``error.result`` holds the result. The text of
the error names the node that caused the failure, its error code and its message:
``launch ended failed: yosys_fpga [executable_not_found]: Executable 'yosys' was not found.``

.. code-block:: python

    # outcomes.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})
    result = Workspace("xeda_run").run(spec).require_passed()

    for name, node in result.nodes.items():
        print(name, node.outcome, node.disposition, node.elapsed)

A simulation passes only on evidence that it ended. The metrics ``sim.ended_by``, ``sim.errors``
and ``sim.warnings`` show the evidence. A testbench that never ends is not a pass. Read the
metrics of the flow with ``xeda.introspect.results_info(flow)``.

Designs
=======

``Design.load(path)`` reads a design file, expands dotted keys and source patterns, and checks the
result. It runs no generator and no Git fetch: they run inside the launch. Write new designs in
YAML (see :doc:`design-file`). You can also build a design in Python. It takes the keys of a design
file as keyword arguments. ``name`` and ``root`` are required, and ``root`` is the folder that
relative paths refer to.

.. code-block:: python

    # design.py
    from xeda import Design

    fifo = Design(
        name="fifo",
        root="examples/sv/fifo",
        rtl={"sources": ["fifo.sv"], "top": "fifo", "clocks": [{"port": "clk"}]},
        tb={"sources": ["fifo_tb.sv"], "top": "fifo_tb"},
    )
    print(fifo.name, fifo.rtl.top, [source.path.name for source in fifo.rtl.sources])

    wide = fifo.with_updates({"rtl.parameters.DATA_WIDTH": 16})
    print(fifo.rtl.parameters, wide.rtl.parameters)    # the original does not change
    print(wide.updates)                                # the arguments of each with_updates call

``Design(...)`` behaves like ``Design.load`` of a file in ``root`` whose content is the keyword
arguments. A relative path resolves against ``root``, a pattern such as ``*.sv`` expands, a dotted
key expands, and a Python ``None`` is a YAML ``null``. A mistake raises ``DesignError``, and the
``field`` of the error names the key. Xeda does not check that ``top`` names a module: the tool does.

A design and everything in it is read-only. An assignment raises an error, and a list or a table of
a design cannot change. ``with_updates(updates)`` returns a new design. Mappings merge, ``sources``
replace, and an unknown key raises ``DesignError``. ``design.with_target(name)`` selects a target
(a named overlay of a design, such as a board), and ``Design.load(path, target=name)`` loads with
one. ``target=None`` means no target everywhere. ``design.to_dict()`` gives the effective design in
the shape of a design file.

Settings
========

``Spec.settings`` is the contents of a ``flows`` table. Each top-level key names the node that
receives the setting. In this release, the node name is the flow name. A nested table and a dotted
key mean the same thing, and the command line uses the same address (``-s nextpnr.seed=7``):

.. code-block:: python

    {"nextpnr": {"seed": 7}, "yosys_fpga": {"clock": {"period": "10 ns"}}}
    {"nextpnr.seed": 7, "yosys_fpga.clock.period": "10 ns"}      # the same settings

A setting accepts exactly the input forms that its type lists, from Python and from a file.
Xeda converts no value to another type. A wrong form raises ``SettingsError`` (for example with the
code ``invalid_value``). In particular:

* A physical quantity is text with a unit: ``"10 ns"``, ``"100 MHz"``. A bare number raises
  ``SettingsError`` (``invalid_quantity``). A record, a plan and a sweep column hold a
  quantity in its canonical text, in the unit of its setting.
* An integer is an ``int``. The text ``"1"``, the number ``1.0`` and ``True`` are refused.
* A boolean is ``True`` or ``False`` only.
* A list is a list, or comma-separated text. Text in brackets is refused.
* A relative path in a setting that the flow reads is relative to the design root.

A setting that two flows of a chain share (``fpga``, ``board``, ``clocks`` and a few more) goes to
every node that declares it. Set it once, on any node. If two nodes get different values, Xeda
raises ``SettingsError`` with the code ``shared_setting_conflict``.

Settings come from layers, and the last layer wins: the defaults of the flow, the user file, the
host profile, the project, the design, the target, the project-local file and ``Spec.settings``
(see :doc:`flows`). Every value remembers its layer, and ``SettingsError.origin`` names it.

Xeda checks all settings when you plan or run. An error raises a subclass of ``SpecError`` before
anything runs. ``SettingsError`` has the attributes ``code``, ``message``, ``node``, ``setting``,
``problem``, ``suggestions``, ``origin`` and ``errors``. One validation can find several mistakes:
the exception is the first of them, and ``errors`` holds all of them.

.. code-block:: python

    # settings_errors.py
    from xeda import Design, Spec, Workspace
    from xeda.errors import SettingsError, SpecError

    design = Design.load("examples/sv/fifo/fifo.xeda.yaml")
    ws = Workspace("xeda_run")

    def check(settings):
        try:
            ws.plan(Spec("verilator", design, settings=settings))
        except SettingsError as error:
            for item in error.errors:
                print(item.code, "|", item.node, item.setting, "|", item.suggestions)
        except SpecError as error:
            print(error.code)

    check({"vcd": True})                          # unscoped_setting: the key has no node
    check({"verilator": {"vc": True}})            # unknown_setting: suggestions list close names
    check({"verilator": {"vcd": "yes"}})          # boolean_text: a boolean is True or False
    check({"verilator": {"stop_time": 5}})        # invalid_quantity: write a unit, "5 ns"
    check({"yosys": {"flatten": True}})           # unknown_node: the plan has no node yosys

Catch ``SpecError`` when you do not care which error it is.

Plan before you run
===================

``ws.plan(spec)`` is the Python form of ``xeda run --dry-run``. It starts no tool. It can fetch a
Git dependency of the design, into the run root. ``plan.nodes`` maps each node name to a
``PlanNode`` in execution order. The ``state`` of a node says what a launch would do:

``fresh`` (the launch would reuse the passed record), ``rejected`` (it would reuse the rejected
record), ``stale`` (it would run; ``reason`` says why), ``unknown`` (it depends on a producer that
would run) or ``blocked`` (a producer is rejected or blocked).

The state is an observation, and execution checks it again. ``plan.to_dict()`` is the document that
``xeda run --dry-run --json`` prints. Give a plan to ``ws.run(plan)`` to run exactly this plan.
Launch options (``rebuild_all=True``, ``clean=True``, ``outputs_to=...``) belong to the plan: make
the plan with them. An option given to ``ws.run(plan, ...)`` raises ``SpecError`` with the code
``plan_options``. If the plan no longer matches the spec or the workspace, ``ws.run`` raises
``StalePlanError``, and ``error.difference`` names the first difference. A changed source file is
no difference.

.. code-block:: python

    # plan.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    ws = Workspace("xeda_run")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})

    plan = ws.plan(spec)
    for name, node in plan.nodes.items():
        print(name, node.state, node.reason, node.run_dir)
    print(ws.run(plan).outcome)

Reuse and rebuild
=================

Xeda runs are make-like. A node whose run directory holds a passed or rejected run of the same
identity, with unchanged sources, is fresh. A launch reuses it and starts no tool. A change to a
setting, a design parameter, a source or the inherited environment makes the node stale. A
``failed`` or ``canceled`` run keeps no trace, so the next launch runs it again.

.. code-block:: python

    # reuse.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    ws = Workspace("xeda_run")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})

    first = ws.run(spec)
    again = ws.run(spec)
    print(again.nodes["vivado_synth"].disposition)     # reused
    forced = ws.run(spec, rebuild_all=True)            # run every node, producers included
    print(forced.nodes["vivado_synth"].disposition)    # ran
    clean = ws.run(spec, clean=True)                   # empty each run directory first

**A common trap.** With the default layout, the run directory is
``<run root>/<design>[/<target>]/<flow>``. A second configuration of the same flow and design
replaces the first run in that directory. A record that you kept from the first run then raises
``RunReplacedError`` when you open it. Create the workspace with ``hashed_run_dirs=True`` when you
compare configurations: the directory then ends with the first 16 characters of the identity of
the node, and two configurations never share one. A sweep over variants needs it. See
:doc:`run-directories`.

``prune_variants=True`` removes, for every node of the plan (producers too), the other run
directories of the same flow, design and target before the run, after ``confirm`` says yes. ``post_cleanup=True`` removes the files that only the
tool needed. ``scrub`` removes the run directories of a design (see `Past runs`_).

Find the files
==============

A record holds the location of its run (``run_root`` and ``run_dir``) as absolute directories, and
every file of the run by its name relative to the run directory. A later run of the same
configuration replaces the directory. So open the record to get absolute paths that nobody can
replace while you read them:

.. code-block:: python

    # files.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})
    record = Workspace("xeda_run").run(spec).record

    with record.open() as run:
        print(sorted(run.artifacts)[:3])                 # files for people, by key
        print(run.artifacts["log"].read_text()[:80])     # the log of the main tool
        print(sorted(run.outputs))                       # files that another flow can read

* An output is a file that another flow can read. ``run.outputs`` maps each output name to a tuple
  of paths. The tuple is empty for an optional output that the run did not write.
  ``run.output(name)`` returns the one file of an output.
* An artifact is a file for people: a log, a report, a waveform. No flow reads an artifact. The
  key ``log`` is the log of the main tool of the flow, and ``waveform`` is the waveform when the
  flow wrote one.
* Inside the ``with`` block, no launch can replace these files. If the run was replaced before you
  opened it, ``open`` raises ``RunReplacedError``. A path that you take out of the block is a plain
  ``Path`` and promises nothing more.

In a chain, a file belongs to the record of the node that wrote it: read the netlist from
``result.nodes["yosys_fpga"].record``. To keep files, ask the launch to deliver them to a folder
that you own: ``ws.run(spec, outputs_to="results/sqrt")``. It does not replace a file that someone
changed, unless you also pass ``overwrite_outputs=True`` or ``confirm`` answers yes.

Metrics
-------

``record.metrics`` is a nested read-only mapping. A metric has a dotted path, for example ``fmax``
or ``sim.ended_by``. A unit is never part of the name: it is in
``record.metric_info[path].unit``. ``record.metric(path)`` raises ``KeyError`` when the run did
not report the metric.

Find a flow, its settings and its metrics
-----------------------------------------

``xeda.introspect`` describes the built-in flows as plain JSON data. Nothing runs. The command line
has the same documents: ``xeda list-flows``, ``xeda list-settings FLOW`` and
``xeda list-results FLOW``.

.. code-block:: python

    # introspect.py
    from xeda.introspect import flow_info, flows_info, results_info, settings_info

    names = [flow["name"] for flow in flows_info()]
    info = flow_info("vivado_synth")
    print(info["category"], [output["name"] for output in info["outputs"]][:3])
    fields = settings_info("vivado_synth")["fields"]
    print([field["name"] for field in fields][:5])
    metrics = results_info("vivado_synth")["metrics"]
    print([metric["path"] for metric in metrics][:5])
    with_fmax = [
        name for name in names
        if any(m["path"] == "fmax" for m in results_info(name)["metrics"])
    ]
    print(with_fmax)

A flow name that does not exist raises ``FlowNotFoundError``, and ``suggestions`` has the close
names.

Sweeps
======

``ws.sweep(specs)`` runs many specs. It plans every point first, so a mistake in any point raises
before any point runs. Points that need the same node share it: three seeds on one netlist run one
synthesis. ``sweep.rows(metrics=...)`` gives one row for each point, in input order. A metric has
the address ``<node>.<metric path>``, and that text is its column name. A sweep over variants needs
``hashed_run_dirs=True``: without it, two configurations of one node would use one run
directory, and the sweep raises ``colliding_run_dirs`` before any point runs.

.. code-block:: python

    # sweep.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    ws = Workspace("xeda_run", hashed_run_dirs=True)
    specs = [
        Spec(
            "vivado_synth",
            design.with_updates({"rtl.parameters.G_IN_WIDTH": width}),
            settings={"vivado_synth.clock.period": "5 ns"},
        )
        for width in (16, 24, 32)
    ]
    sweep = ws.sweep(specs)
    print(sweep.counts())
    for row in sweep.rows(metrics=("vivado_synth.fmax", "vivado_synth.lut")):
        print(row["design.rtl.parameters.G_IN_WIDTH"], row["outcome"], row["vivado_synth.lut"])

* A point that is rejected, failed or canceled is still a point, with its result and its row.
  A metric cell is ``None`` when its node has no record in that point or the record has no such
  metric. A mistyped metric name gives a column of ``None``, not an error.
* The first point whose launch ran a shared node reports ``ran``. The other points report
  ``shared``.
* Xeda refuses two equal specs (``equal_specs``), two configurations on one run directory
  (``colliding_run_dirs``), and two points that deliver to one destination
  (``shared_destination``).
* The columns come only from the spec: vary a design with ``with_updates``.
* ``sweep.to_pandas(metrics=...)`` returns a pandas ``DataFrame``. Xeda does not install pandas:
  without it, the call raises ``SpecError`` with the code ``missing_dependency``.
* ``max_workers`` above 1 starts worker processes with the ``spawn`` method. A script that does
  this starts it under ``if __name__ == "__main__":``.
* Use ``post_cleanup=True`` to keep only the records, artifacts and outputs of the nodes that the
  sweep ran.

Past runs
=========

A run root is a folder that Xeda owns. The same ``Workspace`` reads it without running a flow.

.. code-block:: python

    # records.py
    from xeda import Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    ws = Workspace("xeda_run")
    ws.run(Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"}))

    listing = ws.records(design_name="sqrt", flow="vivado_synth")
    for record in listing.records:
        print(record.run_dir.name, record.outcome, record.metrics.get("fmax"))
    for problem in listing.problems:
        print(problem.code, problem.path)                # a record that Xeda could not read
    same = ws.load_record(listing.records[0].run_dir)           # the record of one run directory

``ws.records()`` does not raise for a damaged record. It puts the problem in ``listing.problems``
and reads the others. ``print(listing)`` shows the number of records and one line for each problem.
A node that a failed producer blocks makes no run directory, so a listing that you filter to that
flow has no record and no problem. Read the listing without the filter to find the node that
failed. ``ws.load_record`` raises ``RecordError`` with the same codes. The records of failed
and rejected runs are in the listing.

``ws.scrub(design_name="sqrt")`` removes the run directories of a design, after the ``confirm``
function of the workspace answers yes. A workspace without ``confirm`` removes nothing.

Cancel and watch a launch
=========================

Pass a ``CancelToken`` to stop a launch. ``CancelToken(timeout=seconds)`` cancels itself after that
many seconds, counted from the construction of the token, and ``token.cancel()`` cancels from any
thread. The launch stops the process group of the running tool. Its outcome is ``canceled``, and
the nodes that had not started are ``skipped``. Create one token for each launch.

``on_event`` receives an ``Event`` for each step of a launch, in the thread that called ``run``.
``event.kind`` is ``launch_started``, ``node_started``, ``node_finished`` or ``launch_finished``.
In a sweep, the events of different points interleave: use ``event.launch_id`` to tell them apart.

.. code-block:: python

    # events.py
    from xeda import CancelToken, Design, Spec, Workspace

    design = Design.load("examples/vhdl/sqrt/sqrt.yaml")
    spec = Spec("vivado_synth", design, settings={"vivado_synth.clock.period": "5 ns"})
    ws = Workspace("xeda_run")

    events = []
    ws.run(spec, on_event=events.append)
    print([event.kind for event in events])

    token = CancelToken(timeout=600)
    token.cancel()
    print(ws.run(spec, rebuild_all=True, cancel=token).outcome)    # canceled

What a call does to your process
================================

A call of the API is a library call. It does not change the state of your program.

* **Current directory.** A call reads it only to resolve a relative path that you pass, once, at the
  call. A path inside a design file or a project file is relative to the folder of that file. No
  call changes it. Tools run in their run directories.
* **Environment.** A launch reads your environment once, at its start. No call changes
  ``os.environ``. A tool inherits the environment. It is no part of the identity of a node, but a
  digest of it is freshness evidence: a changed variable makes the node run again. To give a
  variable to a flow, and to put it in the identity and the record, use the ``env`` setting. Its
  values are text.
* **Files.** A launch writes in the run root, and the copies you ask for (``outputs_to``, a
  deliverable setting). Nothing else.
* **Output.** The output of a tool goes to the log files of the run directory. With
  ``Workspace(tool_output="terminal")`` it also goes to the standard output and error of your
  process. Apart from that, no call prints, calls ``warnings.warn`` or reads the standard input.
* **Logging.** The ``xeda`` logger has only a handler that writes nothing. Configure ``logging``
  yourself to see messages. Each log record of a launch has the attributes ``xeda_launch_id`` and
  ``xeda_node``.
* **Input.** Xeda never prompts. A question (replace a file, prune, scrub) goes to the ``confirm``
  function of the workspace. Without it, the answer is no.
* **Threads.** No call installs a signal handler. Every call blocks and is thread-safe. Several
  threads, workspaces and processes can use one run root: Xeda locks each run directory.
* **Leases.** A ``with record.open()`` block holds a shared lease. A launch, a sweep or a scrub
  inside the block raises ``LeaseHeldError`` before it plans. Finish the block first.

Projects and private settings
=============================

A project file (``xedaproject.yaml``) groups designs and the settings that they share. It is a file
of declarations that you check in. ``Project.load(path)`` reads it, and ``project.design(name)``
returns a design that carries the project settings. A project runs nothing: a ``Workspace`` does.

.. code-block:: python

    # project.py
    from xeda import Project, Spec, Workspace

    project = Project.load("examples/vhdl/xedaproject.yaml")
    print(project.designs, project.run_root)       # ('full-adder',) .../examples/vhdl/xeda_run
    design = project.design("full-adder")           # it carries the flows table of the project
    ws = Workspace(project.run_root)
    print(ws.plan(Spec("vivado_synth", design)).roots)

The library never searches for a project file and never reads the private files by itself. The
command line reads two private files, the user file and the project-local file. A script reads
them only when it calls ``xeda.local_settings(project)``, and passes the result to the workspace:
``Workspace(local_settings=xeda.local_settings(project))``. The run root that results is the one
that the command line would use. A script that wants none of this calls nothing. The files and the
order of the layers are in :doc:`flows`.

If a design comes from ``project.design(...)``, the workspace has no ``run_root``, and the project
run root is not ``xeda_run``, the call raises ``SpecError`` with the code ``run_root_mismatch``.
This catches the script that forgot to pass the run root of the project.

UNSET, None and a value
=======================

``xeda.UNSET`` means "the caller gave no value". It is different from ``None``, which can have a
meaning of its own. For ``target`` in ``project.design``, ``UNSET`` selects the default target of
the project, ``None`` selects no target, and a text selects that target. For ``target`` in
``ws.records`` and ``ws.scrub``, ``UNSET`` selects any target, and ``None`` selects the runs
without a target. ``Design.load(path, target=None)`` has no project, so there ``None`` is the
default. The launch options of ``Workspace.run`` default to ``UNSET``: for a ``Spec`` it means the
default of ``plan``, and for a ``Plan`` any given option is an error. The type of ``UNSET`` is
``xeda.Unset``.

Exceptions
==========

A launch that starts returns a ``Result``. These exceptions can come out of a call:

* the Xeda errors, all subclasses of ``XedaError`` with a stable ``code`` (see :doc:`api/errors`
  and :doc:`machine-readable`);
* ``KeyError`` from a lookup (``metrics``, ``metric``, ``outputs``, ``artifacts``, ``nodes``);
* ``TypeError`` for an argument of the wrong Python type;
* the exception that an assignment to a read-only object raises;
* ``KeyboardInterrupt``, after cleanup;
* an exception that your ``on_event`` callback raises. It cancels the launch and propagates.
