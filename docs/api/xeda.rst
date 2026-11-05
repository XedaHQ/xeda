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
   :members: path, type, generated, output_name
   :undoc-members:

.. autoclass:: xeda.Project
   :members:
   :undoc-members:

.. autoclass:: xeda.LocalSettings
   :members:
   :undoc-members:

.. autofunction:: xeda.local_settings

What to launch
--------------

.. autoclass:: xeda.Spec
   :members:
   :undoc-members:

Plan a launch
-------------

.. autoclass:: xeda.Plan
   :members:
   :undoc-members:

.. autoclass:: xeda.PlanNode
   :members:
   :undoc-members:

.. autoclass:: xeda.PlanInput
   :members:
   :undoc-members:

.. autoclass:: xeda.PlanDelivery
   :members:
   :undoc-members:

.. autofunction:: xeda.plan

Run a launch
------------

.. autoclass:: xeda.Workspace
   :members:
   :undoc-members:

.. autoclass:: xeda.CancelToken
   :members:

.. autoclass:: xeda.Event
   :members:
   :undoc-members:

.. autoclass:: xeda.Question
   :members:
   :undoc-members:

.. autoclass:: xeda.Remote
   :members:
   :undoc-members:

.. autoclass:: xeda.ScrubResult
   :members:

.. autofunction:: xeda.run

.. autofunction:: xeda.sweep

.. autofunction:: xeda.scrub

Results of a launch
-------------------

.. autoclass:: xeda.Result
   :members:
   :undoc-members:

.. autoclass:: xeda.NodeResult
   :members:
   :undoc-members:

.. autoclass:: xeda.Outcome
   :members:
   :undoc-members:

.. autoclass:: xeda.Disposition
   :members:
   :undoc-members:

.. autoclass:: xeda.DeliveryRecord
   :members:
   :undoc-members:

.. autoclass:: xeda.SweepResult
   :members:
   :undoc-members:

.. autoclass:: xeda.SweepPoint
   :members:
   :undoc-members:

Records of earlier runs
-----------------------

.. autoclass:: xeda.RunRecord
   :members:
   :undoc-members:

.. autoclass:: xeda.ErrorInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.InputRecord
   :members:
   :undoc-members:

.. autoclass:: xeda.InputSource
   :members:
   :undoc-members:

.. autoclass:: xeda.MetricInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.OutputRecord
   :members:
   :undoc-members:

.. autoclass:: xeda.OpenRun
   :members:
   :undoc-members:

.. autoclass:: xeda.RunListing
   :members:
   :undoc-members:

.. autoclass:: xeda.RecordProblem
   :members:
   :undoc-members:

.. autofunction:: xeda.records

.. autofunction:: xeda.load_record

The unset value
---------------

.. autoclass:: xeda.Unset
   :members:
   :undoc-members:

.. autodata:: xeda.UNSET
   :no-value:
