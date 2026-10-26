******************
Reporting problems
******************

Please open an issue at https://github.com/XedaHQ/xeda/issues.

What to include
===============

Xeda records its decisions in the run directory, so the contents of the directory help to diagnose
a failed flow:

``settings.json``
    The input settings of the run (``flow_settings``: the defaults, the design and project
    sections, and the ``-s`` overrides, merged) and what the flow made of them
    (``effective_flow_settings``). The effective settings show what Xeda asked the tool to do.
    That is usually the first question.

``results.json``
    What Xeda parsed from the reports of the tool.

``trace.json``
    What the run consumed and produced. Xeda uses it to decide whether a later run is up to date.

The generated tool scripts and logs
    The ``.tcl``, ``.ys`` and ``.sdc``/``.xdc`` scripts that Xeda generated, and the log files of
    the tool.

Run the command again with ``--debug``. It turns on verbose logging, and it raises an exception
with a full traceback instead of reporting a failure. Both help in a bug report::

    xeda run <flow> <design> --debug

Also include:

* the output of ``xeda --version``
* the EDA tool and its version (``results.tools`` of ``xeda run --json`` lists the versions that
  Xeda detected for a node that ran)
* your operating system and Python version

Rejected settings
=================

Xeda rejects an unknown setting, so a typo is a ``FlowSettingsError``. A removed setting is
rejected too. Its error names the setting to use, or, when nothing replaces it, says why and to
delete it. If Xeda rejects a setting that you expected to work, compare the spelling with the
output of::

    xeda list-settings <flow>

This is a bug only if the setting is documented and Xeda still rejects it.
