- A flow whose producer ran again stays up to date when the outputs it takes from that producer
  are the same, so a source edit that does not change the netlist no longer re-runs `nextpnr`.
  Traces are format 15: each flow runs once more.
