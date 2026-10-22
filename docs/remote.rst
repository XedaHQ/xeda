Remote runs
===========

``xeda run FLOW DESIGN --remote HOST`` runs the whole request on another machine over SSH and
brings the results back. Use it when the EDA tools, the licenses or the CPU cores are on a server
and the design files are on your laptop. A remote run takes the same request as a local run: a flow,
a chain, bindings, a target, ``--outputs-to`` and ``--dry-run``.

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --remote cluster

``HOST`` is any OpenSSH destination: an alias from ``~/.ssh/config``, ``user@host`` or
``ssh://user@host:port``. The ``XEDA_REMOTE`` environment variable gives the same value. The host
needs no Xeda. The first run installs it under one directory of the host (see `The first run`_).
The results arrive in ``<run root>/@HOST/`` (see `The local mirror`_).

To start:

1. Run ``ssh HOST true``. If this works without a problem, ``--remote HOST`` can connect.
2. Run ``xeda remote check HOST``. It installs Xeda on the host and shows which tools of the flows
   the host finds.
3. Run your request with ``--remote HOST``.

This is how a remote run works:

1. Xeda checks the request on your machine, with the code of a local run. A mistake in the request
   shows before Xeda connects to anything.
2. Xeda opens one SSH connection and makes sure that the host has the same Xeda that you run here.
   The first run on a host installs it. Later runs find it ready.
3. Xeda sends the inputs. Only the files that changed since the last run go over the wire.
4. The host runs the request with the normal launcher, in a workspace that stays between runs. A
   flow that is up to date is not run again, as in a local run.
5. Xeda copies the results, logs, reports and declared outputs into a mirror in your run root, and
   delivers ``--outputs-to`` from there.

Everything that Xeda installs on the host goes under one directory, the remote root. Xeda never
installs anything outside it, and it needs no administrator rights on the host.

Requirements
============

On your machine:

* The OpenSSH client, ``ssh``, on the ``PATH``. Xeda uses ``ssh -G`` to resolve the destination,
  the ControlMaster feature to share one connection, and ``ssh -O check`` and ``ssh -O exit`` to
  manage it. Other SSH clients do not offer these. Linux and macOS work. A native Windows machine
  is not supported; use WSL.
* CPython. Xeda installs the same Python version on the host, and it refuses a local Python of
  another implementation.

On the host:

* An SSH server that runs commands, on a Linux system with an x86_64 or aarch64 CPU and glibc or
  musl. Xeda refuses any other system. The host needs no Xeda, no Python and no ``uv``.
* A POSIX shell (``sh``), ``bash``, ``tar``, ``curl`` or ``wget``, and ``sha256sum`` or ``shasum``.
  Xeda starts every launch with ``bash -lc``, so that your login profile applies.
* Internet access on the first run: to GitHub, for the ``uv`` program and for a Python build, and
  to the Python package index, for the packages of Xeda. A later run needs it again only when the
  package versions change, for example when you upgrade a dependency of Xeda on your machine or add
  ``tb.python_packages``. A host behind a proxy or with a package mirror gets the settings in the
  ``env`` table of its profile (see `Host profiles`_).
* The EDA tools of the flows. Either they are on the ``PATH`` of the login shell, or the ``setup``
  lines of the host profile make them available (``module load vivado``, or a ``settings64.sh``
  file). A flow that runs a tool in Docker needs Docker on the host.

Python is not a requirement of the host. Xeda installs the Python version that runs Xeda on your
machine, with ``uv``. A host profile can name a Python of the host instead.

These limits apply to every remote run:

* The run directories are on the host. ``--post-cleanup`` and
  ``--post-cleanup-purge`` are refused. Use ``xeda remote clean`` to free the space on the host.
* ``--scrub`` is refused, because it asks you to confirm and a remote launch cannot ask.
* A deliverable setting that names a location is refused, because it would write on the host. Use
  ``--outputs-to`` (see `What comes back`_).
