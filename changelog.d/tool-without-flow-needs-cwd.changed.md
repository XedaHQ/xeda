- A `Tool` that runs for no flow now needs an absolute `cwd=` each time it starts a process, and so
  does `Docker.run`. Xeda no longer falls back to the current directory of the Python process.
