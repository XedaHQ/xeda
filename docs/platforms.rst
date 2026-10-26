**************
ASIC platforms
**************

An ASIC flow works on one platform. This page explains what a platform is, where its values come
from, how to install the PDK files it needs, and how to define a platform of your own.

``xeda list-platforms`` lists the platforms that Xeda ships.

What a platform is
==================

A platform is a standard cell library together with its technology files. An example is
``nangate45`` (FreePDK45 with the Nangate Open Cell Library). The flows that map a design to a
platform (today the ``yosys`` flow) need three things from it:

- the files of the PDK: the Liberty, LEF and GDS files, the maps for the synthesis tool, and more;
- facts about the library, such as the site of the cells, the tie cells and the cells that must not
  be used;
- numbers for the back end, such as the routing layers and the power grid.

Xeda does not ship the PDK files. It ships a short file for each platform that says where each
file is, and it reads the facts from the PDK files themselves. The PDK files live in a *PDK store*
on your machine.

You select a platform with the setting ``platform``, which takes the name of a bundled platform:

.. code-block:: bash

    xeda run yosys design.yaml -s platform=nangate45

Use ``platform_file`` for a platform that Xeda does not ship (see `A platform of your own`_).

Bundled platforms
=================

Xeda ships two platforms:

``nangate45``
    FreePDK45 with the Nangate Open Cell Library. The ``orfs`` provider serves it. The provider
    takes the platform directory of OpenROAD-flow-scripts at the commit
    ``6b305a712966be72d8908695c04398acdbe273ca``. The corner is ``typical``, and ``tt`` is an alias
    of it.

``sky130hd``
    SkyWater SKY130 with the high density standard cell library (``sky130_fd_sc_hd``). The
    ``ciel`` provider serves it. The provider takes the open_pdks build at the commit
    ``8afc8346a57fe1ab7934ba5a6056ea8b43078e71``, for the family ``sky130`` and the variant
    ``sky130A``. The default corner is ``tt_025C_1v80``, and ``tt`` is an alias of it. The other
    corners are ``ss_100C_1v60`` and ``ff_n40C_1v95``.

    The dont-use cells come from ``drc_exclude.cells`` in the open_pdks configuration of the
    library, ``libs.tech/openlane/sky130_fd_sc_hd``. That is the file that ``DRC_EXCLUDE_CELL_LIST``
    names. Xeda reads the file when it runs a flow and does not copy it. Install the files of
    ``sky130hd`` with ``xeda pdk install sky130hd``, which downloads about 128 MiB (see
    `The PDK store`_).

    The ``lambdapdk`` provider serves ``sky130hd`` too (see `The lambdapdk provider`_). Select it
    with ``pdk_provider: lambdapdk``.

The four layers
===============

Xeda builds the *platform model* from four layers. A layer gives values by name. A later layer
wins at each value:

1. **derived**. Xeda reads the LEF and Liberty files of the PDK and takes what they state: the
   site, the tie cells, the antenna cell, the supply pins and voltage, the routing tracks, the
   cells that the library marks ``dont_use``, and the units. This needs no tool. A fact that the
   files do not state gives no value, and Xeda never guesses.
2. **upstream**. Xeda reads the configuration file that the PDK or OpenROAD-flow-scripts ships for
   the platform: the ``config.mk`` of a platform of OpenROAD-flow-scripts, or the ``config.tcl``
   of open_pdks and IHP. Xeda reads these files as text. It runs no Make and no Tcl, and it
   ignores a line with a key it does not know.
3. **residue**. The small file that Xeda ships for the platform, ``platform.yaml``. It names the
   providers, the corners and the files, and gives only the values that the first two layers do
   not give.
4. **user**. Your own residue file, ``platform_file``. It has the same form as the residue file.

A flow that lacks a value it needs refuses to run. The error names the value and the layers that
Xeda looked in. The ``results`` of ``xeda run --json`` have a ``platform`` entry with the name, the provider, the
version and the corner, and ``sources``, which tells for each value the layer that gave it.

