- `Design.load`, `Design(...)`, `with_updates` and `with_target` now raise errors that are
  `xeda.errors.DesignError`, so `except xeda.errors.XedaError` catches a bad design.
