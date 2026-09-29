******************************
Run directories and results
******************************

Every flow execution gets its own directory. Everything Xeda decided and everything the tools
produced is there, which makes a run reproducible and a failure diagnosable after the fact.

Where it goes
=============

The parent is ``./xeda_run`` by default; change it with ``--xeda-run-dir`` or the
``XEDA_RUN_DIR`` environment variable. ``--cwd`` relocates only the *requested* flow's run
directory into the current directory; its dependencies still go in the usual layout under
``--xeda-run-dir`` (``./xeda_run/<design>/<flow>/`` by default), not beside it.

A run replaces and deletes files in its run directory, so xeda runs only in a directory of its
own, and marks every run directory it uses with a ``.xeda-run-dir`` file:

- ``--cwd`` (or the API's ``run_path``) needs a directory that is empty, or one xeda ran in
  before. Run without ``--cwd`` from a directory holding your own files.
- An existing directory where xeda would put a run (``--xeda-run-dir`` pointed at a directory of
  yours, say) is used only if it is marked, is empty, or holds an earlier xeda run of the same
  flow (its ``settings.json`` says so: runs of older releases are adopted and marked). Otherwise
  the run is refused, naming the directory, before anything is written or deleted. ``xeda scrub``
  removes only such directories too, and ``--remote`` fetches a run's results only into such a
  directory, refusing any other before it connects.
- xeda deletes nothing outside the run directory. An output path you name outside it
  (``-s bitstream=/elsewhere/x.bit``) is written by the tool where you said, as it always was,
  replacing a file already there; confirmation before replacing one comes in 0.5.
- Files you put into a run directory of xeda's are removed by the next ``--clean``.
- xeda never writes through a symbolic link: a file it generates (a script, constraints,
  ``settings.json``, ``results.json``, a copied resource, the marker) replaces a link at its name
  instead of writing to what the link points to.

Within the parent, the layout depends on ``--run-dirs``:

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Options
     - Path
   * - *(default,* ``--run-dirs stable`` *)*
     - ``<design>/<flow>/``
   * - ``--run-dirs hashed``
     - ``<design>/<flow>_<16-char settings hash>/``

A dependency gets its own directory, a **sibling** of the flow that launched it, in the same
layout -- never nested under it, and never relocated by ``--cwd``: only the requested flow moves,
its dependencies stay under ``--xeda-run-dir``. ``--run-dirs hashed`` names each by its own
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

``xeda run`` is make-like *by default*: ``--rebuild stale`` re-runs a flow only when something it
consumed or produced changed since its last successful run; ``--rebuild all`` runs every flow
unconditionally. A flow that runs logs why (``Running <flow>: <reason>``); a flow left alone logs
that it is up to date, and its previously recorded results are shown as if it had just run.

What makes a flow stale
------------------------

- there is no successful previous run (none yet, or the last one failed);
- the flow always runs: it programs a device, draws a new random seed, or reads pin constraints
  fetched from a URL -- see `Flows that always run`_;
- its settings changed -- the reason names which ones;
- Xeda changed: its version, or any file of the installed package (code, templates, bundled data
  -- an editable install keeps one version string across edits); for a flow defined outside Xeda,
  its own modules and templates;
- a program it started (an executable's path, size or mtime; a container image's ID) changed, or
  was replaced while the run was going on;
- a dependency ran again -- until declared inputs/outputs arrive, a dependency re-running always
  re-runs what depends on it, even if nothing it produced actually changed for this consumer;
- an input was added, removed, changed, or has gone missing -- or was modified while the run that
  read it was going on, so that what the run read is unknown;
- an output was deleted or edited (by hand, or by another tool) since the run that produced it;
- the design's metadata changed (``top``, parameters, defines, and other behavior-affecting
  fields -- not merely where files live).

Dependencies are always brought up to date before the flow that depends on them is judged, so a
stale dependency reruns first. Within one launch, a run directory is entered at most once: two
different configurations of one flow resolving to the same directory in the same launch is an
error naming both requesters (use ``--run-dirs hashed`` to give them separate directories).

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
  yosys's), and the dependencies' outputs;