The residue file
================

The residue file of ``nangate45`` is a good example. This is the file with some values left out:

.. code-block:: yaml

    name: nangate45
    description: FreePDK45 with the Nangate Open Cell Library
    providers:
      default: orfs
      orfs:
        version: 6b305a712966be72d8908695c04398acdbe273ca
        directory: flow/platforms/nangate45
        download_mib: 1.7
      directory: {layout: orfs}
    upstream: {adapter: orfs_config_mk, files: [config.mk]}
    corners: {default: typical, aliases: {tt: typical}}
    files:
      liberty: {typical: [lib/NangateOpenCellLibrary_typical.lib]}
      tech_lef: lef/NangateOpenCellLibrary.tech.lef
      cell_lef: [lef/NangateOpenCellLibrary.macro.mod.lef]
      adder_map: cells_adders.v
    values:
      endcap_cell: TAPCELL_X1
      tapcell: {distance: 120}
      routing: {layer_adjustments: {metal2-metal3: 0.5, metal4-metal10: 0.25}}
    overrides:
      tracks.metal1:
        value: {x: [0.095, 0.19], y: [0.07, 0.14]}
        reason: the LEF gives one pitch per layer; ORFS sets x and y

The keys are:

``name``, ``description``
    The name of the platform and one line about it.

``providers``
    The providers that serve the platform. ``default`` names the one that Xeda uses when you name
    none. Each provider has its own entry (see `Providers and layouts`_). A provider without an
    entry does not serve the platform. The entry of a Git provider (``orfs``, ``lambdapdk``) may
    list ``fetch``, the paths below its ``directory`` that the install fetches. The entry of
    ``lambdapdk`` may also hold ``files``, ``upstream`` and ``values`` for this provider. Its
    ``files`` and ``upstream`` take the place of the top-level keys of the same name, and its
    ``values`` override the top-level ``values`` leaf by leaf (see `The lambdapdk provider`_).

``upstream``
    The adapter (``orfs_config_mk`` or ``open_pdks_config``) and the configuration files it reads.
    The paths are relative to the directory of the platform in the store.

``corners``
    The default corner and the aliases. The known corners are the keys of ``files.liberty``. A flow
    works on one corner, which ``corner`` selects. ``tt`` is an alias of ``typical`` above.

``files``
    The files of the platform by role, as paths relative to the directory of the platform in the
    store. The roles are ``liberty`` (a table: corner, then the files of that corner), ``tech_lef``,
    ``cell_lef``, ``gds``, ``rcx_rules``, ``cdl``, ``klayout_tech``, ``klayout_layer_props``,
    ``latch_map``, ``clkgate_map`` and ``adder_map``. A role that holds one file is a string, and a role that
    holds several files is a list.

``shipped``
    Only a bundled platform has this key. It maps the role of a Yosys techmap (``latch_map``,
    ``clkgate_map`` or ``adder_map``) to the name of a file that Xeda ships beside the residue
    file. Use it for a map that the PDK does not have. The file is part of Xeda and not of the PDK
    store, so the install does not fetch it, and Xeda locates it in the package. A flow reads it
    as any other platform file, and its content is part of the identity of the run. A role in
    ``files`` replaces the shipped file of the role. ``sky130hd`` ships ``cells_clkgate.v``, the
    clock gate map, because neither ``open_pdks`` nor ``lambdapdk`` has one. A residue file of
    your own cannot ship files: it names its files in ``files``.

``values``
    The values that the derived and upstream layers do not give, as nested YAML.

``overrides``
    A value that a lower layer gives and that this platform sets differently. The key is the
    dotted name of the value, and each override needs a ``reason``.

The tests of Xeda keep the bundled residue files minimal. They refuse a value that a lower layer
gives already, and ask for the line to be deleted. They refuse a differing value in ``values`` and
ask for it to be moved to ``overrides`` with a reason. They also refuse an override that no lower
layer gives.