* A flow that programs a board (``openfpgaloader``) cannot run with ``--remote``, because the board
  is on your machine. Run the flows before it with ``--remote``. Then run the programmer locally
  with a binding to the fetched bitstream.
* A design generator runs on your machine, and Xeda sends the generated sources.

The first run
=============

The first run on a host makes a set of directories under the remote root, and shows one line for
each long step:

.. code-block:: text

    Connecting to cluster...
    Remote host cluster: Linux x86_64
    Installing uv 0.12.23
    Installing Python 3.12
    Creating the environment
    Uploading xeda

A later run shows the first two lines and none of the others. The remote root is
``${XDG_CACHE_HOME:-~/.cache}/xeda`` unless the host profile names another one (a scratch file
system, for example). The host ignores a relative value of ``XDG_CACHE_HOME``. Xeda keeps a layout
version in the path, so a later layout cannot misread this one:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Path below the remote root
     - Content
   * - ``v1/uv/<version>-<target>/``
     - The ``uv`` program, a pinned release that Xeda downloads and checks against its SHA-256.
   * - ``v1/python/``
     - The Python builds that ``uv`` installs.
   * - ``v1/cache/uv/``
     - The package cache of ``uv``.
   * - ``v1/env/<key>/``
     - A Python environment with the packages of Xeda, and ``xeda-lock.txt``, the list of the
       installed packages and versions.
   * - ``v1/code/<key>/``
     - A copy of the ``xeda`` package that runs on your machine.
   * - ``v1/ws/<key>/``
     - A workspace: the inputs, the run directories and the records of the launches (see
       `The workspace`_).
   * - ``v1/bin/``, ``v1/locks/``, ``v1/tmp/``
     - The helper scripts and the file of the ``env`` table, the locks and the scratch space.

The environment is built once for each set of requirements. Its key covers the requirements of
Xeda and of ``tb.python_packages``, the exact version that you have installed locally of each
package in their dependency closure, the Python implementation and version, the version of ``uv``,
and the CPU architecture, the C library and its version on the host. So the host runs the package
versions that you run, for everything that Xeda needs. A new Xeda release, or an upgrade of one of
those packages, builds a new environment once. The code of Xeda is separate from the environment.
When the Xeda code on your machine changes, the next run uploads a new copy of the package, which
takes a moment.

A directory counts only when its completion marker exists, and a process that dies while it builds
leaves nothing that a later run would trust. A lock serializes two builds of one directory. An
environment is never changed after it is complete. Hosts that share one home directory share the
environments, and a host with another CPU architecture or C library gets an environment of its own
next to them.

Xeda refuses to build an environment when a package of that dependency closure is an editable
install, a local directory or a direct URL: a version number does not identify its code. Install
it from a package index, then run again. Other packages of your Python environment do not matter.
A package that has no release for Linux fails the build with the code ``env-failed``, and the
output of ``uv`` names the package.

Authentication and SSH options
==============================

Xeda starts the OpenSSH client of your machine and has no SSH options of its own. Your
``~/.ssh/config`` holds everything about the connection: user, port, identity files, jump hosts
(``ProxyJump``) and certificates. Xeda adds only these options:

* ``ControlMaster``, ``ControlPath`` and ``ControlPersist``, for connection sharing,
* ``ServerAliveInterval`` and ``ServerAliveCountMax``, for keepalive. They take precedence over the
  same options in your configuration.
* ``BatchMode=yes`` and ``-T``, for its own commands through the shared connection.

None of them weakens authentication or the host key check. Test the destination with ``ssh HOST``
first. If that works, ``--remote HOST`` connects.

* Each launch opens one connection, and every Xeda command of the launch goes through it. You log
  in once. A key, an agent, a certificate, a password, a passphrase or a second factor works as it
  does for ``ssh``: OpenSSH asks for what it needs, on your terminal. Xeda never reads, asks for or
  stores a secret.
* OpenSSH checks the host key as you configured it. Xeda adds no host key option. If OpenSSH asks
  you to accept a new host key, it asks on your terminal.
