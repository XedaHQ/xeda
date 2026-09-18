**********************
Reporting problems
**********************

Please open an issue at https://github.com/XedaHQ/xeda/issues.

What to include
===============

A flow failure is much easier to diagnose with the run directory's contents than without, because
Xeda records everything it decided there:

``settings.json``
    The run's input settings (``flow_settings``: defaults, design and project sections and ``-s``
    overrides, merged) and what the flow made of them (``effective_flow_settings``). The latter
    answers "what did Xeda actually ask the tool to do", which is usually the first question.

``results.json``
    What was parsed back out of the tool's reports.

The generated tool scripts and logs
    The ``.tcl``, ``.ys``, ``.sdc``/``.xdc`` scripts Xeda generated, and the tool's own log files.

Re-running with ``--debug`` turns on verbose logging and re-raises exceptions with a full
traceback instead of reporting a failure, which is what you want in a bug report::

    xeda run <flow> <design> --debug

Also include:

* the output of ``xeda --version``
* the EDA tool and its version (``results.json`` records the versions Xeda detected)
* your operating system and Python version

Settings that are rejected
==========================

Xeda rejects unknown settings rather than ignoring them, so a typo surfaces as a
``FlowSettingsError`` instead of silently doing nothing. If a setting you expected is rejected,
check the exact spelling against::

    xeda list-settings <flow>

That is not a bug unless the setting is documented and still rejected.