A platform of your own
======================

A residue file of your own has the same keys. Give it to a flow with ``platform_file``. A relative
path is relative to the design root.

A file with ``extends`` changes a bundled platform. The bundled file is the residue layer, and
your file is the user layer. Xeda takes the providers, the upstream files, the corners and the
files from your file where it gives them, and from the bundled platform where it does not. Your
``values`` and ``overrides`` win over every other layer:

.. code-block:: yaml

    name: nangate45_dense
    extends: nangate45
    description: nangate45 with a higher placement density
    values:
      place_density: 0.7

A list in ``values`` replaces the whole list of a lower layer. A table such as ``tracks`` or
``wire_rc.layers`` merges key by key.

A file without ``extends`` is a complete platform. It must name a default provider with its entry,
at least one Liberty corner and the default corner. This file describes a library that sits in
the directory ``<pdk_root>/mylib`` (the ``directory`` provider, layout ``orfs``):

.. code-block:: yaml

    name: mylib
    description: The standard cells of my lab
    providers:
      default: directory
      directory: {layout: orfs}
    corners: {default: typical}
    files:
      liberty: {typical: [mylib.lib]}
      tech_lef: mylib.tech.lef
      cell_lef: [mylib.lef]
    values:
      min_buffer: {cell: BUF_X1, input: A, output: Z}

Select it with the file alone. If you also give ``platform``, it must be the ``name`` in the file:

.. code-block:: bash

    xeda run yosys design.yaml -s platform_file=mylib.yaml -s pdk_root=/data/pdks

What the OpenROAD flows take from a platform
============================================

The OpenROAD flows (see :doc:`flows`) read these values and files of the platform. Each value
has a setting that replaces it for one run. To change it for every run, give a residue file of
your own (`A platform of your own`_).

.. list-table::
   :header-rows: 1

   * - Platform value or file
     - What the flows do with it
     - The setting that replaces it
   * - ``files.liberty``, ``files.tech_lef``, ``files.cell_lef``, ``time_unit``
     - Every process reads them. The flows convert each time to the time unit of the Liberty
       files.
     -
   * - ``site``, ``tracks``
     - The floorplan makes the rows of this site and the routing tracks.
     - ``floorplan.site``, ``floorplan.tracks``
   * - ``tap_cell``, ``endcap_cell``, ``tapcell``
     - The tap cells and end caps, and the distance between the tap cells.
     - ``tapcell.master``, ``tapcell.endcap_master``, ``tapcell.distance``
   * - ``pdn``
     - The power grid: the supply nets, the rails, the straps and their connections.
     - ``pdn.grid``
   * - ``io_layers``
     - The layers of the pins of the design.
     - ``io.horizontal_layers``, ``io.vertical_layers``
   * - ``place_density``
     - The target density of the global placement.
     - ``global_placement.density.value``
   * - ``routing``
     - The routing layers of the signal and clock nets, and the capacity that the global route
       keeps free on each layer. The database keeps them for the later flows.
     - ``routing.signal_layers``, ``routing.clock_layers``, ``routing.layer_adjustments``
   * - ``wire_rc``
     - The resistance and capacitance of the wires, for the estimates before the detailed
       route. Each flow sets them, since the database does not keep them.
     - ``wire_rc``, leaf by leaf
   * - ``tie_high``, ``tie_low``
     - The cells that drive the constant nets.
     -
   * - ``dont_use_cells``
     - The cells that the resizer does not use.
     - ``dont_use_cells`` adds to them
   * - ``antenna_cell``, ``diode_usable``
     - The diode of the antenna repair. Without a usable diode, the flows skip the repair and
       say so in the log, and the check still runs.
     - ``antenna.diode_cell``
   * - ``fill_cells``
     - The filler cells that the last flow places.
     - ``fill.cells``
   * - ``files.rcx_rules``
     - The rules of OpenRCX. Without them, the last flow estimates the parasitics.
     - ``extraction.method``
   * - ``supply``
     - The voltages of the supply nets for the analysis of the IR drop.
     - ``ir_drop.nets``
   * - ``files.cdl``, ``files.gds``, ``files.klayout_tech``
     - The CDL netlist ``final.cdl``, and the layout ``final.gds`` that KLayout writes with
       ``write_gds``.
     -

