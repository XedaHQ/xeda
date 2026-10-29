- `Workspace.load(run_dir)`, `Workspace.records()` and `RunRecord.open()` read stored runs. `open()`
  holds the files of a run under shared leases and checks that no later run replaced it.
  `Workspace.scrub(design_name=...)` removes the run directories of a design and, without a flow,
  its generation storages. A record now names the producer of each generated source.
