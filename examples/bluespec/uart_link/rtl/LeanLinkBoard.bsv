// LeanLinkBoard: a board demo of LeanLink on the shared UART PHY, with the test endpoint (each
// response is the 32-bit count of earlier executions, then the request payload).
//
// The interface is the one of mkUartEcho (examples/bluespec/uart_echo), so its pin files and
// board wrappers apply: a clock CLK, rxd, four switches, a button, txd and four LEDs, and a
// power-on reset made inside. With LL_STALL_SW=1, sw[0] stalls the endpoint (for stall tests);
// by default the switches are ignored. The button clears the sticky PHY error LEDs.
//
// LEDs: led[0] responseReady, led[1] executed count bit 0, led[2] a framing, parity or break
// error seen, led[3] a receive overrun seen.
//
// Macros: CLK_HZ and BAUD for the PHY (see uart/rtl/Uart.bsv), LL_LOGW (default 3, W = 8) and
// LL_LOGP (default 8, 256-byte pages) for the link.
package LeanLinkBoard;

import Clocks :: *;
import GetPut :: *;
import Connectable :: *;
import Uart :: *;
import LeanLink :: *;
import LeanLinkTestTop :: *;
`ifdef LL_APP_ASCON
import StreamExchange :: *;
import AsconStream :: *;
`elsif LL_APP_GCD
import StreamExchange :: *;
import GcdStream :: *;
`endif

`ifndef LL_LOGW
`define LL_LOGW 3
`endif
`ifndef LL_LOGP
`define LL_LOGP 8
`endif
`ifndef LL_STALL_SW
`define LL_STALL_SW 0
`endif

interface LeanLinkBoard;
   (* always_ready, always_enabled, prefix = "" *)
   method Action inputs((* port = "rxd" *) Bit#(1) rxd,
                        (* port = "sw" *) Bit#(4) sw,
                        (* port = "btn" *) Bit#(1) btn);
   (* always_ready, result = "txd" *)
   method Bit#(1) txd;
   (* always_ready, result = "led" *)
   method Bit#(4) led;
endinterface

// The demo under the power-on reset. The pin wires have no reset (see mkUartEcho).
module mkLinkDemo(LeanLinkBoard);
   Wire#(Bit#(1)) rxdIn <- mkBypassWire(reset_by noReset);
   Wire#(Bit#(4)) swIn <- mkBypassWire(reset_by noReset);
   Wire#(Bit#(1)) btnIn <- mkBypassWire(reset_by noReset);
   Reg#(Bit#(1)) btnSync1 <- mkReg(0);
   Reg#(Bit#(1)) btnSync2 <- mkReg(0);
   Reg#(Bit#(1)) stallSync1 <- mkReg(0);
   Reg#(Bit#(1)) stallSync2 <- mkReg(0);

   Uart#(8) uart <- mkUart;
   LeanLink#(`LL_LOGW, `LL_LOGP) link <- mkLeanLink;
`ifdef LL_APP_ASCON
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(link);
   Empty application <- mkAsconStream(stream);
   Bit#(1) activity = 0;
`elsif LL_APP_GCD
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(link);
   Empty application <- mkGcdStream(stream);
   Bit#(1) activity = 0;
`else
   LeanLinkTestTop demo <- mkLeanLinkTestTopOf(link);
   Bit#(1) activity = demo.executed[0];
`endif

   mkConnection(uart.rx, link.rx);
   mkConnection(link.tx, uart.tx);

   (* fire_when_enabled, no_implicit_conditions *)
   rule pins;
      uart.pins.rxd(rxdIn);
      btnSync1 <= btnIn; btnSync2 <= btnSync1;
      stallSync1 <= swIn[0]; stallSync2 <= stallSync1;
`ifndef LL_APP_ASCON
`ifndef LL_APP_GCD
      demo.stall(`LL_STALL_SW != 0 && stallSync2 == 1);
`endif
`endif
      if (btnSync2 == 1) uart.clearFlags;
   endrule

   Clock clk <- exposeCurrentClock;
   UartFlags fl = uart.flags;
   Bit#(4) ledValue = {pack(fl.overrun), pack(fl.frameError || fl.parityError || fl.brk),
                       activity, pack(link.responseReady)};
   ReadOnly#(Bit#(1)) txdPin <- mkNullCrossingWire(clk, uart.pins.txd);
   ReadOnly#(Bit#(4)) ledPins <- mkNullCrossingWire(clk, ledValue);

   method Action inputs(Bit#(1) rxd, Bit#(4) sw, Bit#(1) btn);
      rxdIn <= rxd; swIn <= sw; btnIn <= btn;
   endmethod
   method Bit#(1) txd = txdPin;
   method Bit#(4) led = ledPins;
endmodule

// The synthesizable top with its own power-on reset, as mkUartEcho.
(* synthesize, no_default_reset *)
module mkLeanLinkBoard(LeanLinkBoard);
   Clock clk <- exposeCurrentClock;
   MakeResetIfc powerOn <- mkReset(0, False, clk, reset_by noReset);
   Reg#(UInt#(3)) held <- mkRegU;

   (* fire_when_enabled, no_implicit_conditions *)
   rule powerUp (held != 7);
      held <= held + 1;
      powerOn.assertReset;
   endrule

   LeanLinkBoard demo <- mkLinkDemo(reset_by powerOn.new_rst);

   method Action inputs(Bit#(1) rxd, Bit#(4) sw, Bit#(1) btn) = demo.inputs(rxd, sw, btn);
   method Bit#(1) txd = demo.txd | pack(held != 7);
   method Bit#(4) led = demo.led;
endmodule

endpackage