A setting that is unset takes the value of the platform. A residue file that leaves out a value
that a flow needs makes the flow fail before OpenROAD starts, and the message names the value.

Providers and layouts
=====================

A provider maps the role names of the residue file to the files in the PDK store. It uses path
arithmetic only: finding the files starts no process and uses no network. The ``pdk_provider``
setting selects the provider. The default is the ``default`` of the residue file.

.. list-table::
   :header-rows: 1
   :widths: 14 56 30

   * - Provider
     - Directory of the platform
     - Version (the pin)
   * - ``orfs``
     - ``<root>/xeda/orfs/<commit>/<directory>``, the platform directory of a sparse checkout of
       OpenROAD-flow-scripts that ``xeda pdk install`` writes. ``<directory>`` comes from the
       residue file, for example ``flow/platforms/nangate45``. Several platforms can share one
       commit, each in its own directory. The store keeps no ``.git`` for it.
     - The commit of OpenROAD-flow-scripts.
   * - ``ciel``
     - ``<root>/ciel/<family>/versions/<hash>/<variant>``, as ``ciel`` writes it. For
       ``sky130hd``, the family is ``sky130`` and the variant is ``sky130A``. The role paths are
       relative to this directory (``libs.ref/<library>/...``, ``libs.tech/...``).
     - The hash of the PDK build.
   * - ``lambdapdk``
     - ``<root>/xeda/lambdapdk/<commit>/<directory>``, a directory of a sparse checkout of the
       lambdapdk repository that ``xeda pdk install`` writes. For ``sky130hd``, ``<directory>``
       is ``lambdapdk/sky130``, and the install fetches only ``base`` and ``libs/sky130hd`` of it.
       The role paths are relative to this directory.
     - The commit of lambdapdk.
   * - ``directory``, layout ``orfs``
     - ``<root>/<platform>``. The root is the ``flow/platforms`` directory of an
       OpenROAD-flow-scripts checkout, or a copy of it.
     - ``pdk_version`` as you give it, else ``unpinned``.
   * - ``directory``, layout ``open_pdks``
     - ``<root>/<variant>``. The role paths are relative to this directory.
     - ``pdk_version`` as you give it, else ``unpinned``.

``<root>`` is the PDK store, ``pdk_root``.

Xeda checks the version of a ``directory`` against what the directory itself says, when it says:
the ``.git/HEAD`` of an OpenROAD-flow-scripts checkout, or the ``SOURCES`` file or the
``.config/nodeinfo.json`` of an ``open_pdks`` build. A version that does not match is an error.
With ``unpinned``, Xeda checks nothing. The recorded digests of the files still make a run stale
when a file changes.

The PDK store
=============

These commands work with the store. The ``xeda pdk`` commands take ``--root`` to name another
store than ``~/.ciel``.

.. code-block:: bash

    xeda pdk install nangate45          # download the PDK files that the platform pins
    xeda pdk install sky130hd           # download the open_pdks files of sky130hd with ciel
    xeda pdk install sky130hd --provider lambdapdk   # the lambdapdk files of sky130hd
    xeda pdk list                       # each platform and provider, the pin, and whether installed
    xeda pdk path nangate45             # the directory of the platform in the store
    xeda list-platforms                 # the bundled platforms and whether they are installed

