- `yosys` takes more options for the flip-flops, latches and clock gates of a standard-cell
  mapping, in the group `sequential`. They cover a flip-flop map, clock-gate inference, retiming of
  chosen modules and tri-state conversion. `sequential.latch_map` and `sequential.clockgate_map`
  can now turn their map off with `none`.
