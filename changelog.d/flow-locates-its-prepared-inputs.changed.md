- A flow that registers files in `init()` or `prepare_inputs()` must say where they are in
  `Flow.locate_prepared_inputs()`, which a dry run uses. A launch refuses a flow that does not.