``xeda pdk install`` is the only command that uses the network and writes to the store. It shows
what it downloads, an estimate of the size (when the platform gives one), where it installs and
the commands it runs, and it asks before it starts. ``--yes`` skips the question. Machine-readable
output (``--json``, ``--format json``, ``--format jsonl`` and ``--format yaml``) also installs
without asking, as ``--yes`` does. ``--platform-file`` installs the PDK of a platform of your own.
The install is safe to repeat: when the store holds the files already, the command says so and
does nothing. The other commands only look at the files.

An ``orfs`` or ``lambdapdk`` install fetches into a partial directory in the store, checks every
file of every corner, writes the receipt and then moves the directory of the platform to its place
in one step. If anything fails, the store is as it was. The command refuses to install over a platform directory that is there
but incomplete, and tells you to move it away. A ``ciel`` install runs ``ciel``, which writes into
the store itself, so a failed install may leave files of ``ciel`` behind. See
:doc:`machine-readable` for their JSON output.

The install refuses a symbolic link inside the store: in ``<root>/xeda`` and below it, anywhere
on the way to a file or directory that it writes, moves or removes. The message names the link and
where it points. The install writes only real directories inside the store, so that it never
writes or deletes outside it. The root itself (``--root`` or ``~/.ciel``) may be a link. The
install resolves it first. A link under ``<root>/ciel`` is not a concern of the install: ``ciel``
writes that directory.

``xeda list-platforms`` looks in the default store. Use ``xeda pdk list --root <directory>`` for
another store.

Settings of the PDK
-------------------

``pdk_root``
    The directory of the PDK store. The default is ``~/.ciel``. It tells Xeda where the files are,
    so it is no part of the identity of a run: the run depends on the content of the files that it
    reads. If you move the store and keep the same files, the run directory stays the same and the
    run becomes stale, so Xeda runs the flow again. It does not start a different run. A relative
    path is relative to the design root. Xeda reads no environment variable to find the store,
    except ``HOME`` for the default.

``pdk_provider``
    The provider (``orfs``, ``ciel``, ``lambdapdk`` or ``directory``). The default is the default
    provider of the platform.

``pdk_version``
    The version of the PDK files, the pin: a full commit hash for ``orfs`` and ``lambdapdk``, the
    hash of the build for ``ciel``. It is part of the identity of the run. When you give
    none, Xeda fills it in from the platform before it hashes anything: the commit or the hash of
    the residue file, or ``unpinned`` for a ``directory``. A flow chain agrees on one platform
    and one pin along every edge (see :doc:`flows`).

When the PDK files are missing
==============================

Before a flow runs, it looks for the PDK files with ``stat``. It stops if one is missing. The
message says what to do:

.. code-block:: text

    platform 'nangate45' needs the PDK files of flow/platforms/nangate45 (orfs 6b305a712966).
      looked in: /home/me/.ciel/xeda/orfs/6b305a712966be72d8908695c04398acdbe273ca/flow/platforms/nangate45 (missing: lib/NangateOpenCellLibrary_typical.lib, and 10 more)
      install:   xeda pdk install nangate45
      or name another location: -s pdk_root=<directory>

Install the platform with the command in the ``install`` line, or give ``pdk_root`` the store that
holds the files. With ``--dry-run`` the plan shows the node as unknown, with the same message,
since the plan cannot say whether the run is fresh.

A store that holds another version than the pin fails with a message that names both. Change
``pdk_version``, install the pinned version, or name another store.

The lambdapdk provider
======================