* A launch with no terminal (a CI job, for example) cannot answer a prompt. Use a key and an agent.
* Xeda sets a keepalive on the connection, so a dead network ends it in about a minute.
* The control socket is in a private temporary directory of your machine.
* Xeda closes the connection when the launch ends. If Xeda is killed, OpenSSH ends the idle
  connection after five minutes.

Host profiles
=============

A host profile holds what is specific to Xeda on one host. Everything about SSH stays in
``~/.ssh/config``. The profiles are in the file ``${XDG_CONFIG_HOME:-~/.config}/xeda/remotes.yaml``.
Xeda ignores a relative value of ``XDG_CONFIG_HOME``. The file is optional. A host with no entry
gets the default profile, which suits a host whose tools are on the login ``PATH``.

The ``hosts`` table is keyed by the exact text of ``--remote``. ``--remote cluster`` uses
``hosts.cluster`` and ``--remote alice@cluster`` uses ``hosts.alice@cluster``. Write the file
as YAML, as in this example, which is a complete ``remotes.yaml``:

.. code-block:: yaml

    # remotes.yaml
    hosts:
      cluster:
        root: /scratch/alice/xeda
        setup:
          - module load vivado/2025.2
        env:
          XILINXD_LICENSE_FILE: 2100@license.example.org
        python: managed
        flows:
          vivado_synth:
            nthreads: 8
          openroad:
            pdk_root: /data/pdks

Every field is optional:

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Field
     - Meaning
   * - ``root``
     - The remote root. Write ``~/`` and a path below the home directory, or an absolute path. The
       path may have letters, digits and the characters ``._/+-`` only. The default is
       ``$XDG_CACHE_HOME/xeda``, else ``~/.cache/xeda``. Put it on a file system with enough space
       for the environments and the run directories.
   * - ``setup``
     - A list of shell lines. They run in the ``bash -lc`` of every launch on the host, before
       Xeda starts, in this order. Use them for ``module load``, ``source settings64.sh``, or a
       license variable. They do not apply to the steps that install Xeda, nor to ``xeda remote
       check``.
   * - ``env``
     - Environment variables for every remote command, the installation and the launches included.
       Use it for a proxy, ``UV_INDEX_URL`` or ``SSL_CERT_FILE``. The file that holds them is
       readable by the owner only, because a proxy setting can hold a password. Xeda refuses a name
       that it sets itself (``PYTHONPATH``, ``PYTHONHOME``, ``VIRTUAL_ENV``, ``UV_CACHE_DIR``,
       ``UV_PYTHON_INSTALL_DIR``, ``UV_NO_CONFIG``, and every name that starts with ``XEDA_BOOT_``).
   * - ``python``
     - ``managed`` (the default) lets ``uv`` install the Python version that you run locally. Or
       give the absolute path of a Python on the host. It should be the same version as your local
       Python. Nothing checks this. The environment is built with the Python that you name. If its
       version differs, the packages can resolve differently or fail to install, and the code can
       behave differently from your local runs.
   * - ``flows``
     - Flow settings that apply on this host only (see `Host settings`_).

Xeda checks the whole file every time it reads it. An entry that is not valid fails the load for
every host, and the error names the host and the field.

Host settings
-------------

The ``flows`` table has the same shape as the ``flows`` section of a design file: a table of
settings for each flow name. It is a settings layer for the host. It sits just above the defaults
of the flow, and below the project file, the design file, the target and the command line (see
:doc:`flows`). Use it for facts about the host, such as the thread count that suits its CPU. Xeda
applies this layer to the checks on your machine as well, so both sides plan the same request.

A *locator* setting, such as ``pdk_root``, names the place of installed data. A value from a local
layer names a place on your machine, so Xeda never sends it. The remote gets a locator from the
host profile only. If the profile has none, the flow on the host finds the data in its own default
place.

Testbench Python packages
=========================

A cocotb testbench imports Python packages that are not dependencies of Xeda. Name them in the
``tb`` section of the design, so that every user and every host gets the same list:

