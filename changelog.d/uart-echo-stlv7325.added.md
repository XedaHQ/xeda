- Add a `stlv7325` target for the STLV7325 v2 to the Bluespec `uart_echo` example, and a transformed
  echo (`ECHO_TRANSFORM=1`) that a wire loopback cannot pass. The pins of the `stlv7325_v2` board
  file are checked on one board. The dependency group `examples` includes `pyserial` for the host
  test.
