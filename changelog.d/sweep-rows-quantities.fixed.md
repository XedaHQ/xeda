- `SweepResult.rows(quantities=True)` now converts a pint quantity of an `Optional` field to the
  unit of the field, and never returns your own object. With `quantities=False`, a quantity is
  quantity text.
