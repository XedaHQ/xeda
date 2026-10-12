*****
About
*****

**Xeda** automates simulation, synthesis and implementation with commercial and open-source EDA
tools. One declarative design file drives all of them: you switch tools by naming another flow.

The parts of Xeda
=================

Xeda keeps four concerns separate.

``Design``: what to build
    The data of a design file, which is YAML, TOML or JSON. It has an ``rtl`` section (sources,
    top module, parameters, clocks), a ``tb`` section (testbench sources, top module, cocotb
    settings) and, optionally, the settings of each flow. Xeda hashes the content of each source
    file. The hashes tell runs apart, and they let Xeda skip a run that is up to date. See
    :doc:`design-file`.

``Flow``: how to build
    A named sequence of steps that one or more tools perform: ``vivado_synth``, ``ghdl_sim``,
    ``openroad`` and so on. A flow declares its settings. It can take the outputs of other flows,
    its producers, and it parses the reports of the tools into results. See :doc:`flows`.

``Tool``: the executable
    An executable that a flow runs. It runs natively or in a Docker container. Flows ask for
    tools by name. With ``--remote``, Xeda runs the whole flow on another machine over SSH.

``FlowLauncher``: the orchestration
    Creates the flows, runs their producers first, manages run directories and the reuse of
    up-to-date runs, and collects the results. ``xeda run`` uses a launcher, and ``xeda dse``
    uses one that runs many flows in parallel. See :doc:`run-directories`.

Why the parts are separate
==========================

Design and flow are separate, so the same design file can be simulated with GHDL during
development, synthesized with Vivado for an FPGA, and taken through OpenROAD for an ASIC. Only the
flow name and a few flow settings change.

Flow and tool are separate, so the same flow can run with a locally installed tool, in a Docker
image, or on a remote machine over SSH.

Scripting and automation
========================

Every query command can write JSON, and ``xeda run`` can report a whole run as one JSON document
on stdout. See :doc:`machine-readable`. That page is also the reference for driving Xeda from a
coding agent.