lambdapdk (https://github.com/siliconcompiler/lambdapdk) is the PDK package of SiliconCompiler. It
holds PDK files in a Git repository. Xeda uses it as a source of files only. It starts nothing from
lambdapdk and imports no Python package of it. ``xeda pdk install sky130hd --provider lambdapdk``
fetches the commit that the platform file pins, with a sparse Git checkout of the two directories
that ``sky130hd`` needs. That is about 8 MiB. The pin is a commit hash, not a tag. The comment in
``platform.yaml`` names the tag (``v0.2.22``).

lambdapdk is less stable than ``ciel``. Its releases come often, and a release can move files.
So the pin changes only with a reviewed change of ``platform.yaml``: replace the commit, replace
the tag in the comment, and run the conformance tests (``XEDA_TESTS_PDK_ROOT``,
``XEDA_TESTS_PLATFORMS`` and ``XEDA_TESTS_PDK_PROVIDERS=sky130hd:lambdapdk``) against the new
commit. ``pdk_version`` selects another commit for one run.

The layout and the values of lambdapdk differ from those of ``ciel``, so the ``lambdapdk`` entry of
``sky130hd`` gives its own ``files`` and ``values``:

- The Liberty files are ``.lib.gz``. yosys and OpenROAD read them as they are.
- The corners are ``tt_025C_1v80``, ``ss_n40C_1v40`` and ``ff_100C_1v95``. A corner of ``ciel``
  only, such as ``ss_100C_1v60``, is refused when you select ``lambdapdk``, and the message names
  the corners of lambdapdk. The default corner and the alias ``tt`` are the same for both providers.
- lambdapdk has no configuration file that Xeda can read. Its entry gives the values that
  lambdapdk states in its own configuration: the tap cell and its distance (14 um), the end cap
  ``decap_4``, the antenna diode, the smallest buffer and the driver cell of the technology
  mapping, the filler cells, the clock buffers, the dont-use cells, the routing layers and the
  global-route derates, the layers of the pins and the layers for the wire estimates, and the
  power rails and straps. ``NOTICE.md`` in the platform directory of the Xeda package names the
  source of each value.
- The power grid has fixed pitches (27.14 um on ``met4`` and 27.2 um on ``met5``). These are the
  upper limits of the pitches that lambdapdk computes from the size of the core. A core smaller
  than 54 um gets fewer straps than lambdapdk would give. Give ``pdn.grid`` to change the grid.
- Xeda does not copy the measured resistance and capacitance of each layer: the units of lambdapdk
  are those of its own flow scripts. OpenROAD uses the values of the technology LEF, as it does
  with ``ciel``. The power nets keep the names of ``ciel`` (``VPWR`` and ``VGND``), and the body
  pins ``VPB`` and ``VNB`` stay connected.

So a run on lambdapdk files differs from a run on ``ciel`` files in the end cap (``decap_4`` instead
of ``decap_3``), the tap distance (14 instead of 13 um), the dont-use and clock buffer lists, the
filler cells and the global-route derates. The two runs have different identities: the provider and
the version are part of the settings.

lambdapdk also holds the Nangate library, under the Nangate Open Cell Library License. That license
says that the library is not intended for commercial use and prohibits benchmarking it against
another library. Xeda offers ``nangate45`` from ``orfs`` only, and the license of the Nangate files
is an open question (see `Licenses`_).

Licenses
========

Xeda ships no Nangate file and no other PDK library file. The residue files hold only file names
and numbers. ``xeda pdk install nangate45`` fetches the files of the Nangate Open Cell Library
from OpenROAD-flow-scripts, at the commit that the residue file pins, and only when you ask for
it. ``xeda pdk install sky130hd`` runs ``ciel``, which fetches the open_pdks build of SKY130 into
the store, and only when you ask for it. ``xeda pdk install sky130hd --provider lambdapdk``
fetches files of the lambdapdk repository, and only when you ask for it. The files stay under
their own license. Read the license of a PDK before you use it. The file ``NOTICE.md`` in the
platform directory of the Xeda package lists the sources of the values that the residue files take
from OpenROAD-flow-scripts, from open_pdks and from lambdapdk.

Checking a platform against OpenROAD-flow-scripts
=================================================

The repository tool ``tools/asic_parity.py`` compares the ASIC flow of Xeda with
OpenROAD-flow-scripts (ORFS) for one design on a platform. It runs the design through ORFS and
through Xeda with equivalent settings, then compares the results. The tool is part of the source
checkout, not of the installed package. See ``tools/asic_parity/README.md`` for how to run it.
