- `Spec`, `Plan`, `Result`, `SweepPoint` and `SweepResult` are explicitly not hashable: `hash()` on
  them raised `TypeError` before, and now their class says so.
