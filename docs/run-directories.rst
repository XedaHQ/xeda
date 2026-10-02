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
     - ``<design>/<flow>_<16-char settings hash>/``

A dependency gets its own directory, a **sibling** of the flow that launched it, in the same
layout -- never nested under it. ``--hashed-run-dirs`` names each by its own
settings, so two settings variants of one flow (a design's own run and a dependency's, or two DSE
candidates) coexist instead of overwriting each other; the hash is of the flow's *input settings*
only, so editing the design never moves a dependency's directory. There used to be a third layer,
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
``[flows.<flow>]`` sections and the command-line ``-s`` overrides, merged key by key. It is exactly
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
by the source's location. A parameter whose value is a path
under the design root, such as one given as a file relative to it, counts relative to it; one
outside the root counts as the location it names, and a parameter file's content is not hashed.
``flowrun_hash`` covers settings, with a path counted as its text relative to the design
(``$DESIGN_ROOT/c.xdc``). Moving a whole design anywhere keeps both hashes (unless a parameter
names a file outside it); starting Xeda from another directory does too. The hashes never read a
file a setting names: whether its content changed since the last run is the trace's business
(see ``trace.json`` below).

``results.json``
----------------

What the flow parsed out of the tool's reports. A run that fails after it has started (a failing
tool, or a failing dependency) still writes it, as a failure document: ``success`` is ``false``, and
``error.type`` and ``error.message`` say what failed, beside the run's identity. The previous
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
   * - ``design_hash``, ``flow_hash``
     - Hashes identifying the design and the effective settings.
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

    xeda run vivado_synth sqrt.toml --json | jq '.results.Fmax'

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
- a dependency ran again -- until declared inputs/outputs arrive, a dependency re-running always
  re-runs what depends on it, even if nothing it produced actually changed for this consumer;
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
stale dependency reruns first. Within one launch, a run directory is entered at most once: two
different configurations of one flow resolving to the same directory in the same launch is an
error naming both requesters (use ``--hashed-run-dirs`` to give them separate directories).

``trace.json``
---------------

Written last, atomically, only after a run succeeds, and removed before the *next* run of that
directory executes -- so its presence alone means "the last run here completed and succeeded". It
records:

- the run's settings identity (``flowrun_hash``) and design hash;
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

Without declared outputs, location is the only safe way to tell a run's file from a user's edit.
The run directory -- always under the run root now (D21) -- is the run's alone: every entry in it
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
It replaces the old
``--no-incremental``, the per-flow ``clean`` setting, GHDL's ``clean`` (now
``clean_before_analyze``), Verilator's ``clean_before_run``, and the launcher's
``cleanup_before_run`` setting -- each of those names now fails with what to use instead.

Flows that always run
----------------------

Some runs can never be reused, and say why: they always run, keep no trace, and report the reason
(``Running <flow>: <reason>``, and ``nodes[].reason`` under ``--json``):

- ``openfpgaloader``, and ``open_xc7`` when it programs (``program`` or ``board`` is set):
  "it programs a device" -- that changes the outside world. A non-programming ``open_xc7`` run is
  an ordinary node, reused when nothing changed.
- A flow asked for a fresh random seed: "it draws a new random seed". Every seed is a setting with
  a fixed default, so equal inputs give equal outputs: Verilator's random initialization (off unless ``random_init`` is set) uses
  ``random_seed`` (default 1) and cocotb's ``random_seed`` defaults to 1. Ask for a new seed per
  run with ``random_seed = "random"``, or with ``randomize_seed = true`` for ``nextpnr`` and
  ``open_xc7`` (now off by default).
- ``nextpnr`` when a board's pin constraints are fetched from a URL: "its constraints are fetched
  from a URL" -- no trace can verify a file on the network. A local constraints file (``lpf_cfg``,
  or a board from ``custom_boards_file``) is tracked as usual.

What is not tracked
---------------------

Each of these can make a stale result look fresh; ``--rebuild-all`` is the escape until each is
declared:

- **Programs started indirectly** -- a compiler run by ``make`` (Verilator's model build), Python
  packages such as cocotb imported by a simulator: only the programs Xeda starts itself are
  recorded.
- **Environment variables.** A flow that reads one without declaring it as a setting can change
  behavior invisibly to the trace.
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
declared and legacy dependencies, including producers without a trace. A change in the gap
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

A remote host must run a P1b-capable xeda build: the 0.4.4 release line (including development
builds) or newer, with remote protocol 3 or newer. Protocol 2 added canonical resolved settings,
relocated read inputs with their original path identities, declared output records and checked
hand-over. Protocol 3 adds the P1b simulation evidence rule: remote simulations must confirm that
the simulation ended successfully. Xeda checks the package the remote interpreter actually
imports before shipping the design. An older install or a build without protocol 3 support is
refused with an "upgrade the remote xeda" error. Until a release with protocol 3 is available,
install this P1b branch on the remote host.

A remote run (``--remote``) always runs fresh on the remote, and its results are always mirrored
locally in the hashed layout (``<design>/<flow>_<hash>``), so remote runs of different settings
never share a directory. ``--rebuild-all``, ``--clean`` and ``--hashed-run-dirs`` would change
nothing, so each is refused.

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
without running anything.

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
``Tool.execute`` right before the container starts) is written the same way. A tool is free to
replace a project or
a directory by its own name inside the run directory (Vivado's ``create_project -force``,
Quartus's ``project_new -overwrite``, DC's ``write_icc2_files -force``, Diamond's and ISE's new
project) -- the whole directory is Xeda's, so there is nothing of yours there to lose.

**Nothing outside the run root changes except what you name.** An output setting you give a
location, or ``--outputs-to``, is copied there (see `Outputs where you name them`_) -- that is the
one deliberate exception, and it never deletes anything: a destination is written and, if it was
already there, replaced only with your say-so. Two further exceptions exist, both something you
configure explicitly rather than something Xeda decides on its own: a git dependency's configured
``clone_dir``/``local_cache`` (otherwise its clone goes in the run root's ``.dependencies``, D21's
Task 7), and a writable entry of ``Docker.mounts`` you add yourself (a container otherwise gets the
design root and the RTL/testbench source directories mounted read-only; a dependency's run
directory is not mounted at all today, so a dockerized flow needing one of its files needs a
``Docker.mounts`` entry for it -- that is not yet automatic). In both cases Xeda writes there as
told, and guards nothing about what it finds: that directory is yours to manage, not Xeda's.

Outputs where you name them
============================

A flow's tools write only inside its run directory, under a fixed **conventional name** per
deliverable setting (plan 2's convention is ``outputs/<design>.<ext>``; until then, the setting's
own default name). Give such a setting a location -- an absolute path, or one anchored with
``$PWD``/``$DESIGN_ROOT`` -- and Xeda still writes the run's copy under its conventional name in
the run directory (what the tools do and what the run is identified by never depend on where you
asked for the copy); once the whole launch has finished, that file is **delivered**: copied to the
location you named. The same is true of ``--outputs-to DIR``, which delivers the requested flow's
artifacts, each at its path relative to the run directory. Renaming or moving a delivery's
destination therefore never re-runs the flow, and never changes what the tools do -- only where
the copy lands.

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
works). This is checked twice: before any tool of the flow runs (so a refusal is reported before
minutes of tool time are spent) and again right before the copy is made, in case something changed
in between -- a destination that changed since it was first checked is never replaced, confirmed
or not. A **fresh** run (one the trace found up to date, so no tool ran) delivers its outputs too:
delivery follows the run's outcome, not whether a tool executed.

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
only from its local mirror -- always the hashed layout, ``<design>/<flow>_<hash>/``, whatever
``--hashed-run-dirs`` says for local runs -- once the remote's results have been fetched there; a
deliverable given as a location is refused before anything is shipped, since the remote run's own
identity would otherwise depend on where the local side later copies its output. What a remote
run's ``--outputs-to`` may never land on or in is what the requested flow reads as the launch uses
its settings (its ``[flows.<flow>]`` section with ``-s`` over it), plus whatever every other flow's
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
