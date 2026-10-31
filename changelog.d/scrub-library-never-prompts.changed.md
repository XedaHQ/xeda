- The Python API no longer prompts when it scrubs: it removes nothing unless the caller passes a
  confirmation. `xeda run --remote --scrub` is now refused, because the run directories are on the
  host.
