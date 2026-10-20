// UartEcho: a board demo for the UART PHY. Everything the host sends comes back.
//
//   rxd -> UartCore -> echo FIFO (BRAM) -> UartCore -> txd
//
// The core is board agnostic: a clock `CLK`, no reset pin (the power-on reset is made inside, see
// `mkUartEcho`), and the pins of a UART, four LEDs, four switches and a button:
//
//   rxd, txd   the UART, 8N1
//   sw[3:0]    the baud rate, an index into a table made at elaboration from CLK_HZ: 0 is `BAUD`,
//              then the rates a USB UART bridge can generate (12 M, 8 M, 6 M, 4 M, 3 M, 2 M,
//              1.5 M, 1 M, 921600, ... down to MIN_BAUD) that have at least 8 clocks per bit.
//              Indices past the end of the table select the last, the slowest rate. With
//              FIXED_BAUD 1 the switches are ignored.
//   btn        clears the sticky LEDs
//   led[0]     a frame was received (stretched: a human can see it)
//   led[1]     a byte was sent (stretched)
//   led[2]     sticky: framing error, parity error or break
//   led[3]     sticky: overrun, a byte was dropped because the PHY's receive buffer was full
//              (a full echo FIFO keeps it from emptying)
//
// A new baud rate takes effect when the echo is idle: the transmitter has nothing left to send and
// the receive line has been high for 11 bit times of the slowest rate, so no frame is in
// progress. Switches and button are asynchronous inputs and go through two flops each.
//
// FIXED_BAUD (0 or 1, default 1): with 1 the design has one baud rate, `BAUD`, and ignores the
// switches. The PHY then uses the static timing of `BAUD`, and the table, the switch
// synchronizers and the idle detector are not built (72 LUT and 48 FF fewer in Vivado 2024.2).
// The ports are the same, so the board files do not change; `sw` is just unused.
//
// ECHO_TRANSFORM (0 or 1, default 0): with 1 the byte b comes back as f(b) = reverseBits(b) ^ 8'hB1
// (`echoKey`). A hardwired loopback from txd to rxd returns every byte unchanged, so it cannot
// pass a test that expects f(b). The transform is wiring and inverters between the echo FIFO and
// the transmitter: no state and no latency, so the line stays fully used. The key is not a bit
// palindrome, which makes f(b) != b for all 256 bytes (elaboration checks it). The host test
// `uart_echo_test.py --transform` has the same constant, and so has the testbench: keep them equal.
//
// `configFixedBaud` and `configEchoTransform` read the two macros once and turn them into
// elaboration-time Bools. `mkUartEcho` passes them to `mkEcho` as parameters, and nothing else
// tests a macro for them. They take a value so that each can be switched off as well as on. Any
// value but 0 or 1 does not build, and Xeda leaves out a macro that is `false`, which gives the
// default. The testbench (`TbUartEcho`) builds `mkEcho` in all four combinations in one simulation.
//
// Macros (Xeda sets them with `rtl.defines`), all with defaults:
//
//   CLK_HZ       clock frequency in Hz (100000000)
//   BAUD         the baud rate: with FIXED_BAUD the one rate, otherwise the one for the switches
//                at 0 (12000000)
//   MIN_BAUD     the slowest rate in the table, which sizes the period counters (115200); not
//                used with FIXED_BAUD 1
//   ECHO_DEPTH   the echo FIFO, in bytes (2047: the library's BRAM FIFO holds 2**n - 1 bytes
//                in 2**n entries, so this is exactly one 18 Kbit block RAM at 8 bits)
//   ACTIVITY_US  how long an activity LED stays on, in microseconds (100000); the LED
//                stays on for 0.4 to 1 times as long
//   FIXED_BAUD   0 or 1: one baud rate, `BAUD`, and the switches ignored (1)
//   ECHO_TRANSFORM  0 or 1: the echo returns reverseBits(b) ^ 8'hB1 (0)
//   HOLD         the receiver's glitch filter depth in clocks (3); at most (D + 2) / 3 for D clocks
//                in the shortest bit, that of the fastest rate of the table (3 at 12 Mbaud /
//                100 MHz); a larger value is an elaboration error
//
// Constraints a board needs besides the pins: the synchronizer registers are named `rxSync1`,
// `rxSync2` (the PHY's receiver), `lineSync1`, `lineSync2`, `swSync1`, `swSync2`, `btnSync1` and
// `btnSync2` (with FIXED_BAUD 1 only `rxSync*` and `btnSync*` exist); the board files mark them
// ASYNC_REG and the input pins as false paths.
package UartEcho;

