Package xeda.introspect
=======================

These functions return plain data that can be serialized as JSON. They never touch the console.

.. autofunction:: xeda.introspect.flows_info

.. autofunction:: xeda.introspect.flow_info

.. autofunction:: xeda.introspect.settings_info

.. autofunction:: xeda.introspect.results_info

.. autofunction:: xeda.introspect.design_schema

.. autofunction:: xeda.introspect.boards_info

.. autofunction:: xeda.introspect.platforms_info

Except for ``design_schema``, which returns a JSON Schema, each function returns a mapping, or a
list of mappings, of a fixed shape. A class below describes each shape as a ``TypedDict``. A key marked ``NotRequired`` is absent from some entries.

.. autoclass:: xeda.introspect.FlowInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.FlowInputInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.FlowOutputInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.FlowRelation
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.FlowBinding
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.SettingsInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.SettingInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.ResultsInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.MetricDescription
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.ArtifactDescription
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.BoardInfo
   :members:
   :undoc-members:

.. autoclass:: xeda.introspect.PlatformInfo
   :members:
   :undoc-members:
