- A Git dependency is now fetched when Xeda plans a run, into `<run root>/.dependencies/`, and
  `commit` must be a full hash. The keys `clone_dir` and `local_cache` are removed; use a local
  dependency for a checkout that you manage.