import BRAMFIFO :: *;
import Clocks :: *;
import ConfigReg :: *;
import FIFOF :: *;
import GetPut :: *;
import List :: *;
import BuildVector :: *;
import Vector :: *;
import Uart :: *;

export UartEcho(..);
export echoRates;
export echoByte;
export configFixedBaud;
export configEchoTransform;
export mkEcho;
export mkUartEcho;

`ifndef CLK_HZ
`define CLK_HZ 100000000
`endif
`ifndef BAUD
`define BAUD 12000000
`endif
`ifndef MIN_BAUD
`define MIN_BAUD 115200
`endif
`ifndef ECHO_DEPTH
`define ECHO_DEPTH 2047
`endif
`ifndef ACTIVITY_US
`define ACTIVITY_US 100000
`endif
`ifndef HOLD
`define HOLD 3
`endif
`ifndef FIXED_BAUD
`define FIXED_BAUD 1
`endif
`ifndef ECHO_TRANSFORM
`define ECHO_TRANSFORM 0
`endif

interface UartEcho;
   // The three inputs, one method so that it is one enable-less port group.
   (* always_ready, always_enabled, prefix = "" *)
   method Action inputs((* port = "rxd" *) Bit#(1) rxd,
                        (* port = "sw" *) Bit#(4) sw,
                        (* port = "btn" *) Bit#(1) btn);
   (* always_ready, result = "txd" *)
   method Bit#(1) txd;
   (* always_ready, result = "led" *)
   method Bit#(4) led;
endinterface

// The baud rates the switches select: `dflt`, then the candidates that the clock can do at 8 clocks
// per bit or more and that are not slower than `minBaud`, at most 16 in all.
function List#(Integer) echoRates(Integer clockHz, Integer dflt, Integer minBaud);
   List#(Integer) candidates = toList(vec(12000000, 8000000, 6000000, 4000000, 3000000, 2000000,
                                           1500000, 1000000, 921600, 460800, 230400, 115200,
                                           57600, 38400, 19200, 9600));
   function Bool usable(Integer b) = b != dflt && b >= minBaud && clockHz >= 8 * b;
   return List::take(16, List::cons(dflt, List::filter(usable, candidates)));
endfunction

// The counter of the static period of BAUD, with a accumulator bits (the same sizing as mkUart).
typedef StaticCntBits#(`CLK_HZ, `BAUD, a) FixedCntBits#(numeric type a);

// With switches the period counters count D-1 for the slowest rate; the period carries 6 fraction
// bits.
typedef StaticCntBits#(`CLK_HZ, `MIN_BAUD, 6) CntBits;
typedef BitPeriod#(CntBits, 6) Period;

// Clocks that the receive line must stay high before the baud rate may change: 11 bit times of
// the slowest rate, so a frame of 10 bits is over, plus one.
typedef TAdd#(TDiv#(TMul#(11, `CLK_HZ), `MIN_BAUD), 1) QuietClocks;
typedef TLog#(TAdd#(QuietClocks, 1)) QuietBits;

// An activity LED holds for 7 ticks of a free-running prescaler, 6 to 7 ticks since the event
// is not aligned to it. A tick is the power of two of clocks that makes 7 ticks at most
// ACTIVITY_US.
typedef TMax#(1, TSub#(TLog#(TAdd#(TDiv#(TMul#(`CLK_HZ, `ACTIVITY_US), 7000000), 1)), 1)) TickBits;

