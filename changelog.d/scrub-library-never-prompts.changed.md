- The Python API no longer prompts when it scrubs: it removes nothing unless the caller passes a
  confirmation. `xeda run --remote --scrub` is now refused, because a remote launch cannot ask.
