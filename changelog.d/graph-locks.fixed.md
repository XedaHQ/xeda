- Launches that share run directories or generation storages now lock them all in one order, so
  they cannot wait for each other. A delivery also locks its destinations, so two launches never
  write one file at once.