// The value of an on/off macro: 0 is False, 1 is True, and anything else does not elaborate. A
// macro that has no value (`-D NAME`) never gets here: bsc reads the call below as one with a
// missing argument and stops with a syntax error at that line.
function Bool macroFlag(String name, Integer value);
   if (value == 0)
      return False;
   else if (value == 1)
      return True;
   else
      return error(name + " is " + integerToString(value) + ": write 0 or 1");
endfunction

// The two on/off macros, read once. A macro defined with no value expands to nothing, and bsc then
// reports "Unexpected `)'" on its line below: write 0 or 1.
Bool configFixedBaud = macroFlag("FIXED_BAUD", `FIXED_BAUD);
Bool configEchoTransform = macroFlag("ECHO_TRANSFORM", `ECHO_TRANSFORM);

Bit#(8) echoKey = 8'hB1;

// The key is not a palindrome, so no byte is its own echo: checked for all 256 bytes when the
// design is elaborated. (The bits of reverseBits(b) ^ b are a palindrome for every b, so f(b) == b
// would need a palindromic key.)
function Bool changesByte(Integer i);
   Bit#(8) b = fromInteger(i);
   return (reverseBits(b) ^ echoKey) != b;
endfunction

Bit#(8) echoKeyChecked =
   List::all(changesByte, List::upto(0, 255))
   ? echoKey
   : error("ECHO_TRANSFORM: the key is a bit palindrome, so some byte would come back unchanged");

// The byte the echo sends back for the received byte b: with `transform` reverseBits(b) ^ 8'hB1,
// otherwise b itself.
function Bit#(8) echoByte(Bool transform, Bit#(8) b) =
   transform ? reverseBits(b) ^ echoKeyChecked : b;

