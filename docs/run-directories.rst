******************************
Run directories and results
******************************

Every flow execution gets its own directory. Everything Xeda decided and everything the tools
produced is there, which makes a run reproducible and a failure diagnosable after the fact.

Where it goes
=============

Every run directory lies in the **run root**, ``./xeda_run`` by default; change it with
``--run-root`` or the ``XEDA_RUN_ROOT`` environment variable (the API's ``run_root``). The first
time Xeda uses a run root, it creates it (or takes an existing empty directory) and marks it: a
``.xeda-run-root`` file, a ``.gitignore`` of ``*``, and a ``CACHEDIR.TAG``, so that version control
and backup tools leave the whole tree alone. Everything under a marked run root is Xeda's -- keep
nothing of yours there; see `xeda's space and yours`_.

A directory named as the run root that already holds files and carries no marker is refused,
naming it and the fix, before anything runs: move it aside, or create ``<dir>/.xeda-run-root`` to
hand it to Xeda. The one exception is by *name and location*, not by history: a directory named
``xeda_run`` directly in the start directory is taken as Xeda's run root and marked, whatever put
files there -- Xeda does not check who created it, only where it is and what it is called. **Keep
nothing of your own in a directory named** ``xeda_run`` **beside where you run Xeda from**;
anything already there when it is adopted becomes Xeda's from then on, the same as everywhere else
under a run root. Marking adds a ``.xeda-run-root`` file, a ``.gitignore`` of ``*`` and a
``CACHEDIR.TAG`` under whichever of those three names is not already present; an existing file by
one of those names (a ``.gitignore`` of your own, say) is left exactly as it is. The exception
follows the *resolved path* alone, not which option produced it: an explicit ``--run-root`` (or
``XEDA_RUN_ROOT``, or the API's ``run_root``) that also resolves to ``xeda_run`` directly under the
start directory is adopted the same way as the default; naming anything else -- another name, or
``xeda_run`` somewhere other than directly under the start directory -- gets no exception. A
leftover ``xeda_run_dse`` or ``xeda_run_<optimizer>`` left by an older Xeda (from before ``xeda
dse`` shared this same default) is not named ``xeda_run``, so it gets no exception either: delete
it, or create its own ``.xeda-run-root``.

Within the run root, the layout depends on ``--hashed-run-dirs``:

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Options
     - Path
   * - *(default)*
     - ``<design>/<flow>/``
   * - ``--hashed-run-dirs``
     - ``<design>/<flow>_<16-char run hash>/``

A design selected as a *target* (``targets.<name>`` in its design file, selected with
``--target`` or, when it has only one, by itself) lies one level deeper, in a directory of the
target's own: ``<design>/<target>/<flow>/`` (``<design>/<target>/<flow>_<16-char run hash>/`` with
``--hashed-run-dirs``). A design without a target, or with an empty ``targets`` table, keeps
``<design>/<flow>/``. The target's name is no part of the run hash, so two targets that yield the
same design have the same flow directory names in two places: each is built once, and each stays
up to date without the other re-running it. A run made before targets existed
(``<design>/<flow>``) is not taken for a target's, and a target's launch never reads or removes it.

A dependency gets its own directory, a **sibling** of the flow that launched it, in the same
layout, within the same target -- never nested under it. ``--hashed-run-dirs`` names each by its run hash (the first 16
characters of its ``flowrun_hash``), so two variants of one flow (a design's own run and a
dependency's, or two DSE candidates) coexist instead of overwriting each other. The run hash
covers the flow's input settings and, for a flow that declares inputs, where each of them comes
from: the producing flow's own run hash and output, or the design's sources. So a consumer whose
settings did not change gets another directory when its producer's settings do, or when another
producer or output feeds it. Editing a source file never moves a directory: the run hash reads no
file, and a source counts only as the origin of an input (the trace, not the directory name,
notices a changed file). There used to be a third layer,
``<design>_<design_hash>/``, dropped when ``--incremental`` was off; it is gone (delete any such
directories by hand -- Xeda no longer looks for them).

Xeda reuses a flow's directory across runs by default, which is what you want while iterating:
incremental tool state (a synthesis checkpoint, a compiled library) survives between runs. Use
``--clean`` for a run that must start from nothing; see `Rebuilding only what changed`_.

What is in it
=============

.. code-block:: text

    xeda_run/sqrt/vivado_synth/
      settings.json          the run's input settings, and what the flow made of them
      results.json           what was parsed back out of the reports
      trace.json             what the run consumed and produced -- see below
      vivado_synth.tcl       generated tool script
      clock.xdc              generated constraints
      vivado.log             the tool's own log
      reports/               reports the tool produced
      outputs/               netlists, bitstreams, and other deliverables
      checkpoints/           tool checkpoints, where the flow writes them

``settings.json``
-----------------

``flow_settings`` holds the run's *input*: the flow's defaults, the project's and design file's
``flows.<flow>`` sections and the command-line ``-s`` overrides, merged key by key. It is exactly
what the run is identified by, so it can be fed back to reproduce the run.
``effective_flow_settings`` holds what the flow made of them while preparing and executing the run
(resolved paths, derived options and generated outputs) -- the difference between what you asked
for and what was asked of the tool, and the first thing to read when a run did something you did
not expect. It also records the design as Xeda resolved it.

It records ``design_hash``, ``flowrun_hash`` and ``xeda_version`` too. The two hashes identify the
run (and are what the trace, described below, checks against); the version is diagnostic metadata
and is not part of run identity. Both hashes
depend on what the inputs mean, not on where anything is: the design hash covers each source's
content hash, type, ``standard`` and ``variant``, and its position in the source order, plus
behavior-affecting RTL/testbench metadata. Every source also counts by its path relative to the
design root -- outside the root too, as ``../lib/defs.vh`` -- since a tool can resolve other files
by the source's location. The design hash counts the parts of the design the flow reads: its RTL, and
the testbench for a flow that reads one (a simulation, and ``bsc`` and ``vivado_project``, which read
the testbench's sources too), so a synthesis flow stays up to date when only a testbench changes, and its trace records no testbench file as an input. (A flow that
consumes another flow's outputs still runs again when that flow does.) A parameter whose value is a path
under the design root, such as one given as a file relative to it, counts relative to it; one
outside the root counts as the location it names, and a parameter file's content is not hashed.
``flowrun_hash`` covers settings, with a path counted as its text relative to the design
(``$DESIGN_ROOT/c.xdc``), and where each declared input of the flow comes from, in order: the
producing flow's own ``flowrun_hash`` and output, or the design's sources. A producer's settings
therefore count for the flows that consume it. ``settings_hash``, beside it in ``results.json``,
covers the settings alone. Moving a whole design anywhere keeps both hashes (unless a parameter
names a file outside it); starting Xeda from another directory does too. The hashes never read a
file a setting names: whether its content changed since the last run is the trace's business
(see ``trace.json`` below).

How an input was asked for -- a chain, a ``flows.<flow>.inputs`` binding in a design, a project, on
the command line or through the API -- is not part of either hash; only what it selects is. A
chain, a saved binding and the defaults that choose the same producer and output give the same
hashes and the same directories, and a chain that selects another producer or another output gives
the consumer another ``flowrun_hash`` (with ``--hashed-run-dirs``, another directory). Without
hashed directories the consumer keeps its directory, and the trace says why it ran again:
``declared input bindings changed``. See :ref:`flow-chains`.

``results.json``
----------------

What the flow parsed out of the tool's reports. A run that fails after it has started (a failing
tool, or a failing dependency) still writes it, as a failure document: ``success`` is ``false``, and
``error.type`` and ``error.message`` say what failed, beside the run's identity (a flow whose
reports or checks failed without a tool error says ``ReportedFailure``, and a flow downstream of a
failed one ``FlowDependencyFailure``, quoting it). The previous
``results.json`` is removed before a run starts, so an earlier success never stands for a run that
failed. Every flow reports:

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
     - Hashes identifying the design, the run (its settings and where its declared inputs come
       from) and the settings alone.
   * - ``run_path``
     - Absolute path of this directory.
   * - ``timestamp``
     - When the run finished, as ``YYYY-MM-DD-HHMMSS``.

Flows with declared outputs also record ``outputs``: each enabled output's absolute path and
content digest (an ordered list for list outputs). Consumers receive those checked records under
a verified read lease; see :doc:`machine-readable`. Output-record validation failures use
``MissingOutput`` and the normal failure document; nextpnr checks its enabled configuration
earlier and raises ``FlowFatalError`` if the tool did not write it.

On top of that each flow reports its own keys - ``xeda list-results <flow>`` lists them. Keys
beginning with ``_`` are internal detail and may change without notice.

Reading results from a script
=============================

``xeda run --json`` writes the whole run as one JSON document to stdout, with tool output and log
messages on stderr, so no file-hunting is needed::

    xeda run vivado_synth sqrt.yaml --json | jq '.results.Fmax'

The exit status is non-zero when the flow fails. The JSON document then carries an ``error``
object alongside any parsed results. See :doc:`machine-readable`.

Rebuilding only what changed
=============================

``xeda run`` is make-like *by default*: it re-runs a flow only when something it consumed or
produced changed since its last successful run; ``--rebuild-all`` runs every flow
unconditionally, and so does ``--clean``. A flow that runs logs why (``Running <flow>: <reason>``); a flow left alone logs
that it is up to date, and its previously recorded results are shown as if it had just run.

What makes a flow stale
------------------------

- there is no successful previous run (none yet, or the last one failed);
- the flow always runs: it programs a device, draws a new random seed, or reads pin constraints
  fetched from a URL -- see `Flows that always run`_;
- its settings changed -- the reason names which ones -- or a path a setting names now points
  elsewhere (from another start directory, or for another design tree);
- Xeda changed: its version, or any file of the installed package (code, templates, bundled data
  -- an editable install keeps one version string across edits); for a flow defined outside Xeda,
  its own modules and templates;
- a program it started (an executable's path, size or mtime; a container image's ID) changed, or
  was replaced while the run was going on;
- a dependency ran again -- a dependency that re-ran always re-runs what depends on it, even if
  nothing it produced actually changed for this consumer;
- an input was added, removed, changed, or has gone missing -- a file in a directory a setting
  names too (a library recompiled in place) -- or was modified while the run that read it was
  going on, so that what the run read is unknown;
- an output was deleted or edited (by hand, or by another tool) since the run that produced it --
  in a run directory Xeda manages, that is any file the run left there, declared as an artifact
  or not, or the file a symbolic link there points to; the flows depending on it then re-run too;
- a file appeared in a run directory Xeda manages since its run ("new file in the run
  directory"): the directory is Xeda's alone, and a depender reading it could find the file;
- the design's metadata changed (``top``, parameters, defines, and other behavior-affecting
  fields -- not merely where files live).

A run directory is reused from run to run, so a report the previous run left is still there when
a flow runs again. A flow reads only the reports its own run wrote: right before the run, Xeda
notes what the run directory holds -- each file's device, inode, size and times -- and a report
found unchanged since then (or under a directory it could not read) counts as missing -- "not
written by this run", decided by the file itself, never by a clock -- so a tool that fails before
it writes its reports never passes on the previous run's.

Dependencies are always brought up to date before the flow that depends on them is judged, so a
stale dependency reruns first. A launch has one run per flow, so it enters each run directory once.

``trace.json``
---------------

Written last, atomically, only after a run succeeds, and removed before the *next* run of that
directory executes -- so its presence alone means "the last run here completed and succeeded". It
records:

- the run's identity (``flowrun_hash``: its settings and where its declared inputs come from),
  the hash of its settings alone (``settings_hash``) and the design hash;
- the Xeda version, a digest of every file of the installed Xeda package, and one of the flow's
  own modules when it is defined outside Xeda;
- the programs it started, each as a file record (below) of the resolved executable -- size,
  mtime, inode change time, inode and content hash -- so a program replaced during the run is
  caught the same way any other input is; a container image is recorded by its ID alone;
- its **inputs**, recorded *as the run found them when it started*: every file the design's
  ``rtl`` and ``tb`` name (sources, and a parameter or define given as a file, such as
  ``$readmemh`` data), every existing file a path-typed setting names (a relative path under both
  the design root and the start directory; nested settings too, such as the ghdl plugin's inside
  yosys's), every entry under a directory a path-typed setting names (a compiled library in
  ``lib_paths``, Verilator's ``include_dirs``, a PDK directory: recursively, a symbolic link
  followed -- cycles broken by device and inode, and a link that leads back into a place already
  pruned is not re-entered -- so a library reached only through a link is tracked like any other;
  version control's ``.git``, ``.hg`` and ``.svn`` left out; the flow's own working locations --
  ``reports``, ``outputs``, ``checkpoints``, OpenROAD's ``results`` and the like -- excepted, as is
  the run directory and run root themselves where a named directory holds them), every file a flow
  resolves before it runs (yosys's ``abc_script``, expanded against the start directory or the
  environment), and the dependencies' outputs. A file whose record from the previous run still
  vouches for it (see below) is not read again;
- its **implicit inputs**, known only after the run: files a tool reported reading via a depfile
  (``yosys -E``), and files a flow reads on its own while it runs (a board's pin constraints);
- its **outputs**: every entry in the run directory after the run, recursively -- a depender may
  read any of them by path (``vivado_power`` reads the routed checkpoint of ``vivado_synth``) --
  plus the artifacts outside it. A regular file is a file record (below); a directory is recorded
  by its metadata alone (a fixed ``"directory"`` digest, never descended into for its own record --
  its entries are recorded in their own right); a FIFO, socket or device is recorded by its
  metadata and a ``"special:<kind>"`` digest, never read; a symbolic link is recorded by its
  target text and, when that names a file, that file's content too -- a directory it points to is
  not walked, since the run directory's own listing never follows a link. Only the names Xeda
  reserves are left out (see `Locking`_). The reports a flow read while parsing its results
  (``Flow.reports_read``) are removed from the run directory only when the next run turns out not
  to be fresh, right before it executes -- not right after the trace that recorded them, so a
  fresh run's own outputs, reports included, are left alone -- so a tool that fails before writing
  its own report can never leave a previous run's report to be read as if it were this run's;
- each file above as a **file record**: ``(size, mtime, inode change time, inode, content hash)``;
- where each path-typed setting points, by its name (``lib_paths[0][1]``): an absolute path (a
  ``$PWD`` or ``$DESIGN_ROOT`` path once expanded) as itself, a relative one as each existing
  path it is found at under the design root and the start directory. A run's identity counts
  ``$PWD/libs`` as written, so launched from another start directory -- or for another design
  tree whose design file reads the same -- it has the same identity; a setting that names a
  directory (a compiled library) is bound by nothing else, and pointing elsewhere makes the run
  stale ("``lib_paths[0][1]`` now names ``<B>/libs`` (was ``<A>/libs``)");
- ordered declared-input bindings: name, source/producer/none origin, producer identity and
  consumed paths. A changed binding invalidates reuse even if the file set is unchanged;
- a ``run_id`` for this run, and the ``run_id`` of each dependency's run it consumed, keyed by
  that dependency's run directory.

Recording the inputs before the run is what makes an edit made *during* a long run count: the
file no longer matches the trace, so the next launch runs again. A file that no one could record
before the run (one a depfile names for the first time, a program) and that was written while the
run went on is recorded as unknown, which never matches.

Location is how Xeda tells a run's file from a user's edit. The run directory -- always under the run root -- is the run's alone: every entry in it
is the run's output, whether the run wrote it or not; a file that appears there since the last run
makes the run stale ("new file in the run directory"). Anywhere else, a file named by a setting, a
depfile, or the design stays an input; if it was absent before the run or changed during it (its
mtime, or its inode change time, at or after the run started), its record is unknown and the next
launch re-runs.

Every file under a directory a setting names is an input, with no limit on how many: a limit
would let a change past it go unnoticed. Each is checked by its metadata at every launch and
hashed only when that cannot vouch for it; a listing of more than 10,000 files, or one that
takes more than 2 seconds, is logged as a warning naming the setting.

Times are read from the clock of the run directory's file system -- by touching a marker file
there -- not from the computer's clock, since they are compared with the times of files. A check
that finds a read-only run directory still checks, and only skips saving what it learned.

A file counts as unchanged when its size, mtime, inode change time (``ctime``) and inode match
its record and it did not change within 2 seconds of being recorded (to rule out a racy
timestamp); otherwise its content hash decides. So a bare ``touch``, a ``chmod``, a copy that
keeps the mtime (``cp -p``), or checking out a branch and back, costs a content hash -- not a
re-run -- while an edit given back its old mtime (``touch -r``, restoring a backup) is still
noticed: no one can set a file's inode change time back. A run's outputs are hashed once,
after it, and later checks go by the same rule. A file hashed at a check because it had changed
too recently to be trusted is recorded afresh once that window has passed, so later checks do
not read it again.

``--clean``
------------

Empties each flow's run directory before it runs, and runs every flow -- "make clean, then make".
It replaces the old ``--no-incremental``, the per-flow ``clean`` setting, GHDL's ``clean`` (now
``clean_before_analyze``), Verilator's ``clean_before_run``, and the launcher's
``cleanup_before_run`` setting -- each of those names now fails with what to use instead.

Flows that always run
----------------------

Some runs can never be reused, and say why: they always run, keep no trace, and report the reason
(``Running <flow>: <reason>``, and ``nodes[].reason`` under ``--json``):

- ``openfpgaloader``: "it programs a device" -- that changes the outside world. The stages that
  build its bitstream (``yosys_fpga``, ``nextpnr``, ``fpga_pack``) are ordinary nodes, reused
  when nothing changed.
- A flow asked for a fresh random seed: "it draws a new random seed". Every seed is a setting with
  a fixed default, so equal inputs give equal outputs: Verilator's random initialization (off unless ``random_init`` is set) uses
  ``random_seed`` (default 1) and cocotb's ``random_seed`` defaults to 1. Ask for a new seed per
  run with ``random_seed: random``, or with ``randomize_seed: true`` for ``nextpnr`` (off
  by default).
- ``nextpnr`` when a board's pin constraints are fetched from a URL: "its constraints are fetched
  from a URL" -- no trace can verify a file on the network. It is fetched into guarded scratch
  in the run directory only when the flow executes. Local typed constraint sources and local
  board fallbacks are tracked before freshness is checked. Bundled archive board files are
  materialized under the run root's content-keyed ``.cache/board-files/`` directory.

The run root also holds what several designs share. ``nextpnr`` for a Xilinx 7-series part
generates the die's chip database once, into ``.cache/xilinx-chipdb/<identity>/`` (about a minute
and 3.5 GB of memory for an ``xc7a100t``), and every later launch under that run root -- of any
design -- reads the same file; it is registered as an input before freshness is judged, so an
unchanged relaunch starts nothing. The identity is the content of the installed toolchain's
generator, executables and device data, never a version string. One lock per identity (POSIX)
keeps two launches from generating it twice; an interrupted generation leaves only scratch, which
the next one removes. ``--clean``, post-cleanup and ``xeda scrub`` leave the cache alone.

