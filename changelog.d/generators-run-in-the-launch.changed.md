- A design generator runs in the launch, not when the design loads, and writes into
  `<run root>/.generated/<design>/<key>/` (`$XEDA_GENERATED`), never into the design tree. A plan
  runs no generator, and `--remote` runs it on the host.
