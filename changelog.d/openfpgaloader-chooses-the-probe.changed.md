- `openfpgaloader` chooses the probe whose FPGA matches the part when several probes fit the cable
  and the run names none. It fails before programming, listing each probe and its `usb_serial_num`,
  when no probe or several match, or when a probe cannot be read.