A design whose sources a generator writes (``rtl.generator``) keeps its record there too, in
``.cache/generators/``: the content of what the generator reads -- its ``sources``, which may be
directories outside the design, and its selected direct executable -- identifies an entry holding
the digest of every source its last generation left, so the generator runs again only when one of
them changed. Indirect tools and dependencies remain outside that identity. A
POSIX lock on the existing design-root directory serializes generations for the same tree,
including bootstrap and differing identities, without creating a marker beside the design or
making the run root early. Separate design roots that write to one external destination are not
coordinated; Windows retains the existing no-interprocess-lock behavior.
A generation happens while the design is *loaded*, before any flow or run directory exists, which
is why its record lives in the run root rather than beside the design, and why a design loaded
with no run root in sight generates every time. ``--rebuild-all`` and ``--clean`` regenerate as
they re-run every flow. See :ref:`generators`.

What is not tracked
---------------------

Each of these can make a stale result look fresh; ``--rebuild-all`` is the escape:

- **Programs started indirectly** -- a compiler run by ``make`` (Verilator's model build), Python
  packages such as cocotb imported by a simulator: only the programs Xeda starts itself are
  recorded.
- **Environment variables.** A flow that reads one without declaring it as a setting can change
  behavior invisibly to the trace, and so can a design generator: a generator's environment is
  deliberately not part of its identity, so that one record is reusable from another shell.
- **Files a tool finds on its own without reporting them** -- a Vivado IP repository, tool data
  outside any setting, anything not named by a setting and not listed in a depfile.
- **What a symbolic link in a run directory points to, when that is a directory** -- the link is
  recorded by its target, the directory's contents are not.
- **Filesystem clock differences across filesystems.** Run start and input snapshot times come
  from the run directory's filesystem. A named input on another filesystem whose clock differs
  may be misclassified as changed during the run (or not changed during it).

Pin constraints fetched from a URL are not verifiable either, which is why a flow using them
always runs (`Flows that always run`_).

Locking
--------

A durable lock file, ``<run dir>.lock`` beside the run directory -- never inside it --
serializes writers to the same directory (POSIX only; there is no lock on Windows). A consumer
holds each completed dependency's lock *shared*, after verifying completion evidence under the
acquired lease, until its own launch ends, including results and trace writing. This protects
every producer, including those without a trace. A change in the gap
between producer completion and shared-lock acquisition refuses hand-over; matching declared
output bytes alone do not prove the rest of the completed run is unchanged.

Compatible readers overlap. Rebuild, cleanup, scrub and DSE purge take the same lock exclusively
and wait for readers. Within one process, same-mode and exclusive-to-shared reentry keep the OS
lock; shared-to-exclusive reentry is refused before writes. ``xeda scrub`` retains the lock file
when deleting a run directory so waiting processes continue to use the same lock identity.
Sibling scrub happens before the current run lock is taken, avoiding deadlocks between settings
variants. Lock acquisition failures name the producer, path, shared mode and OS error.

Xeda reserves these names at the top of a run directory, and never counts them as a run's
outputs: ``trace.json``, ``trace.json.tmp`` (a trace being written), and ``.xeda-time-*`` (a
marker touched to read the file system's clock, removed at once). A flow must not write files by
these names.

A remote host must run the 0.4.4 release line of xeda or newer, with remote protocol 1 or newer,
the first released protocol. It provides canonical resolved settings, relocated read inputs with
their original path identities, declared output records and checked hand-over; the simulation
evidence rule (remote simulations must confirm that the simulation ended successfully); the FPGA
build graph (``fpga_pack``, ``nextpnr`` on Xilinx 7-series, and a programming-only
``openfpgaloader``); the node identity of flow chains (a node's hash is its settings plus where
its declared inputs come from, and the local mirror is named by it, so a remote that computes
another hash cannot take part); the declared Vivado synthesis outputs; and ``yosys``'s declared
netlist, with its ASIC configuration derived from its own ``platform`` and a bundled platform's
files counted relative to xeda's installation. Xeda checks the package the remote interpreter
actually imports before shipping the design. A release without the protocol marker, or with a
lower protocol, is refused with an "upgrade the remote xeda" error.

A remote run (``--remote``) always runs fresh on the remote, and its results are always mirrored
locally in the hashed layout (``<design>[/<target>]/<flow>_<hash>``), so remote runs of different settings
never share a directory. ``--clean`` and ``--hashed-run-dirs`` would change nothing, so each is
refused. ``--rebuild-all`` forces local generator loading before shipping while the remote flow
still runs fresh; remote ``clean`` does not force a local generator on ordinary invocations.

Cleaning up
===========

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Option
     - Effect
   * - ``--clean``
     - Empty every flow's run directory before it runs, and run every flow (implies
       ``--rebuild-all``).
   * - ``--post-cleanup``
     - After a run, keep only ``settings.json``, ``results.json`` and the artifacts (dependencies
       included; deferred until the requested flow has finished, so it can still read their files).
   * - ``--post-cleanup-purge``
     - After a run, remove the run directory entirely.
   * - ``--scrub``
     - Before running, remove the flow's other run directories for this design. Asks for
       confirmation.

Pruning (``--post-cleanup``/``--post-cleanup-purge``) drops the trace first, before anything else
it removes: a pruned run is not reused, even though its ``settings.json``/``results.json`` remain.

``xeda scrub <flow> <design_name>`` removes previous run directories of one flow for one design,
without running anything: the ones directly under ``<design_name>/`` (made before targets existed,
or by a design without one) and those under every target's directory below it. They are found on
disk, so a target the design file no longer names is found too, and no design file is read. With
``--target NAME`` only ``<design_name>/NAME/`` is searched, leaving the pre-target runs and every
other target's. The directories to be removed are listed first and confirmed once. Scrub removes
the runs it listed, which are the runs you confirmed. It takes each directory's lock first, so it
never removes a directory while a launch is running in it: it waits until the launch ends. Then it
looks at what is at the listed path:

* If the run records of the directory (its ``results.json`` and ``trace.json``) are not what the
  listing saw, scrub keeps it and says so. A launch wrote them after the listing: a run finished
  there, or a launch found its run fresh and refreshed its trace. What is there is no longer what
  you confirmed.
* If nothing is at the path any more, because another scrub or a purge removed it, scrub says so
  and goes on. That is what you asked for, so it is no error.
* If the path is no longer a directory, or is now a link that does not lead to a directory beside
  it, scrub stops with an error and leaves it alone. It does the same for a link that was
  retargeted while scrub waited: scrub holds the lock of the directory the link led to, and does
  not remove another directory.
* Otherwise scrub removes it.

The summary line says how many directories scrub removed, kept, and found gone already. With
``--json``, the document lists them (see :doc:`machine-readable`). A run directory that is a link
counts only when it leads to a directory beside it, and scrub removes both. A run directory of another flow is never searched, and a link that leads out of the run
root is never followed.

The lock does not protect a run that was complete when scrub listed it. If another launch is
about to read that run, scrub removes it. For example, a consumer has run its producer but has not
yet taken its read lease on the producer's directory. The consumer then fails with a
``FlowDependencyFailure`` (``changed before acquiring its read lease``) and does not read a
directory that is being removed. This is by design. Do not scrub a flow whose runs other launches
are starting to use.

``--scrub`` on a launch removes the flow's other run directories in the launch's own
directory, so it never reaches another target's.

xeda's space and yours
-----------------------

**Everything under a run root is Xeda's.** Keep nothing of yours in ``xeda_run/`` (or wherever
``--run-root`` points): every run directory in it is Xeda's, decided once per flow when it is
launched, and every deletion a flow or the launcher makes inside one -- ``--clean``, the clean-ups
above, ``xeda scrub``, and what a flow removes before it runs its tool (a previous work library,
dependency files, stale reports, a previous implementation directory) -- goes through one checked
operation (``RunDirectory``) that deletes only inside the directory: a path through a symbolic link
that leads out is refused, naming the link, and a link is removed as itself, never followed. A
tool may leave symbolic links in its run directory: a link at the name of a place a flow works in
(a simulator's build directory, say) that leads out is removed as a link before the next run, and
a link to a directory outside is never delivered. The same goes for every file Xeda itself writes
where a flow runs (``settings.json``, ``results.json``, a rendered template), which is written
whole and then renamed into place, so an interrupted write leaves the earlier file, and never
through a link at its name. A few of Xeda's own writes bypass it directly, because there is nothing of yours under
their names for it to protect: ``trace.json`` and the marker touched to read the run directory's
file-system clock are Xeda's reserved files (the trace and digest modules write and remove them
directly), and a dockerized tool's own environment file (``.<tool>_docker.env``, written by
``Tool.execute`` right before the container starts) is written the same way. A tool's log is the
exception: Xeda does not write it whole first, because you can read it while the tool runs. This covers
``sim.log`` and every other log a flow copies a tool's output to, and the file a tool's output is
sent to (Vivado's ``<flow>_stdout.log``, for example). Before the tool starts, Xeda makes the log
as a new file and renames it onto the log's name. It then writes each line as it arrives. A
symbolic link or a hard link at that name, left by a tool, is replaced and never written through,
and a log that cannot be made starts no tool. A tool that fails or runs out of time leaves the
part of its log that it wrote. Each run replaces the log, so follow it with ``tail -F``: a
``tail -f`` keeps following the file it opened. A tool is free to replace a project or
a directory by its own name inside the run directory (Vivado's ``create_project -force``,
Quartus's ``project_new -overwrite``, DC's ``write_icc2_files -force``, Diamond's and ISE's new
project) -- the whole directory is Xeda's, so there is nothing of yours there to lose.

**Nothing outside the run root changes except what you name.** An output setting you give a
location, or ``--outputs-to``, is copied there (see `Outputs where you name them`_) -- that is the
one deliberate exception, and it never deletes anything: a destination is written and, if it was
already there, replaced only with your say-so. Two further exceptions exist, both something you
configure explicitly rather than something Xeda decides on its own: a git dependency's configured
``clone_dir``/``local_cache`` (otherwise its clone goes in the run root's ``.dependencies``), and
a writable entry of ``Docker.mounts`` you add yourself (a container otherwise gets the design
root and the RTL/testbench source directories mounted read-only; a dependency's run directory is
not mounted at all today, so a dockerized flow needing one of its files needs a
``Docker.mounts`` entry for it -- that is not yet automatic). In both cases Xeda writes there as
told, and guards nothing about what it finds: that directory is yours to manage, not Xeda's.

Outputs where you name them
============================

A flow's tools write only inside its run directory, under a fixed **conventional name** per
deliverable setting (the convention is ``outputs/<design>.<ext>``; a setting without one keeps
its own default name). Give such a setting a location -- an absolute path, or one anchored with
``$PWD``/``$DESIGN_ROOT`` -- and Xeda still writes the run's copy under its conventional name in
the run directory (what the tools do and what the run is identified by never depend on where you
asked for the copy); once the whole launch has finished, that file is **delivered**: copied to the
location you named. The same is true of ``--outputs-to DIR``, which delivers the requested flow's
artifacts, each at its path relative to the run directory. Renaming or moving a delivery's
destination therefore never re-runs the flow, and never changes what the tools do -- only where
the copy lands.

A requested flow that writes no outputs -- a programmer, as in
``xeda run vivado_synth+openfpgaloader`` -- has nothing to deliver. ``--outputs-to`` is then
refused before anything runs. The error names the setting whose location delivers the file the
programmer reads, here ``-s flows.vivado_synth.bitstream=$PWD/<file>``: only an absolute path, or
one under ``$PWD``, is a location, and a bare name stays in the run directory. On a ``--remote``
run, that setting would deliver on the remote host, so the error names the request that brings the
file back instead: ``xeda run --remote vivado_synth ... --outputs-to DIR``.

A delivery never replaces a directory, and never lands on a file any flow of the launch reads (a
dependency's input, a later sibling's), inside a directory a setting of any of them reads (a
library or include directory such as ``lib_paths``: every file under it is an input, so a
destination there is refused -- ``--outputs-to`` naming one up front -- even where no file is yet),
or inside any run root -- a bare name would otherwise put an output back into its own run
directory. An existing file at the destination is replaced without
asking only when it is Xeda's own earlier delivery there, unchanged (its digest and inode still
match what Xeda wrote, tracked in a delivery record kept beside the run directory, in the run
root -- which survives ``--clean`` and scrubbing, so losing the run directory never makes Xeda
overwrite a file it should ask about first); anything else needs ``--overwrite-outputs``, or a yes
typed at a terminal prompt (under ``--json``, or with no terminal on both ends, only the flag
works). This is checked before any tool of the launch runs. The requested flow checks the
deliveries of every flow of the plan when the launch starts: it reports every refusal first, and
then asks its questions, so neither comes after minutes of tool time. A yes holds for the file you
were asked about. If that file changes before the launch reaches the flow that delivers it, Xeda
asks again. If it changes while Xeda waits for your answer, Xeda does not replace it. The check is
made once more right before the copy, in case something changed in between: a destination that
changed since its last check is never replaced, confirmed or not. A **fresh** run (one the trace
found up to date, so no tool ran) delivers its outputs too: delivery follows the run's outcome, not
whether a tool executed.

Reading a delivered file to confirm it is Xeda's own costs a pass over it, which matters for a
gigabyte-sized output, so it is read only when its record's metadata cannot vouch for it -- the
same rule every other file Xeda tracks follows: its size, mtime, inode change time and inode
against its record, outside the racy window.
A check that does read it first reads the clock of the destination's own file system, by making
and removing a short-lived marker file (``.xeda-time-...``) in the destination's directory, where
delivery writes its temporary file anyway, and records that time with the file: a later check of
an unchanged delivery then recognizes it by its size, mtime, inode change time and inode alone
and reads nothing. The record of a file Xeda has just delivered cannot be trusted by its
timestamps, so the first check after it has settled -- more than two seconds after it was
written -- reads it once, and that read anchors the record; no later check or copy reads it
again. A launch still inside those two seconds anchors nothing, so each check and the copy read
the destination, as every launch did before. Where no marker can be made
(a destination directory that is read only), every check reads the content, as it would without
a clock to trust. A destination found on another device than the one its record's clock was read
on is read once more, and anchored afresh by the clock of the file system it is on now.

Every **working** location -- a setting naming where a flow keeps its intermediate files, such as
``sim_dir``, ``bobj_dir``, ``impl_folder`` or a log path -- is a bare name inside the run
directory, never a location: what a working-location setting is given is always used as a name
there, whatever it looks like. ``$PWD`` (and ``$DESIGN_ROOT``) still expand inside a nested
setting given as a mapping or an instance, such as ``cocotb.results_xml`` or
``yosys_sim.cxxrtl.filename`` -- expansion happens once, when the flow's settings are built or a
field of theirs is assigned, not later.

``xeda dse`` delivers nothing: a deliverable setting given as a location, or ``outputs_to``, is
refused before the search starts, since a design-space exploration has no one requested flow to
deliver for. ``xeda run --remote`` delivers ``--outputs-to`` only after a run that succeeded, and
only from its local mirror -- always the hashed layout, ``<design>[/<target>]/<flow>_<hash>/``, whatever
``--hashed-run-dirs`` says for local runs -- once the remote's results have been fetched there; a
deliverable given as a location is refused before anything is shipped, since the remote run's own
identity would otherwise depend on where the local side later copies its output. What a remote
run's ``--outputs-to`` may never land on or in is what the requested flow reads as the launch uses
its settings (its ``flows.<flow>`` section with ``-s`` over it), plus whatever every other flow's
section names as written: the local side cannot know which dependencies the remote will launch,
so it protects those conservatively, even where no flow of the run ends up reading them.
``bsc_sim``'s delivered Bluesim executable is the generated script without its ``.so``: the two
are generated together and the script finds its ``.so`` relative to itself, so a Bluesim
executable is run from the run directory it was copied out of, not from an arbitrary delivery
destination.

**What Xeda does not confine.** The design's own code writes wherever it says: a testbench's
``$fopen`` of an absolute path, or a BSV ``openFile`` under bsc's ``fdir`` when you point ``fdir``
itself at your own tree (by default it is the run directory, and so confined like everything
else). Xeda confines what its settings and flows write; it cannot confine what the design you
asked it to run does on its own.