.. code-block:: yaml

    tb:
      python_packages: [cocotb, cocotb-bus]

Xeda checks locally that each package is installed, and reports a missing one before it connects.
The remote environment installs each package at the version of your local installation, with the
packages that it needs. The list is part of the key of the environment. See :ref:`the tb section of
the design file <tb>`.

The workspace
=============

A workspace is the part of the remote that belongs to one local run root and one remote
destination. Its key is a digest of the identity of your machine, the resolved path of the run root
and the user, host name and port that ``ssh -G`` gives for ``HOST``. The same alias from the same
machine and run root finds the same workspace on the next run. Another ``--run-root`` is another
workspace. The key uses the name that ``ssh -G`` resolves, and not the name of the node that
answers. So a round-robin alias of a cluster whose login nodes share a home directory uses one
workspace, and keeps its reuse on every node. The workspace lock protects it from two launches at
once.

A workspace has these parts:

* ``inputs/<design>/`` holds your design files. Xeda sends a manifest of SHA-256 digests, and the
  host asks only for the files it lacks. A file that did not change is not touched, so the
  freshness records of the host stay valid. A file that left the design is removed. No flow writes
  here.
* ``xeda_run/`` is the run root of the host. Its layout is the layout of a local run root, so
  ``--hashed-run-dirs`` and the other launch options mean the same as for a local run.
* ``launches/<id>/`` holds the log, the request and the result of each launch.
* ``lock`` is held for the time of the sync, the launch and the fetch. Two commands from one
  machine cannot change each other's sources. A command that finds the workspace busy waits. Every
  10 seconds it shows which launch holds the workspace, or "another session" when no launch runs.

The runs are make-like, as local runs are. A second run of the same request finds the run
directories of the first one in the workspace. A flow whose inputs and settings did not change is
fresh, and its results come back without running the tool. A run that passed or was rejected by its
checks is reused. A run that failed runs again. ``--rebuild-all`` and ``--clean`` act on the run
directories of the host. ``--scrub`` is refused with ``--remote``, because it asks you to confirm
and a remote launch cannot ask. Run ``xeda remote clean`` to free the space.

The local mirror
================

