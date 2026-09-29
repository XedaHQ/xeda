******************************
Run directories and results
******************************

Every flow execution gets its own directory. Everything Xeda decided and everything the tools
produced is there, which makes a run reproducible and a failure diagnosable after the fact.

Where it goes
=============

Every run directory lies in the **run root**, ``./xeda_run`` by default; change it with
``--run-root`` or the ``XEDA_RUN_ROOT`` environment variable. Xeda creates and marks the run root;
keep nothing of yours there. ``--cwd`` relocates only the *requested* flow's run directory into
the current directory; its dependencies still go in the usual layout under the run root
(``./xeda_run/<design>/<flow>/`` by default), not beside it. The current
directory stays yours: Xeda never empties it, and deletes or writes over only what its own runs
created there, as they left it -- see `What Xeda deletes, and where`_.

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
layout -- never nested under it, and never relocated by ``--cwd``: only the requested flow moves,
its dependencies stay under the run root. ``--hashed-run-dirs`` names each by its own
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

What the flow parsed out of the tool's reports. Every flow reports:

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
a flow runs again. A flow reads only the reports its own run wrote: one last written before the
run started (by the clock of the run directory's file system) counts as missing -- "not written
by this run" -- so a tool that fails before it writes its reports never passes on the previous
run's.

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
- the programs it started (resolved path, size and mtime; a container image by its ID);
- its **inputs**, recorded *as the run found them when it started*: every file the design's
  ``rtl`` and ``tb`` name (sources, and a parameter or define given as a file, such as
  ``$readmemh`` data), every existing file a path-typed setting names (a relative path under both
  the design root and the start directory; nested settings too, such as the ghdl plugin's inside
  yosys's), every file under a directory a path-typed setting names (a compiled library in
  ``lib_paths``, Verilator's ``include_dirs``, a PDK directory: recursively, a symbolic link as
  itself -- by its target and, for a link to a file, that file's content -- never followed into a
  directory; version control's ``.git``, ``.hg`` and ``.svn`` left out; the flow's own output
  directories -- ``reports``, ``outputs``, OpenROAD's ``results`` and the like, which it writes --
  excepted, as is the run directory itself (a flow a setting of which names it, or one holding
  it, where Xeda does not manage it, always runs: see `Flows that always run`_), and the run
  root and run directory where a named
  directory holds them. In a run directory Xeda manages the directories inside it are not
  listed either, since every file there is an output; in the directory ``--cwd`` names, a library
  kept there is an input like any other), every file a flow resolves before
  it runs (yosys's ``abc_script``, expanded against the start directory or the environment), and
  the dependencies' outputs. A file whose record from the previous run still vouches for it (see
  below) is not read again;
- its **implicit inputs**, known only after the run: files a tool reported reading via a depfile
  (``yosys -E``), and files a flow reads on its own while it runs (a board's pin constraints);
- its **outputs**: in a run directory Xeda manages, every regular file the run left in it,
  recursively, artifact or not -- a depender may read any of them by path (``vivado_power`` reads
  the routed checkpoint of ``vivado_synth``) -- and the artifacts outside it. A symbolic link is
  recorded by its target and, when that is a file, by the file's content too; a directory it
  points to is not walked. Only the names Xeda reserves are left out (see `Locking`_). In the
  directory ``--cwd`` names, which is the user's, the outputs are the artifacts,
  ``results.json`` and every file of what the run created there by name (the files Xeda
  generates, a project and its directories -- see `What Xeda deletes, and where`_) only;
- each file above as ``(size, mtime, inode change time, inode, content hash)``;
- where each path-typed setting points, by its name (``lib_paths[0][1]``): an absolute path (a
  ``$PWD`` or ``$DESIGN_ROOT`` path once expanded) as itself, a relative one as each existing
  path it is found at under the design root and the start directory. A run's identity counts
  ``$PWD/libs`` as written, so launched from another start directory -- or for another design
  tree whose design file reads the same -- it has the same identity; a setting that names a
  directory (a compiled library) is bound by nothing else, and pointing elsewhere makes the run
  stale ("``lib_paths[0][1]`` now names ``<B>/libs`` (was ``<A>/libs``)");
- a ``run_id`` for this run, and the ``run_id`` of each dependency's run it consumed, keyed by
  that dependency's run directory.

Recording the inputs before the run is what makes an edit made *during* a long run count: the
file no longer matches the trace, so the next launch runs again. A file that no one could record
before the run (one a depfile names for the first time, a program) and that was written while the
run went on is recorded as unknown, which never matches.

Without declared outputs, location is the only safe way to tell a run's file from a user's edit.
A run directory Xeda manages -- under the run root, never the directory ``--cwd`` names -- is the
run's alone: every file in it is the run's output, whether the run wrote it or not. Anywhere
else, including the directory ``--cwd`` names, a file named by a setting, a depfile, or the
design stays an input; if it was absent before the run or changed during it (its mtime, or its
inode change time, at or after the run started), its record is unknown and the next launch
re-runs. A flow that writes a settings-named output outside its managed run directory (or into
a directory a setting names there), and a ``--cwd`` run that writes files its settings name,
therefore re-run on every launch until plan 2 declares outputs.

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
Refused together with ``--cwd`` (it would empty the current directory). It replaces the old
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
  a fixed default, so equal inputs give equal outputs: Verilator's random initialization uses
  ``random_seed`` (default 1) and cocotb's ``random_seed`` defaults to 1. Ask for a new seed per
  run with ``random_seed = "random"``, or with ``randomize_seed = true`` for ``nextpnr`` and
  ``open_xc7`` (now off by default).
- A flow a setting of which names the directory it runs in, or one holding it, when that is not a
  directory Xeda manages (``include_dirs = ["."]`` with ``--cwd``): "a setting (include_dirs[0])
  names the directory it runs in, whose contents cannot be told apart from its outputs" -- the
  run writes into the very directory it reads, so no trace can vouch for it.
- ``nextpnr`` when a board's pin constraints are fetched from a URL: "its constraints are fetched
  from a URL" -- no trace can verify a file on the network. A local constraints file (``lpf_cfg``,
  or a board from ``custom_boards_file``) is tracked as usual.

What is not tracked
---------------------

Each of these can make a stale result look fresh; ``--rebuild-all`` is the escape until each is
declared:

- **What a symbolic link in a directory a setting names points to, when that is a directory**:
  the link is recorded by where it points; the directory's contents are not.
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

A lock file, ``<run dir>.lock`` beside the run directory, serializes concurrent launches of the
same flow directory (POSIX only; there is no lock on Windows) -- so two overlapping ``xeda run``
invocations that share a dependency take turns with it instead of one clobbering the other's
output. ``xeda scrub`` removes the lock file along with the directory. The directory ``--cwd``
names is locked by a ``.xeda.lock`` file inside it.

Xeda reserves these names at the top of a run directory, and never counts them as a run's
outputs: ``trace.json``, ``trace.json.tmp`` (a trace being written), ``.xeda.lock``,
``.xeda-owned.json`` and ``.xeda-owned.json.tmp`` (what Xeda owns in a directory it does not
manage, see `What Xeda deletes, and where`_), and ``.xeda-time-*`` (a marker touched to read the
file system's clock, removed at once). A flow must not write files by these names.

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
     - After a run, keep only ``settings.json``, ``results.json``, the artifacts and xeda's
       ``.xeda-run-dir`` marker (dependencies included; deferred until the requested flow has
       finished, so it can still read their files).
   * - ``--post-cleanup-purge``
     - After a run, remove the run directory entirely.
   * - ``--scrub``
     - Before running, remove the flow's other run directories for this design. Asks for
       confirmation.

Pruning (``--post-cleanup``/``--post-cleanup-purge``) drops the trace first, before anything else
it removes: a pruned run is not reused, even though its ``settings.json``/``results.json`` remain.

``xeda scrub <flow> <design_name>`` removes previous run directories of one flow for one design,
without running anything.

What Xeda deletes, and where
-----------------------------

Whether a run directory is Xeda's is decided once per flow, when it is launched: one Xeda chose
under its run root (``--run-root``), and that lies there (not through a symbolic link to
elsewhere), is Xeda's; the directory ``--cwd`` names is yours. Every deletion -- ``--clean``, the
clean-ups above, ``xeda scrub``, and what a flow removes before it runs its tool (a previous work
library, dependency files, stale reports, a previous implementation directory) -- goes through
one checked operation that reads it:

- in Xeda's directory, anything inside it may be deleted -- nothing outside it: a path through a
  symbolic link that leads out is refused, and a link is removed as itself;
- in your directory, only what Xeda owns there (below), as it left it. ``--clean`` is refused,
  and a flow never empties it. GHDL's ``ghdl remove`` (``clean_before_analyze``) runs there only
  over a work library Xeda made there, as it left it; one of yours stops the run, naming it.

What Xeda owns in your directory is what its runs created there by name: the files it generates
itself (scripts, constraints, ``settings.json``, ``results.json``), a project a tool creates with
its directories (Vivado's ``<design>.xpr`` and ``<design>.runs``, ...), a directory a tool writes
whole (DC's ``outputs/icc2_files``, xsim's ``xsim.dir``), and the run's artifacts. After every
run that executed -- one that failed too -- Xeda records each of these files in the directory's
ownership record, ``.xeda-owned.json``. The next run:

- writes a generated file over a file by that name only if Xeda owns it, as it left it (or it
  already holds exactly what Xeda would write);
- lets a tool replace a project or a directory by its name (Vivado's ``create_project -force``,
  Quartus's ``project_new -overwrite``, DC's ``write_icc2_files -force``, Diamond's and ISE's new
  project) only where nothing of it is there yet -- and then does not tell the tool to replace
  anything -- or where what is there is Xeda's, every file in it as Xeda left it; a file you
  added to a project directory, or an edit, counts;
- deletes a file, or a directory it claimed, only on the same terms.

Anything else in the way stops the flow before its tool runs, naming what it would have
replaced: move it away, or let Xeda choose the run directory (without ``--cwd``). An output file
you name yourself -- a setting you give, in any settings layer, such as ``saif`` or
``bitstream_file`` -- is written where you said, replacing the file there, wherever that is;
never a directory, a source of the design, or a file another setting names. A source of the
design is never written over -- not even in Xeda's directory -- and neither is a file a setting
names, unless Xeda owns it (a setting naming where Xeda writes a file). So repeated ``--cwd`` runs
of ``vivado_synth`` replace the project the previous run left, and a run after a failed one
replaces what that one left; the first run in a directory that holds a project of yours by the
design's name refuses.
