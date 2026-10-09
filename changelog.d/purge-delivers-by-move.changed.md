- With `--post-cleanup-purge`, a delivered output is moved out of the run directory, not copied,
  when it can be: a plain file on one file system that no other delivery uses. A large output then
  needs no second copy on disk.