Xeda copies the files of a remote run into ``<run root>/@<host>/``, with the layout of a local run
root (``<design>[/<target>]/<flow>[_<hash>]``). ``<host>`` is the text of ``--remote``, with each
character other than a letter, a digit, ``.``, ``_``, ``@`` and ``-`` replaced by ``_``. The mirror
keeps the files as the host wrote them. Xeda checks each file against its SHA-256 and writes it
atomically. A symbolic link at ``@<host>``, at a design or at a target below it makes Xeda
refuse the run before it writes or deletes anything there, as for a local run root (see
:doc:`run-directories`). The mirror holds no ``trace.json``, so a local run never reuses a
mirror directory. An artifact that a symbolic link on the host leads out of its run directory
does not come back: the results name its place in the mirror, where no file lies (see "Outputs
where you name them" in :doc:`run-directories`).

The records ``results.json`` and ``settings.json`` of each mirror directory are written again with
the paths of your machine, where Xeda can map a path:

* A path in a run directory of the host becomes the same path in the mirror.
* A path of an input that you sent becomes your local file. A path of the inputs directory with no
  local file of that name becomes the same place below your design root.

Xeda keeps a path that it cannot map as it is. These paths stay: a file that Xeda made for the
host (the design document, and the copies of Liberty files whose includes it renamed), and any path
elsewhere on the host, such as the installation of a tool or a PDK store. The directory
``.launches/`` in the mirror holds the records that ``xeda remote attach`` needs.

Each fetch replaces the mirror directories of the planned flows. If a launch or a fetch fails,
also when the connection ends in the middle of the transfer, Xeda empties them, so that an older
run or a part of the new one never looks like a whole result.

What comes back
---------------

By default Xeda fetches, for every flow of the plan:

* ``results.json`` and ``settings.json``,
* the ``*.log`` files at the top of the run directory, and
* everything below ``reports/``.

For the flow that the request names, it also fetches the declared outputs and the artifacts. A
flow that failed returns only the files that its run wrote, and no declared outputs. A file outside
the run directory is in no record of a run (see :ref:`results-json`), and Xeda never reads or
sends it: an artifact there keeps its path on the host in the results, and a warning names it.

``--fetch-all`` fetches every file of the run directories, ``trace.json`` excepted. Use it to look
at the intermediate files of a tool.

``--outputs-to DIR`` delivers from the mirror after a run that succeeded. A deliverable setting
that gives a location (``-s flows.vivado_synth.bitstream=$PWD/out.bit``) would write on the host,
so Xeda refuses it before it connects. Use ``--outputs-to`` instead. A delivery happens only in
the command that has ``--outputs-to``. ``xeda remote attach`` delivers nothing.

Machine-readable output
-----------------------

With ``--json``, ``xeda run --remote`` writes one document on stdout. It is the document of a local
run, plus the keys ``remote`` (the text of ``--remote``) and ``mirror`` (the mirror directory), and
the paths in it name the mirror. A dry run adds ``uploaded``. The document of a failed remote step
has ``remote`` and no ``mirror``. A launch that Xeda stopped following has the key ``launch`` with
the ``id`` and the ``host``. :doc:`machine-readable` lists all keys. The output of
the tools goes to stderr.

Dry runs
========

``xeda run ... --remote HOST --dry-run`` shows the plan, and what each flow would do, as it is on
the host. The host judges freshness against its own workspace, so the answer is correct. To answer,
Xeda may provision the host and sync the inputs into the workspace. It never writes a run
directory, and it says which input files it uploaded. ``--keep-running`` has no launch to keep
running in a dry run, and Xeda refuses the combination.

Ctrl-C and lost connections
===========================

A normal remote run stops when you stop it or when the connection goes away:

* **Ctrl-C** during the launch asks the host to cancel it. The launch gets SIGINT first, as it does
  at a terminal. It passes the signal on to its tools, so they stop as they stop in a local run.
  After 15 seconds it gets SIGTERM, and after 10 more seconds SIGKILL. The same holds for the
  processes that the tools started. A second Ctrl-C closes the connection at once. The host then
  stops the launch by itself.
* A **dropped connection** is noticed by both sides. The local side sends a signal every 5 seconds.
  The host cancels the launch when the connection ends, or when it hears nothing for 30 seconds.
  If a process survives SIGKILL, the error names the number of survivors.
* **Ctrl-C after the launch has finished**, while Xeda fetches or delivers, leaves the results on
  the host. The error is ``RemoteInterrupted``.

To recover, run the same command again. The workspace persists, so the host finds the result of a
run that finished and reuses it. Only the fetch happens again. A run that was canceled in the
middle runs again.

Launches that keep running
==========================

``--keep-running`` keeps the launch running on the host when the connection ends:

.. code-block:: bash

    xeda run vivado_synth examples/vhdl/sqrt/sqrt.yaml --remote cluster --keep-running

Xeda prints the launch id and follows the log. If you press Ctrl-C, or the connection drops, Xeda
leaves the launch running and stops with the error ``RemoteDetached``. Then:

* ``xeda remote attach HOST [LAUNCH_ID]`` shows the log from its start, waits for the end and
  fetches the result into the mirror. Without ``LAUNCH_ID`` it takes the launch that started last
  among those that run in the workspace, else the one that started last, and says which one. It delivers nothing:
  run the first command again to deliver. That command reuses the finished run.
* ``xeda remote cancel HOST [LAUNCH_ID]`` stops a running launch as Ctrl-C stops a normal one. It
  exits with status 1 if a process of the launch survived SIGKILL.
* Running the same request again follows the running launch instead of starting another one. Ctrl-C
  then stops only the following. A request counts as the same when its options and the content of
  its inputs are the same. The verbosity of the log does not count. A different request waits for
  the workspace lock.

Both commands take ``--run-root``, because the run root names the workspace. Give the value of the
``xeda run`` that started the launch. Attach needs the record of the launch in the mirror of this
machine. Without it, run the first command again. Xeda also refuses to fetch the files of a launch
when a later launch used the same inputs, because the run directories may hold other files.

.. warning::

   On a host where systemd-logind has ``KillUserProcesses=yes``, the end of your last session
   kills every process of your user, also a launch that keeps running, unless the administrator
   enabled lingering for you (``loginctl enable-linger``). ``xeda remote check`` reports the
   setting, and ``--keep-running`` warns about it.

Check and clean
===============

``xeda remote check HOST`` connects like a run does and installs what is missing. The environment
that it makes is the one of Xeda alone, without the ``tb.python_packages`` of a design. Then it
reports:

* the platform, the Xeda version and the Python of the host,
* the environment, with the path of its ``xeda-lock.txt``,
* the free disk space of the remote root,
* the ``KillUserProcesses`` setting, with a warning when it is ``yes``,
* the executables of the flows that a login shell finds, and the ones it does not find, and
* whether ``docker`` is on the ``PATH``, which only the flows that run a tool in Docker need.

``check`` looks at the login ``PATH``. It does not run the ``setup`` lines of the profile, so a tool
that only ``setup`` makes available shows as missing. A missing tool is not an error. The exit
status is 0 when the connection and the environment work. ``--json`` writes one document.

``xeda remote clean HOST`` removes what nobody uses: old environments, old code copies and
workspaces, together with the run directories in them. It lists the entries, asks, and then removes
them. It keeps anything used within the last 30 days, and anything in use. A workspace, an
environment and a code copy are in use while a process holds a lock on their lock file. Every agent
process holds the locks of its own environment and code copy while it runs, ``clean`` included, and
a run that you keep running holds them until it ends. On a home directory that several machines
share, this protection works across the machines only if the file system supports ``flock``
between them (NFS may not). The options are:

``--older-than DAYS``
    Change the age limit (the default is 30).

``--all``
    Remove everything that is not in use, however recently it was used.

``--yes``
    Do not ask. ``--json`` does not ask either.

Troubleshooting
===============

Every failure of a remote run is an error with a type. A text run shows the message. With
``--json``, the document has ``success: false`` and ``error.type`` and ``error.message`` (see
:doc:`machine-readable`). The messages name the step that failed and the cause. Run with ``-d`` to
see the commands and the output of the host.

Common problems
---------------

Connection refused, "No route to host", "Could not resolve hostname" or "Permission denied"
    These are messages of OpenSSH in a ``RemoteConnectionError``. Run ``ssh HOST`` by hand and fix
    the destination, the network or your ``~/.ssh/config``. Xeda connects when ``ssh HOST`` does.

A prompt for a password or a passphrase on every run
    Load a key into an agent, or use a certificate. OpenSSH asks once for each launch, as it does
    for any ``ssh`` command.

The login profile of the host prints text
    Xeda skips ordinary text before its own output, up to 1 MiB. A larger amount makes a
    ``RemoteProtocolError``. Print only in an interactive shell.

A tool is not found on the host
    Run ``xeda remote check HOST``. It lists the programs of the flows that a login shell finds.
    Add the lines that make the tool available (``module load``, ``source``) to the ``setup`` of
    the host profile. ``check`` does not run them, but a launch does.

A launch that keeps running ends when you log out
    The host has ``KillUserProcesses=yes``. Ask the administrator to enable lingering for your
    user (``loginctl enable-linger``).

The run waits with "waiting for the workspace lock"
    Another command or a launch uses the same workspace. The message names the launch. Wait for it,
    follow it with ``xeda remote attach``, or stop it with ``xeda remote cancel``.

The host is full
    Run ``xeda remote clean HOST``, or set ``root`` in the host profile to a larger file system.

Errors
------

``RemoteConnectionError``
    OpenSSH could not connect or authenticate, or the connection ended. The message has the OpenSSH
    text and its exit status. Run ``ssh HOST`` by hand and fix the destination or your
    ``~/.ssh/config``. The error also appears when ``ssh`` is not on the ``PATH``, when ``ssh -G``
    cannot resolve the destination, and when the connection closes during a step.

``RemoteProvisionError``
    The environment could not be made. The message starts with a code that names the step:

    ``not-linux``
        The host does not run Linux.

    ``unsupported-platform``
        Xeda has no ``uv`` release for this CPU and C library, or it could not identify the C
        library. It supports x86_64 and aarch64 with glibc or musl.

    ``no-downloader``
        The host has neither ``curl`` nor ``wget``.

    ``no-sha256``
        The host has neither ``sha256sum`` nor ``shasum``, so Xeda cannot check the download.

    ``download-failed``
        A download or an unpack failed, or the downloaded ``uv`` does not run on the host. Check
        the internet access of the host, or set a proxy in ``env``.

    ``checksum-mismatch``
        The ``uv`` archive does not match its pinned SHA-256. A proxy may have changed it. Xeda
        does not use the file.

    ``python-failed``
        ``uv`` could not install the Python, or the ``python`` path of the profile is not an
        executable file.

    ``env-failed``
        ``uv`` could not make the environment or install the packages, or the host could not write
        to the remote root. The message has the output of the host.

    ``no-space``
        A step failed on a file system with less than 50 MiB free. Free space, or set another
        ``root``.

    ``lock-timeout``
        A process on another host has held a lock for more than 30 minutes. If that process is
        gone, remove the lock directory that the message names.

    ``bad-request``
        The host got a command that it does not understand. Report it as a bug in Xeda.

``RemoteDependencyError``
    A package of your Python environment cannot be installed on the host as it is here: it is an
    editable install, a local directory or a direct URL, or a package of ``tb.python_packages`` is
    not installed here. The error also appears when your Python is not CPython. The message names
    the package and the fix.

``RemoteIncompatible``
    The host resolved another plan than your machine did. The message lists the flows whose
    identity differs. Both sides run the same Xeda code on the same inputs, so their plans should
    agree. Compare ``xeda run ... --remote HOST --dry-run --json`` with a local ``--dry-run
    --json``. If the difference has no reason that you can see, report it as a bug.

``RemoteCanceled``
    The launch was canceled: by Ctrl-C, by ``xeda remote cancel`` or because the connection was
    lost. If the message says that processes survived SIGKILL, they may still run on the host. Run
    the command again to start over.

``RemoteInterrupted``
    Ctrl-C ended the run after the launch had finished. The results are on the host. Run the command
    again to fetch them. If Ctrl-C came while Xeda delivered ``--outputs-to``, the results are in the
    mirror, and the delivered files can be incomplete. Run the command again to deliver them.

``RemoteDetached``
    Xeda stopped following a launch that keeps running on the host (``--keep-running``). The
    message and the ``launch`` key of the JSON document give its id. Use ``xeda remote attach`` or
    ``xeda remote cancel``.

``RemoteFetchError``
    A file of the run was not fetched, or it did not match its digest, or a declared output is
    missing or is not a regular file. Run the command again: the finished run is reused and only
    the fetch repeats.

``RemoteConfigError``
    The file ``remotes.yaml`` or one of its entries is not valid, or the text of ``--remote`` is
    empty, starts with ``-`` or has a space. The message names the file, the host and the field.
    The error also appears when the remote root has a character that Xeda cannot put in a command
    line (a double quote, a backslash, ``$``, a backquote or ``!``). Set ``root`` to another path.

``RemoteProtocolError``
    The agent on the host did not start, or sent data that Xeda cannot read. The usual cause is a
    stream that ends before the agent marker, or more than 1 MiB of text before it. Ordinary text
    that a login profile prints is skipped and does no harm. The message includes the output of the
    host, which tells why the agent did not start.

A failure that is not one of these is a plain ``RemoteError`` with a message that says what to do.
Examples are a host without ``bash``, a chain with a programmer flow, and a native Windows machine.
