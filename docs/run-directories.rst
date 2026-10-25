******************************
Run directories and results
******************************

Every run gets its own directory. The directory holds the settings Xeda used, the results it parsed
and everything the tools wrote. This makes a run reproducible and a failure easy to diagnose
afterward.

Where it goes
=============

Every run directory lies in the run root. The default run root is ``./xeda_run``. Change it with
``--run-root``, the ``XEDA_RUN_ROOT`` environment variable or the API's ``run_root``. The first time
Xeda uses a run root, it creates the directory (or takes an existing empty one) and marks it with
three files. The ``.xeda-run-root`` file is the marker. The ``.gitignore`` file holds ``*`` and the
``CACHEDIR.TAG`` file is the standard cache-directory tag, so version control and backup tools leave
the tree alone. Everything under a marked run root belongs to Xeda. Keep nothing of yours there (see
`Xeda's space and yours`_).

A directory that is named as the run root, holds files and has no marker is refused before anything
runs. The error names the directory and the fix: move it aside, or create ``<dir>/.xeda-run-root``
to hand the directory to Xeda.

The one exception depends on the name and the location of the directory, not on who made it. A
directory named ``xeda_run`` directly in the start directory is adopted and marked, whatever put
files there. Xeda does not check who created it.

.. warning::

   Keep nothing of yours in a directory named ``xeda_run`` in the directory where you run Xeda.
   When Xeda adopts that directory, everything in it becomes Xeda's, as everywhere else under a
   run root.