// The echo with the default reset, which is the power-on reset of `mkUartEcho`. `fixedBaud`: one
// baud rate, `BAUD`, and the switches ignored. `transform`: the transformed echo. Both are
// elaboration-time values: what they leave out is not built.
module mkEcho#(Bool fixedBaud, Bool transform)(UartEcho);
   // The pins have no reset: the methods that carry them must not belong to the power-on reset,
   // which the module has no port for.
   Wire#(Bit#(1)) rxdIn <- mkBypassWire(reset_by noReset);
   Wire#(Bit#(4)) swIn <- mkBypassWire(reset_by noReset);
   Wire#(Bit#(1)) btnIn <- mkBypassWire(reset_by noReset);

   // Synchronizer for the button. (The receiver has its own for rxd inside the PHY.)
   Reg#(Bit#(1)) btnSync1 <- mkReg(0);
   Reg#(Bit#(1)) btnSync2 <- mkReg(0);

   Format8N1 fmt = uartFormat(NoParity);
   Depth#(1) one = depth;

   // The baud rate is one of two things. `selected` is the index of the table that the switches
   // pick; it is the only state of the table (the periods are a constant function of it, so they
   // cost no flops), and it exists only with switches. A ConfigReg: the timers read it, and the
   // idle test reads what the timers' users write.
   Reg#(UInt#(4)) selected = ?;
   Uart#(8) uart;
   if (fixedBaud) begin
      // The static period of BAUD; the counter and the accumulator get exactly the bits it needs
      // (a choice of types, so a choice of instance, as in mkUart).
      Integer adjust = defaultAdjust(`CLK_HZ, `BAUD);
      function module#(Uart#(8)) core(BitTiming#(wc, aw) t) = mkUartCore(t, fmt, `HOLD, one, one);
      case (staticAccBits(`CLK_HZ, `BAUD, 6))
         0: uart <- core(BitTiming#(FixedCntBits#(0), 0)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         1: uart <- core(BitTiming#(FixedCntBits#(1), 1)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         2: uart <- core(BitTiming#(FixedCntBits#(2), 2)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         3: uart <- core(BitTiming#(FixedCntBits#(3), 3)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         4: uart <- core(BitTiming#(FixedCntBits#(4), 4)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         5: uart <- core(BitTiming#(FixedCntBits#(5), 5)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         6: uart <- core(BitTiming#(FixedCntBits#(6), 6)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
         default: uart = error("mkUartEcho: the fixed baud rate needs more than 6 accumulator bits");
      endcase
   end
   else begin
      // The baud table: switch index -> period.
      List#(Integer) rates = echoRates(`CLK_HZ, `BAUD, `MIN_BAUD);
      Integer fastest = List::foldr(max, 0, rates);
      function Period entry(Integer i) = bitPeriod(`CLK_HZ, rates[min(i, List::length(rates) - 1)]);
      Vector#(16, Period) periods = genWith(entry);
      selected <- mkConfigReg(0);
      // The fastest rate of the table limits the hold filter (checked by mkUartCore).
      BitTiming#(CntBits, 6) timing = runtimeTiming(periods[selected],
                                                     defaultAdjust(`CLK_HZ, fastest),
                                                     shortestBit(`CLK_HZ, fastest, 6));
      uart <- mkUartCore(timing, fmt, `HOLD, one, one);
   end
   FIFOF#(Bit#(8)) echoQ <- mkSizedBRAMFIFOF(`ECHO_DEPTH);

   // Activity LEDs: one prescaler, and a three-bit hold counter per LED.
   Reg#(UInt#(TickBits)) prescale <- mkReg(0);
   Reg#(UInt#(3)) rxHold <- mkReg(0);
   Reg#(UInt#(3)) txHold <- mkReg(0);

   UartEvents ev = uart.events;
   UartFlags fl = uart.flags;
   Bool rxActivity = ev.rxByte || ev.frameError || ev.parityError || ev.brk || ev.overrun;

   (* fire_when_enabled, no_implicit_conditions *)
   rule synchronize;
      btnSync1 <= btnIn;
      btnSync2 <= btnSync1;
      uart.pins.rxd(rxdIn);
   endrule

   // With switches: the synchronizers of the switches, `lineSync` (a second look at rxd for the
   // idle detector) and the idle detector that takes a new rate when the echo is idle.
   if (!fixedBaud) begin
      Reg#(Bit#(1)) lineSync1 <- mkReg(0);
      Reg#(Bit#(1)) lineSync2 <- mkReg(0);
      Reg#(Bit#(4)) swSync1 <- mkReg(0);
      Reg#(Bit#(4)) swSync2 <- mkReg(0);
      Reg#(UInt#(QuietBits)) quiet <- mkReg(0);

      (* fire_when_enabled, no_implicit_conditions *)
      rule synchronizeSwitches;
         lineSync1 <= rxdIn;
         lineSync2 <= lineSync1;
         swSync1 <= swIn;
         swSync2 <= swSync1;
      endrule

      (* fire_when_enabled, no_implicit_conditions *)
      rule retune;
         Bool lineQuiet = quiet == fromInteger(valueOf(QuietClocks));
         quiet <= (lineSync2 == 0) ? 0 : (lineQuiet ? quiet : quiet + 1);
`ifdef XEDA_INJECT_BUG
         Bool idle = True;   // deliberate bug for negative tests: retune in the middle of a frame
`elsif XEDA_INJECT_BUG_ECHO
         Bool idle = True;
`else
         // lineSync2 is the PHY's rxSync2 (the same two flops on the same pin). With HOLD 1 the
         // receiver starts a frame in the cycle where it goes low, while `quiet` is still full and
         // clears only at the end of that cycle. Without `lineSync2 == 1` the rate could change in
         // that cycle, and the frame would start with the old period and go on with the new one.
         Bool idle = lineQuiet && lineSync2 == 1 && !uart.txBusy && !echoQ.notEmpty;
`endif
         if (idle)
            selected <= unpack(swSync2);
      endrule
   end

   (* fire_when_enabled, no_implicit_conditions *)
   rule clear (btnSync2 == 1);
      uart.clearFlags;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule blink;
      Bool slow = prescale == unpack('1);
      prescale <= prescale + 1;
      function UInt#(3) hold(UInt#(3) h, Bool hit) =
         hit ? 7 : ((slow && h != 0) ? h - 1 : h);
      rxHold <= hold(rxHold, rxActivity);
      txHold <= hold(txHold, ev.txByte);
   endrule

   (* fire_when_enabled *)
   rule toEcho;
      let b <- uart.rx.get;
      echoQ.enq(b);
   endrule

   (* fire_when_enabled *)
   rule fromEcho;
`ifdef XEDA_INJECT_BUG_PLAIN
      uart.tx.put(echoQ.first);   // deliberate bug for negative tests: no transform
`else
      uart.tx.put(echoByte(transform, echoQ.first));
`endif
      echoQ.deq;
   endrule

   // A null crossing is a wire that only drops the reset association of the pin values.
   Clock clk <- exposeCurrentClock;
   Bit#(4) ledValue = {pack(fl.overrun), pack(fl.frameError || fl.parityError || fl.brk),
                       pack(txHold != 0), pack(rxHold != 0)};
   ReadOnly#(Bit#(1)) txdPin <- mkNullCrossingWire(clk, uart.pins.txd);
   ReadOnly#(Bit#(4)) ledPins <- mkNullCrossingWire(clk, ledValue);

   method Action inputs(Bit#(1) rxd, Bit#(4) sw, Bit#(1) btn);
      rxdIn <= rxd;
      swIn <= sw;
      btnIn <= btn;
   endmethod
   method Bit#(1) txd = txdPin;
   method Bit#(4) led = ledPins;
endmodule

// The synthesizable top. Its reset is made here, so that no board needs a reset pin: the 3-bit
// counter `held` counts up to 7 and asserts the reset of the echo meanwhile (MakeReset0 registers
// it, so the reset covers the clock after each count). The counter has no reset itself, and its
// start value is not the same everywhere:
//  * FPGA: 0, every flip-flop is 0 after configuration (bsc's initial blocks are between
//    `synopsys translate_off` lines, so synthesis does not see them): 7 clocks of reset;
//  * simulator: bsc's initial pattern 'hAAAA cut to 3 bits, 3'h2: 5 clocks of reset;
//  * four-state simulator with BSV_NO_INITIAL_BLOCKS: X, which stays X: no reset ever.
//    Simulate with the initial blocks, or give `held` the INIT value 0 as an FPGA has it.
// The library's `mkInitialReset` is no use here: its body is between `synopsys translate_off`
// lines, so it does not exist in the synthesized design. `MakeReset0` has the pragma around its
// initial block only; its flip-flop is 0 on an FPGA, which is in reset with an active-low reset
// and out of reset with an active-high one, and the counter covers both.
(* synthesize, no_default_reset *)
module mkUartEcho(UartEcho);
   Clock clk <- exposeCurrentClock;
   MakeResetIfc powerOn <- mkReset(0, False, clk, reset_by noReset);
   Reg#(UInt#(3)) held <- mkRegU;

   (* fire_when_enabled, no_implicit_conditions *)
   rule powerUp (held != 7);
      held <= held + 1;
      powerOn.assertReset;
   endrule

   UartEcho echo <- mkEcho(configFixedBaud, configEchoTransform, reset_by powerOn.new_rst);

   // Flip-flops are 0 when an FPGA is configured, and the transmitter's is the one that holds txd:
   // it would show a low pulse (a start bit) for the clock or two before the reset reaches it. So
   // txd is held high until the reset is over. `held` cannot glitch the pin: it reaches 7 while
   // the transmitter is in its idle state.
   method Action inputs(Bit#(1) rxd, Bit#(4) sw, Bit#(1) btn) = echo.inputs(rxd, sw, btn);
   method Bit#(1) txd = echo.txd | pack(held != 7);
   method Bit#(4) led = echo.led;
endmodule

endpackage
