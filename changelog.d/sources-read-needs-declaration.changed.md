- `Flow.sources_read()` now raises `TypeError` in a flow that declares no `reads_sources`, where it
  returned every source. A plugin flow that reads the design's sources declares the types it reads.