Marking adds each of the three files that is not there yet. An existing file with one of those
names, such as a ``.gitignore`` of your own, stays as it is. The exception looks at the resolved
path only, not at the option that produced it. An explicit ``--run-root`` (or ``XEDA_RUN_ROOT``, or
the API's ``run_root``) that resolves to ``xeda_run`` directly under the start directory is adopted
in the same way as the default. Any other name is not, and neither is ``xeda_run`` in any other
place.

Inside the run root, the layout depends on ``--hashed-run-dirs``:

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Options
     - Path
   * - *(default)*
     - ``<design>/<flow>/``
   * - ``--hashed-run-dirs``
     - ``<design>/<flow>_<16-char run hash>/``

A design selected as a target (``targets.<name>`` in its design file) has one more level, a
directory named for the target: ``<design>/<target>/<flow>/``, or
``<design>/<target>/<flow>_<16-char run hash>/`` with ``--hashed-run-dirs``. Select a target with
``--target``. A run without ``--target`` has the layout of the table above, whether the design has
no target, an empty ``targets`` table, one target or several targets. The target name is not part of
the run hash. Two targets that yield the same design therefore have the same directory names in two
places. Each one is built once and stays up to date without the other running it again. A run
without a target (``<design>/<flow>``) is not a target's run. A launch for a target never reads or
removes it.

The run directory of a producer is a sibling of the run directory of its consumer. It has the same
layout and the same target, and it is never nested under the consumer. With ``--hashed-run-dirs``,
each run directory is named by the first 16 characters of the run's ``flowrun_hash``. Two variants
of one flow, such as a design's own run and a producer's run, or two candidates of a design-space
exploration, then exist side by side and do not overwrite each other.

The run hash covers the flow's input settings. For a flow that declares inputs, it also covers where
each input comes from: the producing flow's own run hash and output, or the design's sources. A
consumer with unchanged settings therefore gets another directory when its producer's settings
change, or when another producer or output feeds it. Editing a source file never moves a directory.
The run hash reads no file, and a source counts only as the origin of an input. The trace notices a
changed file (see `trace.json`_).

A run directory is a directory named ``<flow>`` or ``<flow>_<16-char run hash>``. It can also be a
symbolic link, but only a link that leads to such a directory of the same flow in the same parent
directory. For example, ``<design>/<flow>`` can lead to ``<design>/<flow>_<hash>``. Xeda follows the
whole chain of links and judges where it ends. A link that leads anywhere else is neither used for a
run nor scrubbed. This includes a link to a target's directory, to the run directory of another
flow, to a directory below or above, to a place outside the run root, to a file, or to nowhere. A
launch refuses such a link before it writes anything, and the error names the link and what it leads
to. ``xeda scrub`` skips it (see `Cleaning up`_). One rule decides both.

No name between the run root and a run directory may be a symbolic link. This holds for the design
directory (``<run root>/<design>``), for the target directory, and for ``@<host>`` and the
directories below it in the mirror of a remote run (see :doc:`remote`). Xeda neither follows nor
removes such a link, whether it leads into the run root or out of it. A launch (``--dry-run``
included) refuses it before it writes anything. ``xeda scrub`` refuses a design directory or a
named ``--target`` that is a link. When it finds a link at a target, it skips the link and says
which one. Every error names the link and what it leads to. A run root that is itself a link works:
Xeda resolves the run root first, so you can keep it on another disk.

By default, Xeda reuses the directory of a flow from run to run. This helps while you iterate,
because incremental tool state (a synthesis checkpoint, a compiled library) survives between runs.
Use ``--clean`` for a run that must start from nothing (see `Rebuilding only what changed`_).

What is in it
=============

A run directory of ``vivado_synth`` looks like this, for example:

.. code-block:: text

    xeda_run/sqrt/vivado_synth/
      settings.json          the run's input settings, and what the flow made of them
      results.json           what Xeda parsed out of the reports
      trace.json             what the run consumed and produced (see below)
      vivado_synth.tcl       generated tool script
      clock.xdc              generated constraints
      vivado.log             the tool's own log
      reports/               reports the tool produced
      outputs/               netlists, bitstreams and other deliverables
      checkpoints/           tool checkpoints, where the flow writes them

``settings.json``
-----------------

The ``flow_settings`` key holds the run's input: the layers merged key by key, from the flow's
defaults, the project's and the design file's ``flows.<flow>`` sections (the selected target's
included) and the ``-s`` items of the command line. Xeda computes the identity of the run from these
settings. A deliverable that has a location counts as the name the run writes under (see `Outputs
where you name them`_). You can feed the settings back to reproduce the run.

The ``effective_flow_settings`` key holds what the flow made of the input while it prepared and ran:
resolved paths, derived options and generated outputs. It shows the difference between what you
asked for and what Xeda asked of the tool. Read it first when a run did something you did not
expect. The file also records the design as Xeda resolved it.

The file records ``design_hash``, ``flowrun_hash``, ``settings_hash`` and ``xeda_version`` too. The
hashes identify the run, and the trace (see below) checks against them. The version is diagnostic
information and is not part of the identity. Both ``design_hash`` and ``flowrun_hash`` depend on
what the inputs mean, not on where they are:

* The design hash covers each source's content hash, its type and its position in the source order.
  It also covers the RTL and testbench metadata that affects behavior. Each source also counts by
  its path relative to the design root, outside the root too (``../lib/defs.vh``), because a tool
  can find other files from the location of a source. A source that a converter flow generated
  (the Verilog of ``bsc``) counts by its name within the run directory of the converter, never by
  a path from the design root, so the hash does not depend on where the run root is.
* The design hash covers the parts of the design that the flow reads. These are the RTL, and the
  testbench for a flow that reads it (a simulation, ``bsc`` and ``vivado_project``). A synthesis
  flow therefore stays up to date when only a testbench changes, and its trace records no testbench
  file as an input. A flow can also be judged on the source types it takes, as ``nextpnr`` is. A
  consumer runs again when what it takes from a producer changes, not whenever the producer runs.
* A parameter whose value is a path under the design root, such as a file given relative to it,
  counts relative to the root. A path outside the root counts as the location it names. The content
  of a parameter file is not hashed.
* ``flowrun_hash`` covers the settings, with each path counted as its text relative to the design
  (``$DESIGN_ROOT/c.xdc``). It also covers where each declared input of the flow comes from, in
  order: the producing flow's own ``flowrun_hash`` and output, or the design's sources. The settings
  of a producer therefore count for its consumers. ``settings_hash`` covers the settings alone.
  ``results.json`` records it too.

Moving a whole design keeps both hashes, unless a parameter names a file outside the design.
Starting Xeda from another directory keeps them too. The hashes never read a file that a setting
names. The trace decides whether the content of such a file changed since the last run (see
`trace.json`_).

How you asked for an input is not part of either hash. This applies to a chain, to a
``flows.<flow>.inputs`` binding in a design, a project, the command line or the API. Only the
producer and the output it selects count. A chain, a saved binding and the defaults that choose the
same producer and output give the same hashes and the same directories. This holds for a link that
passes through flows, too: ``yosys_fpga+fpga_pack`` and ``fpga_pack`` alone give the same hashes. A
chain that selects another producer or another output gives the consumer another ``flowrun_hash``,
and with ``--hashed-run-dirs`` another directory. Without hashed directories, the consumer keeps its
directory and the trace gives the reason for the new run: ``declared input bindings changed``. See
:ref:`flow-chains`.

``results.json``
----------------

This file holds what the flow parsed out of the tool's reports. A run that fails after it has
started still writes the file, as a failure document. This covers a failing tool and a failing
producer. The ``success`` key is ``false``, and ``error.code`` and ``error.message`` say what
failed. The ``code`` is a stable text, never a class name. The run's identity is in the document
too. A flow whose reports or checks failed without a tool error and without a rejection says
``reported_failure``. A flow that rejected its results says ``rejected``. A consumer of a failed
producer says ``producer_failed`` and quotes the cause. Xeda removes the previous ``results.json``
before a run starts, so an earlier success never stands for a run that failed. Every flow reports these keys:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Key
     - Meaning
   * - ``success``
     - Whether the flow succeeded. This decides the process exit status.
   * - ``runtime``
     - Wall-clock duration of the run, in fractional seconds.
   * - ``tools``
     - The executables invoked, with the versions Xeda detected.
   * - ``artifacts``
     - Files the flow produced, by name.
   * - ``design``, ``flow``
     - Names of what was run.
   * - ``design_hash``, ``flow_hash``, ``settings_hash``
     - Hashes that identify the design, the run (its settings and where its declared inputs come
       from) and the settings alone.
   * - ``run_path``
     - Absolute path of this directory.
   * - ``timestamp``
     - When the run finished, as ``YYYY-MM-DD-HHMMSS``.

A flow with declared outputs also records ``outputs``. This key holds the absolute path and the
content digest of each enabled output (an ordered list for a list output). A consumer receives these
checked records under a verified read lease (see :doc:`machine-readable`). A failed output check
uses ``missing_output`` and the normal failure document. ``nextpnr`` checks its enabled configuration
earlier and fails with ``reported_failure`` if the tool did not write the file.

Each flow also reports keys of its own. ``xeda list-results <flow>`` lists them. Keys that start
with ``_`` are internal and may change without notice.

Reading results from a script
=============================

``xeda run --json`` writes the whole run as one JSON document to stdout. Tool output and log
messages go to stderr. You do not need to look for files::

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --json | jq '.results.fmax'

The exit status is not zero when the flow fails. The JSON document then has an ``error`` object
beside any parsed results. See :doc:`machine-readable`.

Rebuilding only what changed
=============================

``xeda run`` is make-like by default. It runs a flow again only when something the flow consumed or
produced changed since its last successful run. ``--rebuild-all`` runs every flow, and so does
``--clean``. A flow that runs logs why (``Running <flow>: <reason>``). A flow that is up to date
logs that, and Xeda shows its recorded results as if it had just run.

What makes a flow stale
------------------------

A flow is stale, and runs again, in any of these cases:

- There is no completed previous run: none yet, or the last one failed without a rejection by its
  checks (a tool error, a crash, a kill, a missing report). A failure that no check rejected keeps
  no trace.
- The flow always runs. It programs a device, draws a new random seed, reads pin constraints
  fetched from a URL, or has a class without a source file (see `Flows that always run`_).
- Its settings changed (the reason names which ones), or a path that a setting names now points
  elsewhere, from another start directory or for another design tree.
- Xeda changed: its version, or any file of the installed package (code, templates, bundled data).
  An editable install keeps one version string across edits.
- The code of a flow defined outside Xeda changed (see `The code of a flow outside Xeda`_).
- A program the flow started changed (the path, size or mtime of an executable, or the ID of a
  container image), or was replaced while the run was going on.
- What a producer handed over changed. This means the files of the outputs that the flow takes from
  the producer, not the producer's run. A producer that ran again with the same outputs leaves the
  consumer up to date, and the log says so.
- An input was added, removed, changed or is missing. This includes a file in a directory that a
  setting names, such as a library recompiled in place. It also includes an input that was modified
  while the run that read it was going on, because what the run read is then unknown.
- An output was deleted or edited, by hand or by another tool, after the run that made it. In a run
  directory that Xeda manages, every file the run left there is an output, whether or not it is an
  artifact. So is the file that a symbolic link there points to. The consumers of that output run
  again only if what they take from it changed.
- A file appeared in the run directory after the run (``new file in the run directory``). The
  directory belongs to Xeda alone, and a consumer that reads it could find the file.
- The design's metadata changed: ``top``, parameters, defines and other fields that affect behavior.
  A change in the place of the files is not a change in the metadata.

``xeda run --dry-run`` applies these rules and runs nothing. It reports each planned flow as
``fresh`` (the launch reuses it), ``stale: <reason>`` (the launch runs it, for the reason that the
log gives), ``unknown: <reason>``, ``rejected again: <message>`` (the launch reports the recorded
rejection again and does not run the flow, see `A run that its checks rejected`_) or
``blocked: <reason>`` (a producer is rejected again or blocked, so the flow does not run). A flow
is unknown when the answer needs something that only a launch makes:

- The flow needs the output of a producer that would run first. The launch judges the flow by the
  outputs that the run hands over, and the reason is ``depends on <producer>``.
- Xeda would have to look up a container image.
- A prepared input, such as a chip database, does not exist yet.
- A launch would refuse the settings in the context of the design, or could not hand over a recorded
  output of a producer.

The dry run takes no lock, does not refresh a trace and starts no process. With ``--rebuild-all`` or
``--clean``, every flow is stale once its prepared inputs pass. The reason is ``every flow runs
(--rebuild-all)`` or ``every flow runs (--clean)``, which a launch records too. A flow that always
runs keeps its own reason (``it draws a new random seed``).

A run directory is reused, so a report that the previous run left is still there when the flow runs
again. A flow reads only the reports that its own run wrote. Right before the run, Xeda notes what
the run directory holds: the device, inode, size and times of each file. A report that is unchanged
since then, or that lies under a directory Xeda could not read, counts as missing (``not written by
this run``). The file itself decides this, never a clock. A tool that fails before it writes its
reports therefore never passes on the previous run's reports.

Xeda brings the producers of a flow up to date before it judges the flow, so a stale producer runs
first. A launch has one run per flow and enters each run directory once.

``trace.json``
---------------

Xeda writes the trace last and atomically, only after a run passed or its checks rejected it. A
failure that no check rejected keeps no trace. Xeda removes the trace before the next run in that
directory executes. The presence of the trace alone therefore means that the last run here
completed. Its ``verdict`` is ``passed`` or ``rejected``.
The trace records:

- The identity of the run: ``flowrun_hash`` (the settings and where the declared inputs come from),
  ``settings_hash`` (the settings alone) and the design hash.
- The Xeda version, a digest of every file of the installed Xeda package, and a digest of the code
  of the flow when the flow is defined outside Xeda.
- The programs the run started, each as a file record (below) of the resolved executable: size,
  mtime, inode change time, inode and content hash. A program that was replaced during the run is
  caught like any other input. A program whose record from the previous run still vouches for it
  (see below) is not read again. A container image is recorded by its ID only.
- The inputs of the run, recorded as the run found them when it started:

  - the files that the parts of the design which the flow reads name (``rtl`` and, for a flow that
    reads it, ``tb``): the sources, and a parameter or define given as a file, such as ``$readmemh``
    data;
  - every existing file that a path-typed setting names. A relative path is looked up under the
    design root and under the start directory. Nested settings count too, such as the GHDL plugin
    setting inside the Yosys settings;
  - every entry under a directory that a path-typed setting names, such as a compiled library in
    ``lib_paths``, the ``include_dirs`` of Verilator or a PDK directory. Xeda lists the directory
    recursively and follows symbolic links. It breaks cycles by device and inode, and does not enter
    again a place that a link leads back to. A library that is reached only through a link is
    therefore tracked like any other. The listing leaves out the directories of version control
    (``.git``, ``.hg`` and ``.svn``). It also leaves out the working locations of the flow
    (``reports``, ``outputs``, ``checkpoints`` and similar), and the
    run directory and run root where a named directory holds them;
  - every file that a flow resolves before it runs, such as the ``abc_script`` of Yosys, expanded
    against the start directory or the environment;
  - the design sources that its declared inputs take.

- The implicit inputs, which are known only after the run. These are the files that a tool reported
  through a depfile (``yosys -E``) and the files that the flow registers as read, such as a chip
  database wherever it was found.
- The outputs: every entry in the run directory after the run, recursively, and the artifacts
  outside the run directory. A consumer may read any entry of the run directory by path. For
  example, ``vivado_power`` reads the routed checkpoint of ``vivado_synth``. Each kind of entry has
  its own record:

  - A regular file has a file record.
  - A directory is recorded by its metadata alone, with the fixed digest ``"directory"``. Xeda does
    not descend into it for its own record, because its entries have records of their own.
  - A FIFO, socket or device is recorded by its metadata and a ``"special:<kind>"`` digest. Xeda
    never reads it.
  - A symbolic link is recorded by its target text and, when it names a file, the content of that
    file. Xeda does not walk a directory that a link points to, because the listing of a run
    directory never follows a link.

  Only the names that Xeda reserves are left out (see `Locking`_). Xeda removes the reports that a
  flow read while it parsed its results (``Flow.reports_read``) from the run directory only when the
  next run turns out not to be up to date, right before it executes. It does not remove them right
  after the trace that recorded them. The outputs of a run that is found up to date, reports
  included, stay. This way a tool that fails before it writes its report can never leave the report
  of a previous run to be read as if it were new.
- A file record for each file above: ``(size, mtime, inode change time, inode, content hash)``.
- Where each path-typed setting points, by its name (such as ``lib_paths[0][1]``). An absolute path
  is recorded as it is, after Xeda has expanded ``$PWD``, ``$DESIGN_ROOT`` and ``$DESIGN_DIR``. A
  relative path is recorded as each existing path that it is found at, under the design root and
  under the start directory. The identity of a run counts ``$PWD/libs`` as written, so a launch from
  another start directory, or for another design tree with a design file that reads the same, has
  the same identity. A setting that names a directory, such as a compiled library, is bound by
  nothing else. If it points elsewhere, the run is stale (``lib_paths[0][1]`` now names ``<B>/libs``
  (was ``<A>/libs``)).
- The ordered bindings of the declared inputs: the name, the origin (source, producer or none), the
  identity of the producer and the consumed paths. A changed binding stops Xeda from reusing the
  run, even if the set of files is the same. The files that a producer handed over are not file
  records of the consumer. The trace compares the path and the digest of each handed file with the
  current record of the producer.
- A ``run_id`` for the run, and the ``run_id`` of each producer run that it consumed, keyed by the
  run directory of that producer.

Xeda records the inputs before the run starts. An edit made during a long run therefore counts: the
file no longer matches its record, and the next launch runs again. Xeda decides whether a file
changed during the run by comparing it with its record from before the run: the size, mtime, inode
change time and inode. It judges a program the same way, against its state when Xeda started it.

A file that Xeda first knows after the run, such as one that a depfile names for the first time, has
no record from before the run. If the file is on the file system of the run directory, Xeda compares
it with the clock of that file system, and the file counts as changed if it was written after the
run started. If the file is on another file system, no clock can tell. Its record is then unknown,
so the next launch runs once more and says so. An unknown record never matches.

Location tells Xeda a run's own file from an edit of yours. The run directory always lies under the
run root, and it belongs to the run alone. Every entry in it is an output of the run, whether the
run wrote it or not. A file that appears there after the last run makes the run stale. Outside the
run directory, a file that a setting, a depfile or the design names stays an input. If such a file
was not there before the run, or changed during it, its record is unknown, and the next launch runs
again.

Every file under a directory that a setting names is an input, and there is no limit on how many. A
limit would let a change past it go unnoticed. Xeda checks each file by its metadata at every launch
and hashes it only when the metadata cannot vouch for it. A listing of more than 10,000 files, or
one that takes more than 2 seconds, is logged as a warning that names the setting.

Xeda reads times from the clock of the run directory's file system, by touching a marker file there,
not from the clock of the computer. It compares them with the times of files. If the run directory
is read only, the check still works and only skips saving what it learned.

A file counts as unchanged when its size, mtime, inode change time (``ctime``) and inode match its
record, and the record was taken more than 2 seconds after the file last changed (this rules out a
racy timestamp). Otherwise the content hash decides. A bare ``touch``, a ``chmod``, a copy that
keeps the mtime (``cp -p``), and checking out a branch and back therefore cost a content hash and do
not cause a new run. An edit that is given back its old mtime (``touch -r``, restoring a backup) is
still noticed, because no one can set the inode change time of a file back. Xeda hashes the outputs
of a run once, after the run, and later checks follow the same rule. A check that had to hash a file
because it changed too recently to trust records the file again once that window has passed, so
later checks do not read it again.

``--clean``
------------

``--clean`` empties the run directory of each flow before the flow runs, and runs every flow. It is
"make clean, then make". Xeda refuses several other names, and the message says what to use:
``--no-incremental``, the per-flow ``clean`` setting, Verilator's ``clean_before_run`` and the
launcher setting ``cleanup_before_run``. GHDL's ``clean`` is refused too. Its message names
``--clean`` and ``clean_before_analyze``, which runs ``ghdl remove`` before the analysis.

Flows that always run
----------------------

Some runs can never be reused. These flows always run, keep no trace and report the reason
(``Running <flow>: <reason>``, and ``nodes[].reason`` with ``--json``):

- ``openfpgaloader`` has the reason "it programs a device", which changes the outside world. The
  stages that build its bitstream (``yosys_fpga``, ``nextpnr`` and ``fpga_pack``) are ordinary
  nodes, and Xeda reuses them when nothing changed.
- A flow whose class has no source file on disk, such as one defined in a notebook cell, has the
  reason "its class ... has no source file on disk ..., so Xeda cannot tell whether its code
  changed".
- A flow that is asked for a fresh random seed has the reason "it draws a new random seed". Every
  seed is a setting with a fixed default, so equal inputs give equal outputs. The random
  initialization of Verilator is off unless ``random_init`` is set, and then it uses ``random_seed``
  (default 1). The ``cocotb.random_seed`` setting also defaults to 1. To get a new seed on each run,
  set ``random_seed: random`` for Verilator with ``random_init``, ``cocotb.random_seed: random`` for
  cocotb, or ``randomize_seed: true`` for ``nextpnr`` (off by default).
- ``nextpnr`` with pin constraints fetched from a URL has the reason "its constraints are fetched
  from a URL", because no trace can verify a file on the network. Xeda fetches the file into guarded
  scratch space in the run directory, and only when the flow executes. Xeda tracks local typed
  constraint sources and local board fallbacks before it judges whether the flow is up to date. It
  writes the board files that the package bundles in an archive to the content-keyed directory
  ``.cache/board-files/`` of the run root.

The run root also holds what several designs share. For a Xilinx 7-series part, ``nextpnr``
generates the chip database of the die once, into ``.cache/xilinx-chipdb/<identity>/``. An
``xc7a100t`` takes about a minute and 3.5 GB of memory. Every later launch under that run root, for
any design, reads the same file. Xeda registers the file as an input before it judges whether the
flow is up to date, so a launch that changes nothing starts nothing. The identity is the content of
the generator, the executables and the device data of the installed toolchain, never a version
string. On POSIX, one lock per identity keeps two launches from generating the database twice. An
interrupted generation leaves only scratch files, which the next one removes. ``--clean``,
post-cleanup and ``xeda scrub`` leave the cache alone.

A design whose sources a generator writes (``rtl.generator``) keeps its record in the run root too,
in ``.cache/generators/``. The content of what the generator reads identifies an entry: its
``sources`` (which can be directories outside the design) and its selected direct executable. The
entry holds the digest of every source that the last generation left. The generator therefore runs
again only when one of its inputs changed. Indirect tools and the packages that the generator uses
stay outside this identity. On POSIX, a lock on the existing design-root directory serializes the
generations for the same tree, including the first one and generations with different identities. It
creates no marker beside the design and does not make the run root early. Separate design roots that
write to one external destination are not coordinated. Windows keeps its existing behavior of no
interprocess lock.

A generation happens while Xeda loads the design, before any flow or run directory exists. This is
why the record lives in the run root and not beside the design. It is also why a design that is
loaded with no run root generates every time. ``--rebuild-all`` and ``--clean`` regenerate, as they
run every flow again. See :ref:`generators`.

A run that its checks rejected
------------------------------

Xeda reports a failure again, and does not run the flow, only when all of these are true:

- The tools of the run ended with a normal exit status.
- Xeda read the reports of the run.
- The declared outputs of the run are valid.
- The checks of the flow rejected the results.
- The flow does not always run (see `Flows that always run`_).
- Nothing that the flow consumes changed since the run.

Such a run keeps its trace, with the verdict ``rejected``. In ``xeda run --json``, the launch in
which the checks reject the run gives the node the state ``failed``. The next launch, with nothing
changed, logs that the last run with these inputs and settings was rejected, and gives the node
the state ``rejected again``, with the message of the rejection as its ``reason``. A flow that
reads the outputs of this run does not run either. Its state is ``blocked``, and its ``reason`` is
"<producer> did not succeed".

These checks reject a run:

- A simulation (``ghdl_sim``, ``nvc``, ``verilator``, ``modelsim``, ``vcs``, ``bsc_sim``,
  ``yosys_sim``, ``vivado_sim`` and ``vivado_postsynth_sim``): an end record that shows the
  simulation did not end as intended, an event at or above ``fail_severity``, or cocotb results
  that Xeda read and that show failed tests. A missing end record, missing cocotb results and a
  missing waveform are failures, not rejections.
- ``quartus`` and ``ise_synth``: negative slack.
- ``diamond_synth``: negative slack, unrouted nets, timing errors, or a DSP or block RAM that
  ``allow_dsps`` or ``allow_brams`` forbids.
- ``vivado_synth``, ``vivado_alt_synth`` and ``vivado_impl`` with ``fail_timing``: negative hold
  slack (WHS) or hold violations.
- ``nextpnr``: a missed constraint that nextpnr reports while it exits normally.

All other failures are tool failures. A tool failure keeps no trace, and the next launch runs the
flow again. This includes these cases:

- A tool that ends with an error status, a crash, a kill, a timeout, and a report that is missing
  or cannot be read. A simulator that ends with an error status is a tool failure. An example is
  GHDL after a failed ``severity failure`` assertion.
- A negative setup slack in Vivado with ``fail_timing``. The Xeda script of Vivado stops the tool
  with an error, so the flow runs again at every launch.
- A missed constraint in ``nextpnr`` without ``timing_allow_fail``. nextpnr stops with an error
  itself, so the flow runs again at every launch.
- Every failure of ``dc``. Xeda never reports these again.
- Every failure of a flow that always runs. Such a flow keeps no trace.

To run a rejected flow again, change what it consumes, or run with ``--rebuild-all``. Xeda
notices a change only in what it tracks. If a file that the run reads is not tracked (see `What is
not tracked`_), an edit of it goes unnoticed, and Xeda reports the old rejection. A data file that a
testbench reads is the common case: declare it as a source of the design.

The code of a flow outside Xeda
-------------------------------

A flow that a plugin or your own code defines is stale when its code changed. Xeda lists the files
and checks each one at every launch, also in a process that lives long, such as a notebook, so an
edit, an added file and a removed file count at once. The check is by the metadata of a file, as for
the inputs of a run: Xeda reads a file again only when its size, times or inode changed, or when it
changed just before the last check. It judges "just before" by the clock of this machine. A code
tree on a network file system whose clock is several seconds behind this machine's can miss two
same-size edits of one file made within one timestamp tick of that file system. A digest names
each file relative to the root of the code, so moving the code to another place does not make the
run stale. The runs of such a flow that an older Xeda made are stale once, because the digest is
made in a new way. The root of the code is:

- For a flow defined in a package, the top-level package: the directory with the ``__init__.py``
  files from the module of the flow up. Every file in it counts: helper modules, templates and
  data. Hidden files, compiled bytecode and run roots (directories with the ``.xeda-run-root``
  marker) do not count.
- For a flow defined in a module outside a package, the module and the modules that it imports from
  its own directory, with the ``templates`` directory next to it. The ``import`` statements in the
  source tell which modules (a relative import with one dot too), and so on for each module found.
  A package of that directory counts as a whole. Other files next to the module do not count. A
  directory without an ``__init__.py`` is no package for this rule: a module in it counts when the
  code imports it, and the other files of the directory do not. A module that the code imports
  with ``importlib.import_module`` is not found: put the code in a package.

A flow class that has no source file on disk cannot be checked this way. An example is a class
defined in a notebook cell or in the interactive interpreter. Such a flow always runs (see `Flows
that always run`_), and Xeda computes no code digest for it. To reuse its runs, define the class in
a ``.py`` file.

What is not tracked
---------------------

Each of these can make a stale result look up to date. ``--rebuild-all`` is the way out:

- Programs that a tool starts indirectly. Examples are a compiler that ``make`` runs (the model
  build of Verilator) and Python packages that a simulator imports, such as cocotb. Xeda records
  only the programs that it starts itself.
- Environment variables. A flow that reads one without declaring it as a setting can change behavior
  in a way the trace cannot see. So can a design generator. The environment of a generator is not
  part of its identity on purpose, so that one record can be reused from another shell.
- Files that a tool finds on its own and does not report, such as a Vivado IP repository, tool data
  outside any setting, and anything that no setting names and no depfile lists.
- What a symbolic link in a run directory points to, when that is a directory. Xeda records the link
  by its target and does not record the contents of the directory.

Xeda cannot verify pin constraints fetched from a URL either, which is why a flow that uses them
always runs (see `Flows that always run`_).

Locking
--------

A durable lock file, ``<run dir>.lock``, lies beside the run directory and never inside it. It
serializes the writers of one directory. This works on POSIX only, and Windows has no lock. A
consumer takes a shared lock on the run directory of each completed producer. It verifies the
completion evidence under the lock it acquired, and holds the lock until its own launch ends,
including the writing of results and trace. This protects every producer, including a producer that
has no trace. If the producer directory changes between the producer's completion and the
acquisition of the shared lock, Xeda refuses the hand-over. Matching bytes of the declared outputs
do not prove that the rest of the completed run is unchanged.

Readers that are compatible overlap. A rebuild, a clean-up, a scrub and the purge of a design-space
exploration take the same lock exclusively and wait for the readers. Inside one process, a lock that
is requested again in the same mode, or exclusive and then shared, keeps the operating system lock.
A request for exclusive access while the process holds the lock shared is refused before anything is
written. ``xeda scrub`` keeps the lock file when it deletes a run directory, so waiting processes
continue to use the same lock identity. Scrub removes sibling run directories before the launch
takes its own run lock. This avoids deadlocks between variants of the settings. A failure to take
the lock names the producer, its path, the shared mode and the error of the operating system.

Xeda reserves these names at the top of a run directory and never counts them as outputs of a run:
``trace.json``, ``trace.json.tmp`` (a trace that is being written) and ``.xeda-time-*`` (a marker
that Xeda touches to read the clock of the file system and removes at once). A flow must not write a
file with one of these names.

Remote runs
===========

A remote run (``--remote HOST``) runs the request on a Linux host over SSH. The run directories
are on the remote host, below the run root of a workspace that stays between runs. The reuse of
earlier runs works there as it does locally. Xeda mirrors the results, the logs, the reports and
the declared outputs under ``<run root>/@<host>/``, with the same layout as a local run root
(``<design>[/<target>]/<flow>[_<hash>]``). The mirror holds no ``trace.json``, so a local run never
reuses it. A mirror directory is replaced by each fetch. ``--hashed-run-dirs``, ``--clean``,
``--rebuild-all`` and ``--dry-run`` act on the remote run directories, as they do for a local run.
``--scrub`` is refused: a remote launch cannot ask you to confirm it.
``--post-cleanup`` and ``--post-cleanup-purge`` are refused, because the run directories are not on
your machine: ``xeda remote clean`` frees the space on the host. See :doc:`remote`.

Cleaning up
===========

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Option
     - Effect
   * - ``--clean``
     - Empty the run directory of every flow before it runs, and run every flow (implies
       ``--rebuild-all``).
   * - ``--post-cleanup``
     - After the run, keep only ``settings.json``, ``results.json``, the artifacts and the declared
       outputs. This includes the producers, but Xeda waits until the requested flow has finished,
       so the flow can still read their files.
   * - ``--post-cleanup-purge``
     - After the run, remove the run directory entirely. It needs no ``--post-cleanup`` beside it.
       Every link that names the run directory beside it (see `Where it goes`_) goes with the
       directory, as with ``xeda scrub``. A file that is delivered to a location you named is
       moved out of the directory, not copied (see `Outputs where you name them`_).
   * - ``--scrub``
     - Before the run, remove the other run directories of the flow for this design. Xeda asks
       for confirmation. If the launch also has to ask whether to replace a file that you named
       for an output, it asks that first. A launch that you decline removes nothing. Xeda
       refuses ``--scrub`` with ``--remote``, because a remote launch cannot ask you.

Pruning (``--post-cleanup`` and ``--post-cleanup-purge``) removes the trace first. A pruned run is
therefore not reused, even though its ``settings.json`` and ``results.json`` remain.

``xeda scrub <flow> <design_name>`` removes the previous run directories of one flow for one design.
It runs nothing. It removes the directories directly under ``<design_name>/`` (made without a
target) and those under every target directory below it. Xeda
finds them on disk, so it also finds a target that the design file no longer names, and it reads no
design file. With ``--target NAME``, Xeda searches only ``<design_name>/NAME/``, and leaves the runs
without a target and the runs of every other target alone. Scrub lists the directories to remove
first and asks you to confirm once. It removes the runs that it listed, which are the runs that you
confirmed. It takes the lock of each directory first, so it never removes a directory while a launch
runs in it. It waits until the launch ends. Then it looks at what is at the listed path:

* If the run records of the directory (its ``results.json`` and ``trace.json``) are not what the
  listing saw, scrub keeps the directory and says so. A launch wrote them after the listing: a run
  finished there, or a launch found its run up to date and refreshed its trace. What is there is no
  longer what you confirmed.
* If nothing is at the path any more, because another scrub or a purge removed it, scrub says so and
  goes on. This is what you asked for, so it is not an error.
* If the path is no longer a run directory of the flow, scrub stops with an error and leaves it
  alone. This happens when a directory became a file, and when a link now leads anywhere but to a
  run directory of the flow beside it. It also happens when a link was retargeted while scrub
  waited. Scrub holds the lock of the directory that the link led to, and does not remove another
  directory.
* Otherwise scrub removes the directory.

The summary line says how many directories scrub removed, kept, and found gone already. With
``--json``, the document lists them (see :doc:`machine-readable`).

A run directory that is a link counts only when it leads to a run directory of the same flow beside
it (see `Where it goes`_). Scrub lists such a directory once, even if it has several names, and
shows each name before it asks. A name is the directory itself, or a link to it, named ``<flow>`` or
``<flow>_<hash>``. A link with any other name is not a name of a run directory. Scrub does not list
it, and it stays. Under the lock of the directory, scrub removes the links first and then the
directory, so no link is left that leads nowhere. If one of the names no longer leads to the
directory by then, scrub stops with an error and removes nothing, even if a run also ended in the
directory.

A link that is named like a run directory of the flow and leads anywhere else is not listed. Scrub
removes neither the link nor what it leads to, and it does not wait for the lock of the place it
leads to. Before it asks, it says so with one line for each link:

.. code-block:: text

    skipped xeda_run/sqrt/vivado_synth_0123456789abcdef: it is a link to
    xeda_run/sqrt/a, which is not a directory named vivado_synth or vivado_synth_<hash> in
    xeda_run/sqrt

Scrub writes each message on one line, with full paths. The end of the message depends on the
reason. For example, a link to a place that does not exist ends with ``which does not exist``. Scrub
never searches the run directories of another flow, and never follows a link that leads out of the
run root, or one at the design or target level.

The lock does not protect a run that was complete when scrub listed it. If another launch is about
to read that run, scrub removes it. For example, a consumer has run its producer but has not yet
taken its read lease on the directory of the producer. The consumer then fails with a
``FlowDependencyFailure`` (``changed before acquiring its read lease``) and does not read a
directory that is being removed. This is the intended behavior: the launch fails and reads nothing.
Do not scrub a flow whose runs other launches are starting to use.

``--scrub`` on a launch removes the other run directories of the flow in the launch's own directory.
It never reaches the directories of another target.

Only the command line asks for the confirmation. The Python functions ``scrub_design`` and
``scrub_runs``, and a ``DefaultRunner`` launch with ``scrub_old_runs``, never prompt. They ask the
callback that you give them (the ``confirm`` argument, or the ``confirm_scrub`` attribute of the
launcher). The callback gets the listed run directories and returns whether to remove them. Without
a callback, nothing is removed, and Xeda logs a warning that names the run directories. This holds
when stdin is closed, in a notebook or a CI job.

Xeda's space and yours
-----------------------

Everything under a run root is Xeda's. Keep nothing of yours in ``xeda_run/``, or wherever
``--run-root`` points. Xeda decides each run directory once per flow, when it launches the flow.
Every deletion that a flow or the launcher makes inside a run directory goes through one checked
operation (``RunDirectory``), which deletes only inside the directory. This covers ``--clean``, the
clean-ups above, ``xeda scrub``, and what a flow removes before it runs its tool (a previous work
library, depfiles, stale reports, a previous implementation directory). The operation refuses a path
through a symbolic link that leads out of the directory, and the error names the link. It removes a
link as itself and never follows it.

A tool may leave symbolic links in its run directory. A link at the name of a place where a flow
works (the build directory of a simulator, for example) is removed as a link before the next run if
it leads out of the directory. Xeda never delivers a link to a directory outside the run directory.
The same holds for every file that Xeda itself writes where a flow runs (``settings.json``,
``results.json``, a rendered template). Xeda writes the whole file and then renames it into place,
so an interrupted write leaves the earlier file. It never writes through a link at the name of the
file.

A few writes of Xeda do not go through this operation, because nothing of yours lies under their
names. These are ``trace.json``, the marker that Xeda touches to read the clock of the run
directory's file system (Xeda writes and removes both directly), and the environment file of a
dockerized tool (``.<tool>_docker.env``, written by ``Tool.execute`` right before the container
starts).

The log of a tool is the one write that Xeda does not make whole first, because you can read the log
while the tool runs. This covers ``sim.log``, every other log that a flow copies the output of a
tool to, and the file that receives the output of a tool (for example, the ``<flow>_stdout.log`` of
Vivado). Before the tool starts, Xeda makes the log as a new file and renames it onto the name of
the log. Then it writes each line as the line arrives. A symbolic link or a hard link that a tool
left at that name is replaced and never written through. A log that cannot be made starts no tool. A
tool that fails or runs out of time leaves the part of its log that it wrote. Each run replaces the
log, so follow it with ``tail -F``, because ``tail -f`` keeps following the file it opened.

A tool is free to replace a project or a directory by its own name inside the run directory.
Examples are ``create_project -force`` of Vivado, ``project_new -overwrite`` of Quartus,
``write_icc2_files -force`` of DC, and the new project of Diamond and ISE. The whole directory
belongs to Xeda, so nothing of yours is there to lose.

Nothing outside the run root changes except what you name. An output setting that you give a
location, or ``--outputs-to``, makes Xeda copy or move the output there (see `Outputs where you name
them`_). This is the one deliberate exception. It never deletes anything of yours. Xeda writes the
destination, and replaces an existing file only with your approval.

A dockerized tool runs in a container. The container mounts the run directory of the flow, which
is writable, and these directories, which are read-only:

- the design root and the directories of the RTL and testbench sources;
- the directory of each input file of the flow: the files that the producers handed over and the
  files that the flow itself registered as inputs, such as the chip database of nextpnr;
- the directory of each existing file that a setting names (for example ``custom_boards_file`` or a
  platform file), and each existing directory that a setting names (for example a library path).
  These are the same files and directories that the trace records as inputs. A setting that names
  what the flow writes, such as a working location or a deliverable, mounts nothing.

A dockerized flow therefore reads the files of its producers and the files that its settings name
without a ``Docker.mounts`` entry. A directory is not mounted again when another mount holds it at
the same path.

Two more exceptions exist, and you configure both explicitly. Xeda does not decide them on its own:

- The ``clone_dir`` or ``local_cache`` of a Git dependency. Without them, the clone goes to the
  ``.dependencies`` directory of the run root.
- A writable entry of ``Docker.mounts`` that you add yourself.

In both cases Xeda writes there as you told it and guards nothing about what it finds. That
directory is yours to manage.

Outputs where you name them
============================

The tools of a flow write only inside its run directory. For each deliverable setting, they write a
fixed conventional name. The convention is ``outputs/<design>.<ext>``. A setting without one keeps
its own default name. The value of such a setting is a location when it is an absolute path after
Xeda has expanded its variables: a path under ``$PWD``, ``$DESIGN_ROOT`` or ``$DESIGN_DIR``, or an
absolute path. A bare name stays in the run directory.

When you give such a setting a location, Xeda still writes the copy of the run under the
conventional name in the run directory. What the tools do, and what identifies the run, never depend
on where you asked for the copy. Once the whole launch has finished, Xeda delivers the file: it
copies the file to the location you named. ``--outputs-to DIR`` works the same way for the artifacts
of the requested flow, each at its path relative to the run directory. Renaming or moving the
destination of a delivery therefore never runs the flow again and never changes what the tools do.
It changes only where the copy lands.

A run is identified by what it writes: whether it writes the output, and the name it writes the
output under in the run directory. A location is not part of that. A run that passed and is up to
date therefore delivers to a location that you give it later, without running again. If the flow
writes the output in any case, you can give a location, change it or remove it, and the run stays
the same. When the setting is unset, the flow writes the conventional name. ``fpga_pack`` always
writes its bitstream, so ``-s bitstream=$PWD/blinky.bit`` after a plain run only delivers the
file. The same holds for ``verilog_output`` of ``ghdl_synth`` and for ``placement`` of ``nextpnr``
on Xilinx targets.

A setting that switches an output on works in another way. ``vivado_synth`` writes no bitstream
until ``bitstream`` is set. The first location you give it runs the flow again. Later locations do
not. A name without a location (``-s bitstream=board.bit``) is a different file in the run
directory, so it is a different run.

A setting that names a file refuses ``true`` and ``false``, as text or as a boolean, because they
would become a file called ``true``. Give a name or a location, or leave the setting unset. Only a
waveform setting such as ``vcd`` takes ``true`` and ``false`` as a switch.

A requested flow that writes no outputs has nothing to deliver. A programmer is such a flow, as in
``xeda run vivado_synth+openfpgaloader``. ``--outputs-to`` is then refused before anything runs. The
error names the setting whose location delivers the file that the programmer reads, here ``-s
flows.vivado_synth.bitstream=$PWD/<file>``, and it names every form of a location given above. With
``--remote``, that setting would deliver on the remote host, so the error names the request that
brings the file back instead: ``xeda run --remote vivado_synth ... --outputs-to DIR``.

A delivery never replaces a directory. It refuses these destinations:

- a file that any flow of the launch reads, such as an input of a producer or of a later sibling;
- a place inside a directory that a setting of any of those flows reads, such as a library or
  include directory like ``lib_paths``. Every file under such a directory is an input, so a
  destination there is refused, also when no file is there yet. ``--outputs-to`` that names such a
  directory is refused up front;
- a place inside any run root. A bare name would otherwise put an output back into its own run
  directory.

Two outputs never go to one destination. When two deliverable settings name the same file, in one
flow or in two flows of the launch, Xeda refuses the launch before any tool runs, and the message
names both settings. ``--outputs-to`` follows the same rule. Before the run, Xeda expects to deliver
the files that the last run of the requested flow made, and it refuses the launch if one of them
would land on a destination that another output names. A file that only this run reveals is compared
after the requested flow has run, before Xeda copies anything.

Xeda compares names as the file system compares them. ``Out.v`` and ``out.v`` are one destination in
a directory that ignores letter case (the default on macOS and Windows). A destination inside
another (``x`` and ``x/y``) is refused too. If two names still reach one file, Xeda finds out when
it delivers the second one. It says that two settings name the file, and the first delivery stays.

Xeda replaces an existing file at the destination without asking only when that file is its own
earlier delivery and is unchanged. Its digest and inode must still match what Xeda wrote. Xeda
tracks this in a delivery record that it keeps beside the run directory, in the run root. The record
survives ``--clean`` and scrubbing, so losing the run directory never makes Xeda overwrite a file
that it should ask about first. Anything else needs ``--overwrite-outputs`` or a yes that you type
at a terminal prompt. With ``--json``, or when there is no terminal on both ends, only the option
works.

Xeda checks all of this before any tool of the launch runs. The requested flow checks the deliveries
of every flow of the plan when the launch starts. It reports every refusal first. For example, a
file of the design, or a file that a setting reads, may lie in the run directory of the flow that
reads it. Then it asks its questions, its own last. No refusal or question therefore comes after
minutes of tool time. All of them come before ``--scrub`` removes anything, and while no other
launch or scrub of the run directory has to wait.

A yes holds for the file you were asked about. If that file changes before the launch reaches the
flow that delivers it, Xeda asks again. If it changes while Xeda waits for your answer, Xeda does
not replace it. Xeda checks once more right before the copy, in case something changed in between.
It never replaces a destination that changed since its last check, whether you confirmed it or not.
A run that is up to date (the trace found it current, so no tool ran) delivers its outputs too,
if it passed. A run that its checks rejected delivers nothing, on the first launch or when it
is reported again.

With ``--post-cleanup-purge``, Xeda deletes the run directory right after the delivery. Xeda then
moves a delivered file out of the run directory and does not copy it, so a large output does not
need a second copy of disk space. Xeda renames the file to a temporary name beside the destination.
Right before it renames the file into place, it makes the same checks as for a copy, and it checks
the digest of the file. If any of this fails, Xeda puts the file back where the run left it. When
both places are on one file system, a move is a rename. Across file systems, and wherever a rename
is refused, Xeda copies the file as usual. Xeda also copies in these cases, because a move would be
wrong or the run directory stays:

* The file is a symbolic link, or has another hard link. The delivered file would be a link that
  dangles after the purge, or would share its content with the other name.
* The file is in a directory outside the run directory, which a link inside the run directory leads
  to. A move would remove a file outside the run directory.
* Two outputs reach the same file, for example a setting and ``--outputs-to``, or a file and a link
  to it. Xeda copies the file for each of them. This holds for the whole launch. When an output of
  one flow is a link to a file that another flow delivers, both are copies.
* The flow was found up to date. Its run directory is not deleted, and its trace still names the
  output there.

Xeda records a moved file in the delivery record like a copy, and the next launch replaces it
without asking.

To confirm that a delivered file is Xeda's own, Xeda would have to read it, which costs a pass over
the file. This matters for an output of a gigabyte. Xeda therefore reads the file only when the
metadata of its record cannot vouch for it. This is the rule that every other file Xeda tracks
follows: the size, mtime, inode change time and inode must match the record, outside the racy
window. A check that does read the file first reads the clock of the file system of the destination.
It makes and removes a short-lived marker file (``.xeda-time-...``) in the directory of the
destination, where delivery writes its temporary file anyway. It records that time with the file. A
later check of an unchanged delivery then recognizes it by its size, mtime, inode change time and
inode alone, and reads nothing.

The timestamps cannot vouch for a file that Xeda has just delivered. The first check after the file
has settled (more than two seconds after it was written) reads it once, and that read anchors the
record. No later check or copy reads it again. A launch that is still inside those two seconds
anchors nothing, so each check and the copy read the destination. Where no marker can be made,
because the destination directory is read only, every check reads the content, as it would without a
clock to trust. If a destination is on another device than the one its record's clock was read on,
Xeda reads it once more and anchors it again with the clock of the file system it is on now.

Every working location is a bare name inside the run directory. A working location is a setting that
names where a flow keeps its intermediate files, such as ``sim_dir``, ``bobj_dir``, ``impl_folder``
or a log path. It is never a location in the sense above: Xeda always uses the value as a name
there, whatever the value looks like. ``$PWD``, ``$DESIGN_ROOT`` and ``$DESIGN_DIR`` still expand
inside a nested setting given as a mapping or an instance, such as ``cocotb.results_xml`` or
``yosys_sim.cxxrtl.filename``. The expansion happens once, when Xeda builds the settings of the flow
or when a field is assigned, not later.

``xeda dse`` delivers nothing, because an exploration runs many variants and they would deliver to
one path. Xeda refuses a deliverable setting that is given as a location, or ``outputs_to``, before
the search starts.

``xeda run --remote`` delivers ``--outputs-to`` only after a run that succeeded, and only from its
local mirror, once the results of the host are there. The mirror has the layout of the run
directories of the host, so ``--hashed-run-dirs`` decides whether its directories carry the hash.
Xeda refuses a deliverable that is given as a location before it connects, because the flow runs on
the remote host and would write the location there, on the remote file system. Use ``--outputs-to``:
it delivers from the artifacts that Xeda fetched into the local mirror. For a remote run,
``--outputs-to`` may never land on or in anything that the requested flow reads. Xeda takes this
from the settings that the launch uses (the ``flows.<flow>`` section with ``-s`` over it), plus
whatever the section of every other flow names as written. The local side cannot know which
producers the remote will launch, so it protects those paths conservatively, even where no flow of
the run ends up reading them.

``bsc_sim`` delivers a Bluesim executable as the generated script without its ``.so``. Bluesim
generates the two together, and the script finds its ``.so`` relative to itself. A Bluesim
executable therefore runs from the run directory that it was copied out of, not from an arbitrary
delivery destination.

Xeda does not confine everything. The code of the design writes wherever it says: for example, a
testbench that does ``$fopen`` on an absolute path, or a BSV ``openFile`` under the ``fdir`` of bsc
when you point ``fdir`` at your own tree (by default it is the run directory, so it is confined like
everything else). Xeda confines what its settings and flows write. It cannot confine what the design
that you asked it to run does on its own.
