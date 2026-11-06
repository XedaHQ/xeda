*****************
Install the tools
*****************

Xeda drives EDA tools. It does not contain them. A flow starts a program such as ``yosys``,
``ghdl`` or ``vivado``, and the flow fails with ``executable_not_found`` if Xeda cannot find that
program. This page tells you how to install the tools and how Xeda finds them.

Install the open-source tools
=============================

The OSS CAD Suite of YosysHQ is one download that holds Yosys, nextpnr, GHDL, Verilator, Icarus
Verilog, SymbiYosys and more. It is the easiest way to get the open-source flows of Xeda. Xeda
can install it for you:

.. code-block:: bash

    xeda tools install oss-cad-suite

The command shows what it downloads and where it installs it, and it asks you to confirm. Add
``--yes`` to skip the question. The download is about 500 MB to 1 GB, and the installed suite is
about 2 GB to 3 GB. Xeda checks the archive against the SHA-256 digest that GitHub lists for it
before it reads one file of the archive. If the check fails, nothing is installed.

By default, the command installs the build that this version of Xeda pins. CI tests Xeda against
the same build. Use ``--version`` to install another build, by its date or as ``latest``:

.. code-block:: bash

    xeda tools install oss-cad-suite --version 2026-10-20
    xeda tools install oss-cad-suite --version latest

``latest`` is looked up once, when you install. Nothing else looks up a newer build, and a run never
uses the network.

The suite goes into the *tool store*. By default the store is in the data directory of your
account: ``$XDG_DATA_HOME/xeda/tools`` (``~/.local/share/xeda/tools``) on Linux, and
``~/Library/Application Support/xeda/tools`` on macOS. Name another store with ``--root`` or with
the environment variable ``XEDA_TOOLS_ROOT``, which must be an absolute path. The store can hold
several builds, side by side.

Xeda supports Linux (x86-64 and arm64) and macOS (Intel and Apple silicon). On Windows, use WSL.

Look at the store
-----------------

.. code-block:: bash

    xeda tools list                      # the installed builds, and the build that Xeda pins
    xeda tools path oss-cad-suite        # the directory of the suite that runs use

``list`` marks the build that a run would use. ``path`` prints the directory of that suite; the
directory has ``bin``, ``lib`` and the ``environment`` script of the suite. Both commands only look
at the files. They start no program and use no network. Add ``--json`` for a machine-readable
document (see :doc:`machine-readable`).

Install the suite by hand
-------------------------

You can also install the suite yourself. Download the archive for your platform from the
`releases page <https://github.com/YosysHQ/oss-cad-suite-build/releases>`_ and extract it
anywhere. On macOS, a browser marks the download as quarantined. Remove the mark from the archive
before you extract it, or run ``./activate`` once in the extracted directory:

.. code-block:: bash

    xattr -d com.apple.quarantine oss-cad-suite-darwin-arm64-20261008.tgz
    tar -xzf oss-cad-suite-darwin-arm64-20261008.tgz

``xeda tools install`` downloads with Python, which sets no quarantine mark. Then put the ``bin``
directory of the suite on ``PATH`` (``export PATH=<where>/oss-cad-suite/bin:$PATH``), or name it
in the ``path`` of your private settings (see below). A suite that you install by hand is not in the
tool store, so Xeda does not choose it by itself.

Use a package manager
---------------------

A package manager works as well. On macOS, Homebrew has ``yosys``, ``ghdl``, ``verilator`` and
``icarus-verilog``:

.. code-block:: bash

    brew install yosys ghdl verilator icarus-verilog

Xeda finds the programs on your ``PATH``. A Homebrew ``yosys`` has no GHDL or SystemVerilog plugin,
unless you install them, so a flow that needs a plugin fails with a message that says so. The OSS
CAD Suite has the plugins.

Install a commercial tool
=========================

Xeda does not install Vivado, Quartus, Design Compiler, ModelSim or VCS. Install them as their
vendors tell you, and make the programs visible to Xeda. Most vendors give a script that sets up
the environment. Run it in the shell that starts Xeda:

.. code-block:: bash

    source /opt/Xilinx/Vivado/2025.2/settings64.sh      # Vivado, xsim
    export PATH="$HOME/intelFPGA_pro/25.1/quartus/bin:$PATH"   # Quartus

Or put the directory of the programs in ``path`` of your private settings, so that you need no
script. A license is a separate matter: set the variable that the vendor names, for example
``XILINXD_LICENSE_FILE`` or ``LM_LICENSE_FILE``, in your shell or in ``env`` of your private
settings. :doc:`design-file` describes both.

How Xeda finds a program
========================

A flow asks for a program by its name, and Xeda looks for that name on a ``PATH`` that it builds
for each node of a run. The directories of that ``PATH`` come in this order:

1. The directories of ``path`` in your private settings. The project-local file comes before the
   user file.
2. The ``bin`` directory of the OSS CAD Suite in the tool store: the newest installed build, or the
   one that ``tools`` in your private settings selects.
3. The ``PATH`` of the shell that started Xeda (or ``PATH`` in ``env`` of your private settings).

The first directory that has the program wins. So a program in ``path`` beats the suite, and the
suite beats your shell. If you install nothing in the tool store, Xeda adds nothing, and the
programs come from your shell as before.

A flow setting ``env: {PATH: ...}`` replaces the whole ``PATH``, directories in front included. A
flow that runs in Docker gets none of these directories: the programs are the ones in the image.

Private settings
----------------

The two keys live in the private settings files, with ``env`` and ``run_root``
(see :ref:`private-files`):

.. code-block:: yaml

    path:
      - ~/tools/vivado/bin
      - /opt/quartus/bin
    tools:
      oss-cad-suite: 2026-10-08

``path``
    A list of directories. A relative entry is relative to the file. A leading ``~`` or ``~/`` is
    your home directory. Xeda expands no variable and no glob. An entry that is not an existing
    directory is an error (``invalid_local_settings``).
``tools``
    ``oss-cad-suite: off`` turns the automatic suite off: Xeda adds nothing to ``PATH``. A date,
    such as ``2026-10-08``, selects that build. It must be installed. Otherwise the run is refused
    before any tool starts, with the error ``tool_not_installed``. Without this key, the newest
    installed build is used, so an upgrade of Xeda never leaves an installed suite unused.

The two keys need no environment variable. Install a build with ``xeda tools install``, and it is
used by the next run.

Directories and builds affect whether a run is up to date, never what it is. If you add a
directory, or install a newer suite, a node runs again, because it may find another program. The
settings and the design of the node are unchanged, so its run directory is the same.

The suite changes only ``PATH``. Xeda does not run the ``environment`` script of the suite, and
it sets no Python, library or ``VIRTUAL_ENV`` variable. Your Python environment and the Python of
Xeda stay as they are.

With ``--remote``, the host uses its own tool store and its own settings. Install the suite on the
host with ``xeda tools install`` there.
