- A dockerized flow now mounts the directories of its input files read-only, so it reads the files
  of its producers without a `Docker.mounts` entry.
