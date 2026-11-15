Package xeda.errors
===================

Every error has a stable ``code``, a lower snake case text. :class:`~xeda.errors.SpecError` and its
subclasses mean that the request is wrong and nothing ran. The other errors mean that a launch, a
record, a lease or a remote host failed. The codes are in :doc:`../machine-readable`.

.. module:: xeda.errors

.. autoexception:: xeda.errors.XedaError
   :members:

.. autoexception:: xeda.errors.SpecError
   :members:

.. autoexception:: xeda.errors.SettingsError
   :members:

.. autoexception:: xeda.errors.DesignError
   :members:

.. autoexception:: xeda.errors.ProjectError
   :members:

.. autoexception:: xeda.errors.FlowNotFoundError
   :members:

.. autoexception:: xeda.errors.ChainError
   :members:

.. autoexception:: xeda.errors.RunRootError
   :members:

.. autoexception:: xeda.errors.StalePlanError
   :members:

.. autoexception:: xeda.errors.StaleRuntimeError
   :members:

.. autoexception:: xeda.errors.RecordError
   :members:

.. autoexception:: xeda.errors.RunReplacedError
   :members:

.. autoexception:: xeda.errors.RemoteError
   :members:

.. autoexception:: xeda.errors.RunFailedError
   :members:

.. autoexception:: xeda.errors.LeaseHeldError
   :members:
