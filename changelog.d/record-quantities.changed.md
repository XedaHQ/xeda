- `RunRecord.quantity(path)` returns one `pint` quantity and raises `ValueError` for a metric of the
  type `numbers`. Use the new `RunRecord.quantities(path)` for such a metric.