- its **implicit inputs**, known only after the run: files a tool reported reading via a depfile
  (``yosys -E``), and files a flow reads on its own (a board's pin constraints);
- its **outputs**: the flow's artifacts, ``results.json``, and every file the run wrote that a
  setting or a depfile names (a netlist written where a setting says, a rendered script);
- each file above as ``(size, mtime, content hash)``;
- a ``run_id`` for this run, and the ``run_id`` of each dependency's run it consumed, keyed by
  that dependency's run directory.

Recording the inputs before the run is what makes an edit made *during* a long run count: the
file no longer matches the trace, so the next launch runs again. A file that no one could record
before the run (one a depfile names for the first time, a program) and that was written while the
run went on is recorded as unknown, which never matches.

Whether a file is an input or the run's own is decided by where it came from, not where it lies:
a file written during the run (its mtime, or its inode change time, at or after the run started)
is the run's own when it is in a run directory Xeda manages, when it was created directly in the
directory ``--cwd`` names, or when the previous run wrote it too -- it is then an output, and not
an input of the next run. Otherwise it may as well be a file you edited while the run read it, and
it is recorded both ways, which costs at most one extra run for a file a run rewrites every time.
So ``--cwd`` from the design directory still tracks the design's constraints and includes, and an
output a setting places outside the run directory is not mistaken for a new input.

A file counts as unchanged when its size and mtime match its record and it was not modified within
2 seconds of being recorded (to rule out a racy timestamp); otherwise its content hash decides. So
a bare ``touch``, or checking out a branch and back, costs a content hash -- not a re-run -- while
a file quietly restored with an old timestamp is still noticed.

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
- ``nextpnr`` when a board's pin constraints are fetched from a URL: "its constraints are fetched
  from a URL" -- no trace can verify a file on the network. A local constraints file (``lpf_cfg``,
  or a board from ``custom_boards_file``) is tracked as usual.

What is not tracked
---------------------

Each of these can make a stale result look fresh; ``--rebuild all`` is the escape until each is
declared:

- **The contents of a directory a setting names** -- an include directory, a ``lib_paths``
  directory, a PDK directory: a header added to or removed from it goes unnoticed unless a tool
  reports reading it in a depfile.
- **Programs started indirectly** -- a compiler run by ``make`` (Verilator's model build), Python
  packages such as cocotb imported by a simulator: only the programs Xeda starts itself are
  recorded.
- **Environment variables.** A flow that reads one without declaring it as a setting can change
  behavior invisibly to the trace.
- **Files a tool finds on its own without reporting them** -- a Vivado IP repository, tool data
  outside any setting, anything not named by a setting and not listed in a depfile.
- **Hand edits to a dependency's files that are not among its artifacts** -- a depender may read
  them, but only the dependency's declared artifacts are recorded.

Pin constraints fetched from a URL are not verifiable either, which is why a flow using them
always runs (`Flows that always run`_).

Locking
--------

A lock file, ``<run dir>.lock`` beside the run directory, serializes concurrent launches of the
same flow directory (POSIX only; there is no lock on Windows) -- so two overlapping ``xeda run``
invocations that share a dependency take turns with it instead of one clobbering the other's
output. ``xeda scrub`` removes the lock file along with the directory. The directory ``--cwd``
names is locked by a ``.xeda.lock`` file inside it.

A remote run (``--remote``) always runs fresh on the remote: an explicit ``--rebuild`` or
``--clean`` is refused. Its results are mirrored locally in the ``--run-dirs`` layout.

Cleaning up
===========

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Option
     - Effect
   * - ``--clean``
     - Empty every flow's run directory before it runs, and run every flow.
   * - ``--post-cleanup``
     - After a run, keep only ``settings.json``, ``results.json``, the artifacts and xeda's
       ``.xeda-run-dir`` marker (dependencies included; deferred until the requested flow has
       finished, so it can still read their files).
   * - ``--post-cleanup-purge``
     - After a run, remove the run directory entirely.
   * - ``--scrub``
     - Before running, remove previous run directories of the same flow. Asks for confirmation.

Pruning (``--post-cleanup``/``--post-cleanup-purge``) drops the trace first, before anything else
it removes: a pruned run is not reused, even though its ``settings.json``/``results.json`` remain.

``xeda scrub <flow> <design_name>`` removes previous run directories of one flow for one design,
without running anything.
