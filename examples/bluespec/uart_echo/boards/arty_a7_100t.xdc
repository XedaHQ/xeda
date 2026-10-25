# Arty A7-100T (Rev. D/E) pins for the uart_echo top, mkUartEcho.
#
# Port names are those of the top module: CLK, rxd, txd, led[3:0], sw[3:0], btn.
# Pins and standards are from the Digilent master XDC (Arty-A7-100-Master.xdc) and match
# src/xeda/data/boards/arty/arty_a7_100t.xdc. Only the port names differ.
#
# Clock: there is no create_clock here on purpose. The flow's clock setting (clock.port: CLK and
# clocks.main_clock.period: 10 ns) writes the constraint, and a clock with two constraints is an error.
#
# Polarity: the Arty buttons and LEDs are active high. The switches are plain levels.
#
# Pins only, one set_property per port, because openXC7's nextpnr reads nothing else (and expands
# no wildcards). The synchronizer and timing constraints of Vivado are in arty_a7_100t_sync.xdc.

## Clock, 100 MHz oscillator
set_property PACKAGE_PIN E3 [get_ports {CLK}]
set_property IOSTANDARD LVCMOS33 [get_ports {CLK}]

## USB-UART (channel B of the FT2232HQ). The FPGA receives on A9 and transmits on D10.
## The board nets are named from the bridge side: uart_txd_in is the FTDI TXD into the FPGA.
set_property PACKAGE_PIN A9 [get_ports {rxd}]
set_property IOSTANDARD LVCMOS33 [get_ports {rxd}]
set_property PACKAGE_PIN D10 [get_ports {txd}]
set_property IOSTANDARD LVCMOS33 [get_ports {txd}]

## LEDs, led[0] to led[3]
set_property PACKAGE_PIN H5 [get_ports {led[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[0]}]
set_property PACKAGE_PIN J5 [get_ports {led[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[1]}]
set_property PACKAGE_PIN T9 [get_ports {led[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[2]}]
set_property PACKAGE_PIN T10 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[3]}]

## Switches, sw[0] to sw[3] (baud-rate select)
set_property PACKAGE_PIN A8 [get_ports {sw[0]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[0]}]
set_property PACKAGE_PIN C11 [get_ports {sw[1]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[1]}]
set_property PACKAGE_PIN C10 [get_ports {sw[2]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[2]}]
set_property PACKAGE_PIN A10 [get_ports {sw[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {sw[3]}]

## Button, btn: clears the sticky status flags
set_property PACKAGE_PIN D9 [get_ports {btn}]
set_property IOSTANDARD LVCMOS33 [get_ports {btn}]
