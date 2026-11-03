- With `multiclock`, the `sby` flow now fails a design that reads its own clock as data, or that
  reads the state behind a state-driven clock, because SBY samples these wrongly. The default
  `depth` with `multiclock` is now 40, and `depth` 1 now fails for a design with state.
