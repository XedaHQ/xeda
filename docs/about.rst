*****
About
*****

**Xeda** is a cross-platform, cross-EDA, cross-target simulation and synthesis
automation platform. One declarative description of a design drives flows across many commercial
and open-source EDA suites, so switching simulator, synthesizer or target does not mean rewriting
your scripts.

Four orthogonal abstractions
============================

Xeda keeps four concerns deliberately separate. Understanding the split is most of what is needed
to use it, and all of what is needed to extend it.

``Design`` - *what* to build
    A design description loaded from TOML, YAML or JSON. It carries an ``rtl`` section (sources,
    top module, parameters, clocks) and a ``tb`` section (testbench sources, top, cocotb
    settings), plus optional per-flow settings. Sources become file resources that carry a content
    hash, which is what lets Xeda tell runs apart and reuse cached ones. See
    :doc:`design-file`.

``Flow`` - *how* to build
    A named sequence of steps performed by one or more tools: ``vivado_synth``, ``ghdl_sim``,
    ``openroad``, and so on. A flow declares its own ``Settings``, may depend on other flows, and
    parses the tools' reports into results. See :doc:`flows`.

``Tool`` - the executable
    An abstraction over an executable, which may be installed natively, run inside a container, or
    run on a remote machine. Flows ask for tools by name and do not care how they are provided.

``FlowRunner`` - orchestration
    Instantiates flows, resolves and runs their dependencies, manages run directories and
    caching, and collects results. The default runner backs ``xeda run``; a design-space
    exploration runner backs ``xeda dse``. See :doc:`run-directories`.

Why the separation matters
==========================

Because *what* is separate from *how*, the same design file can be simulated with GHDL during
development, synthesized with Vivado for an FPGA target, and pushed through OpenROAD for an ASIC
target, with no change to the design description, only a different flow name and a handful of
flow settings.

Because *how* is separate from *the executable*, the same flow can run against a locally
installed tool, a Docker image, or a remote machine over SSH.

Scripting and automation
========================

Every informational command can emit JSON, and ``xeda run`` can report a whole run as a single
JSON document on stdout. See :doc:`machine-readable`; that page is also the reference for
driving Xeda from a coding agent.
