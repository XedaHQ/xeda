- The library no longer changes the working directory of the Python process during a launch, so
  two threads, each with its own launcher, can launch flows at the same time in one process.
