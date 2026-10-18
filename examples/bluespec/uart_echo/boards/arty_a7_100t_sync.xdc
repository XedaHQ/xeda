# Arty A7-100T, Vivado only: the constraints of the asynchronous inputs of the uart_echo top
# mkUartEcho. The pins are in arty_a7_100t.xdc; openXC7's nextpnr cannot read these commands.
#
# rxd, sw[*] and btn are not synchronous to CLK. Each goes through two flops named *Sync1_reg and
# *Sync2_reg in the design (the receiver's rxSync1/rxSync2, lineSync1/2, swSync1/2 and
# btnSync1/2). ASYNC_REG keeps the two flops of a pair in one slice and tells the tools that the
# first one can go metastable; the paths from the pins are not timed.
#
# XDC is not full Tcl: `if` is not supported, so there is no check that the pattern matches.
# When no cell matches, Vivado warns; look for ASYNC_REG in the report of the run.
#
# Clock: there is no create_clock here either, the flow's clock setting writes it.

set sync_regs [get_cells -hierarchical -filter {NAME =~ *Sync1_reg* || NAME =~ *Sync2_reg*}]
set_property ASYNC_REG TRUE $sync_regs
set_false_path -from [get_ports {rxd sw[*] btn}]

## Configuration voltage, as in the Digilent master XDC
set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
