Package xeda
============

The module-level functions ``plan``, ``run``, ``sweep``, ``records``, ``load_record`` and ``scrub`` do
what the methods of :class:`~xeda.Workspace` do. They take the options of a workspace as keyword
arguments.

Designs and projects
--------------------

.. autoclass:: xeda.Design
   :members: load, with_updates, with_target, to_dict, name, root, target, targets, rtl, tb, flows, language, dependencies, description, authors, license, version, url, updates
   :undoc-members:

.. autoclass:: xeda.DesignPart
   :members: sources, parameters, defines
   :undoc-members:

.. autoclass:: xeda.Rtl
   :members: top, clocks, attributes, generator
   :undoc-members:

.. autoclass:: xeda.Testbench
   :members: top, uut, cocotb, python_packages
   :undoc-members:

.. autoclass:: xeda.Source
   :members: path, type, generated
   :undoc-members:

.. autoclass:: xeda.Project
   :members:
   :exclude-members: referenced_name

.. autoclass:: xeda.LocalSettings
   :members:

.. autofunction:: xeda.local_settings

What to launch
--------------

.. autoclass:: xeda.Spec
   :members:

Plan a launch
-------------

.. autoclass:: xeda.Plan
   :members:

.. autoclass:: xeda.PlanNode
   :members:

.. autoclass:: xeda.PlanInput
   :members:

.. autoclass:: xeda.PlanDelivery
   :members:

.. autofunction:: xeda.plan

Run a launch
------------

.. autoclass:: xeda.Workspace
   :members:

.. autoclass:: xeda.CancelToken
   :members:

.. autoclass:: xeda.Event
   :members:

.. autoclass:: xeda.Question
   :members:

.. autoclass:: xeda.Remote
   :members:

.. autoclass:: xeda.ScrubResult
   :members:

.. autofunction:: xeda.run

.. autofunction:: xeda.sweep

.. autofunction:: xeda.scrub

Results of a launch
-------------------

.. autoclass:: xeda.Result
   :members:

.. autoclass:: xeda.NodeResult
   :members:

.. autoclass:: xeda.Outcome
   :members:
   :undoc-members:

.. autoclass:: xeda.Disposition
   :members:
   :undoc-members:

.. autoclass:: xeda.DeliveryRecord
   :members:

.. autoclass:: xeda.SweepResult
   :members:

.. autoclass:: xeda.SweepPoint
   :members:

Records of earlier runs
-----------------------

.. autoclass:: xeda.RunRecord
   :members:

.. autoclass:: xeda.ErrorInfo
   :members:

.. autoclass:: xeda.InputRecord
   :members:

.. autoclass:: xeda.InputSource
   :members:

.. autoclass:: xeda.MetricInfo
   :members:

.. autoclass:: xeda.OutputRecord
   :members:

.. autoclass:: xeda.OpenRun
   :members:

.. autoclass:: xeda.RunListing
   :members:

.. autoclass:: xeda.RecordProblem
   :members:

.. autofunction:: xeda.records

.. autofunction:: xeda.load_record

The unset value
---------------

.. autoclass:: xeda.Unset
   :members:

.. autodata:: xeda.UNSET
   :no-value:
