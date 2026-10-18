// Self-checking testbench for mkUartEcho, the board demo, at the pins.
//
// A behavioural host (the line sender of the UART testbench, TbUartModels) drives rxd, the switches
// and the button; a model of a receiver decodes txd, one frame at a time from its own start edge,
// and compares every byte with what was sent. The design is the real one except for two sizes that
// the design file shrinks for the simulation: the echo FIFO (ECHO_DEPTH) so that it can overrun
// in a few thousand clocks, and the activity LED stretch (ACTIVITY_US).
//
//  1. Power-up: the internal reset ends, the LEDs are off, the line is idle.
//  2. Echo of corner and random bytes at 100 % utilization at 12 Mbaud (switches 0).
//  3. Full line utilization of the transmitter: with the host 0.5 % faster than the echo, the FIFO
//     fills and every echoed frame starts in the clock where the previous one ended.
//  4. LEDs: receive and send activity go on and, after the stretch, off.
//  5. A framing error (stop bit low) and a break light the error LED, nothing is echoed, the
//     button clears it. Bytes after the errors come back.
//  6. Overrun: the host 3.5 % faster than the echo until a byte is dropped; the overrun LED is on,
//     the bytes that were echoed are in order and every missing byte is counted, the button
//     clears it.
//  7. The baud rate: a switch is changed in the middle of a back-to-back burst (the burst comes
//     back whole, at the old rate), and when the echo is idle the new rate takes effect: bytes at
//     3 Mbaud and at 921600 baud are echoed.
//     With FIXED_BAUD the switches are ignored: the same flips must change nothing, the bursts
//     before and after come back at the one rate.
//
// A failed check ends the run with $fatal. `XEDA_INJECT_BUG` makes the echo take a new baud rate
// at once instead of when it is idle (and also plants the PHY bug of the uart example), which the
// burst in 7 catches; `XEDA_INJECT_BUG_ECHO` plants only the echo bug.
package TbUartEcho;

import FIFOF :: *;
import List :: *;
import StmtFSM :: *;
import UartTypes :: *;
import UartEcho :: *;
import TbUartModels :: *;

`ifndef CLK_HZ
`define CLK_HZ 100000000
`endif
`ifndef BAUD
`define BAUD 12000000
`endif
`ifndef MIN_BAUD
`define MIN_BAUD 115200
`endif

Integer clkHz = `CLK_HZ;
UInt#(32) maxCycles = 200000;

// The baud rate the switches select
function Integer baudOf(Integer sw);
`ifdef FIXED_BAUD
   return `BAUD;
`else
   List#(Integer) rates = echoRates(clkHz, `BAUD, `MIN_BAUD);
   return rates[min(sw, List::length(rates) - 1)];
`endif
endfunction

// progress messages, with -D TB_TRACE
function Action note(String msg, UInt#(32) at) = action
`ifdef TB_TRACE
   $display("%0d: %s", at, msg);
`endif
endaction;

function Action fail(String msg) = action $fatal(1, "FAIL: " + msg); endaction;

// ---------------------------------------------------------------------------------------
// A receiver on txd. Each frame is decoded from its own start edge (the first low clock): the
// start bit, eight data bits and the stop bit are sampled at their centres, with a bit period that
// the test sets. Frames may follow each other with or without a gap; `chained` counts those that
// begin where the previous one ended, to within a clock.
//
// Strict mode: every byte must be the next one that the test announced with `announce`. Counting
// mode: the bytes the test sends count up, a byte may be missing (dropped by the echo) and the
// receiver counts how many.

interface EchoReceiver;
   (* always_ready, always_enabled *)
   method Action line(Bit#(1) v);
   method Action setPeriod(Fx p);
   method Action announce(Bit#(8) b);
   // The sequence 0, 1, 2 ... is coming, with bytes missing.
   method Action startCounting;
   method UInt#(32) frames;
   method UInt#(32) missing;
   method UInt#(32) chained;
   // Nothing is being received and every expected byte has arrived.
   method Bool idle;
endinterface

module mkEchoReceiver#(String name)(EchoReceiver);
   Wire#(Bit#(1)) pin <- mkBypassWire;
   RWire#(Fx) newPeriod <- mkRWire;
   PulseWire restart <- mkPulseWire;
   Reg#(Fx) now <- mkReg(0);
   Reg#(Fx) period <- mkReg(clocksFx(8));
   Reg#(Bool) busy <- mkReg(False);
   Reg#(Fx) start <- mkReg(0);
   Reg#(Fx) nextSample <- mkReg(0);
   Reg#(UInt#(4)) index <- mkReg(0);     // 0 start bit, 1 to 8 data, 9 stop bit
   Reg#(Bit#(8)) data <- mkReg(0);
   Reg#(Bool) havePrevious <- mkReg(False);
   Reg#(Fx) previousEnd <- mkReg(0);
   Reg#(UInt#(32)) done <- mkReg(0);
   Reg#(UInt#(32)) lost <- mkReg(0);
   Reg#(UInt#(32)) joined <- mkReg(0);
   Reg#(Bool) counting <- mkReg(False);
   Reg#(Bit#(8)) nextData <- mkReg(0);
   FIFOF#(Bit#(8)) want <- mkUGSizedFIFOF(32);

   (* fire_when_enabled, no_implicit_conditions *)
   rule decode;
      Bit#(1) l = pin;
      now <= now + oneClock;
      period <= fromMaybe(period, newPeriod.wget);
      UInt#(32) nextDone = done;
      UInt#(32) nextLost = lost;
      Bit#(8) nextNext = nextData;
      if (!busy) begin   // from clock 0: txd is idle high even before the reset is over
         if (l == 0) begin
            busy <= True;
            start <= now;
            nextSample <= now + period / 2;
            index <= 0;
            Fx gap = now - previousEnd;
            if (havePrevious && (gap < 0 ? -gap : gap) <= oneClock + oneClock / 16)
               joined <= joined + 1;
         end
      end
      else if (now >= nextSample) begin
         nextSample <= nextSample + period;
         index <= index + 1;
         if (index == 0) begin
            if (l != 0) fail(name + ": the start bit is not low");
         end
         else if (index <= 8)
            data <= {l, data[7:1]};
         else begin
            if (l != 1) fail(name + ": the stop bit is low");
            busy <= False;
            havePrevious <= True;
            previousEnd <= start + 10 * period;
            nextDone = done + 1;
            if (counting) begin
               nextLost = lost + extend(unpack(data - nextData));
               nextNext = data + 1;
            end
            else if (!want.notEmpty)
               $fatal(1, "FAIL: %s: unexpected byte %h at clock %0d", name, data, now / oneClock);
            else begin
               if (want.first != data)
                  $fatal(1, "FAIL: %s: received %h, expected %h (byte %0d)", name, data,
                         want.first, done);
               want.deq;
            end
         end
      end
      done <= restart ? 0 : nextDone;
      lost <= restart ? 0 : nextLost;
      nextData <= restart ? 0 : nextNext;
      if (restart) counting <= True;
   endrule

   method Action line(Bit#(1) v) = pin._write(v);
   method Action setPeriod(Fx p) = newPeriod.wset(p);
   method Action announce(Bit#(8) b) if (want.notFull);
      want.enq(b);
   endmethod
   method Action startCounting = restart.send;
   method UInt#(32) frames = done;
   method UInt#(32) missing = lost;
   method UInt#(32) chained = joined;
   method Bool idle = !busy && !want.notEmpty;
endmodule

// ---------------------------------------------------------------------------------------

(* synthesize *)
module mkTbUartEcho(Empty);
   UartEcho dut <- mkUartEcho;
   LineSender host <- mkLineSender;
   EchoReceiver receiver <- mkEchoReceiver("echo txd");
   Rng rng <- mkRng(32'h2468ACE1);

   Reg#(Bit#(4)) sw <- mkReg(0);
   Reg#(Bit#(1)) btn <- mkReg(0);
   Reg#(UInt#(32)) cycle <- mkReg(0);
   Reg#(UInt#(32)) t <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(16)) sent <- mkReg(0);
   Reg#(UInt#(16)) wasMissing <- mkReg(0);

   (* fire_when_enabled, no_implicit_conditions *)
   rule pins;
      dut.inputs(host.line, sw, btn);
      receiver.line(dut.txd);
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule watchdog;
      cycle <= cycle + 1;
`ifdef TB_TRACE
      if (cycle % 2000 == 0)
         $display("%0d: leds %b frames %0d txd %b rxd %b", cycle, dut.led, receiver.frames, dut.txd,
                  host.line);
`endif
      if (cycle == maxCycles) fail("timeout");
   endrule

   function Stmt pause(Integer n) = seq
      t <= 0;
      while (t < fromInteger(n)) t <= t + 1;
   endseq;

   function Bit#(8) low8(UInt#(16) x) = truncate(pack(x));

   // A byte from the host that the test does not announce: counting mode and errors.
   function Action hostByte(Bit#(8) d) = host.send(frameWord(d, NoParity, 1, False, False), 10);

   function Action sendByte(Bit#(8) d) = action
      hostByte(d);
      receiver.announce(d);
   endaction;

   // Both ends of the line run at `baud`, the host `permille` per mille faster.
   function Action setRate(Integer sel, Integer permille) = action
      host.setPeriod(periodFx(clkHz, baudOf(sel), permille));
      receiver.setPeriod(periodFx(clkHz, baudOf(sel), 0));
   endaction;

   function Action expectLeds(String where, Bit#(4) mask, Bit#(4) value) = action
      if ((dut.led & mask) != value)
         $fatal(1, "FAIL: %s: leds %b, want %b under the mask %b", where, dut.led, value, mask);
   endaction;

   // Wait for the host to be done, the echo to have arrived, and a little more.
   Stmt settle = seq
      await(host.idle);
      await(receiver.idle);
      pause(100);
   endseq;

   Integer bits = clkHz / baudOf(0);       // clocks per bit at the default rate, rounded down
   Integer stretch = 4000;                 // longer than the activity LEDs stay on (ACTIVITY_US)
   Integer quiet = 11 * (clkHz / `MIN_BAUD) + 1000;  // the echo takes a new baud rate after this

   Stmt power = seq
      pause(200);
      expectLeds("power-up", 4'b1111, 4'b0000);
      if (dut.txd != 1) fail("txd is not idle after reset");
      setRate(0, 0);
   endseq;

   Stmt corners = seq
      host.setPhase(0);
      for (i <= 0; i < 64; i <= i + 1) action
         let r <- rng.next;
         Bit#(8) d = (i == 0) ? 8'h00 : (i == 1) ? 8'hFF : (i == 2) ? 8'h55 : (i == 3) ? 8'h80
                   : (i == 4) ? 8'h01 : r[7:0];
         sendByte(d);
      endaction
      // while the echo is under way both activity LEDs are on
      await(receiver.frames >= 6);
      expectLeds("activity", 4'b0011, 4'b0011);
      settle;
      if (receiver.frames != 64) fail("not all of the 64 bytes came back");
      expectLeds("activity after the echo", 4'b1111, 4'b0011);
      pause(stretch);
      expectLeds("activity stretch", 4'b1111, 4'b0000);
   endseq;

   // The host faster than the echo: the FIFO fills and the transmitter never idles.
   Stmt utilization = seq
      setRate(0, 5);
      host.setPhase(30000);
      for (i <= 0; i < 100; i <= i + 1) action
         let r <- rng.next;
         sendByte(r[7:0]);
      endaction
      settle;
      if (receiver.frames != 164) fail("not all of the 100 bytes came back");
      if (receiver.chained < 98)
         $fatal(1, "FAIL: only %0d of 100 echoed frames are back to back", receiver.chained);
      setRate(0, 0);
   endseq;

   Stmt errors = seq
      // a framing error: the byte is dropped. The receiver waits for an idle line (it cannot know
      // where the next frame starts), so the host leaves a gap; the next bytes are echoed.
      action
         host.send(frameWord(8'h5A, NoParity, 1, False, True), 10);
      endaction
      await(host.idle);
      pause(3 * bits);
      for (i <= 0; i < 3; i <= i + 1) sendByte(low8(i) + 8'h30);
      settle;
      pause(stretch);
      expectLeds("framing error", 4'b1111, 4'b0100);
      // a break of 20 bit times: one error, no byte
      host.send(0, 20);
      settle;
      pause(5 * bits);
      sendByte(8'hC3);
      settle;
      pause(stretch);
      expectLeds("break", 4'b1111, 4'b0100);
      btn <= 1;
      pause(10);
      btn <= 0;
      pause(10);
      expectLeds("button", 4'b1100, 4'b0000);
   endseq;

   // The host faster than the echo by more than the FIFO can hold, a counting sequence so that
   // the dropped bytes can be told.
   Stmt overrun = seq
      setRate(0, 35);
      receiver.startCounting;
      sent <= 0;
      while (dut.led[3] == 0 && sent < 1000) action
         hostByte(low8(sent));
         sent <= sent + 1;
      endaction
      await(host.idle);
      expectLeds("overrun", 4'b1000, 4'b1000);
      setRate(0, 0);
      pause(40 * bits);
      // after the burst the echo drains; more bytes show how many were lost
      for (i <= 0; i < 6; i <= i + 1) action
         hostByte(low8(sent));
         sent <= sent + 1;
      endaction
      await(host.idle);
      pause(40 * bits);
      if (sent >= 1000) fail("the echo FIFO did not overrun");
      if (receiver.missing == 0) fail("overrun: no byte is missing");
      if (extend(sent) != receiver.frames + receiver.missing)
         $fatal(1, "FAIL: overrun: sent %0d bytes, %0d came back, %0d missing", sent,
                receiver.frames, receiver.missing);
      expectLeds("overrun sticky", 4'b1000, 4'b1000);
      btn <= 1;
      pause(10);
      btn <= 0;
      pause(10);
      expectLeds("overrun cleared", 4'b1100, 4'b0000);
   endseq;

   // A switch is flipped in the middle of a burst of frames at the old rate.
   Stmt retune = seq
      receiver.startCounting;   // leaves strict mode: the bytes of the burst count up
      sent <= 0;
      for (i <= 0; i < 40; i <= i + 1) action
         hostByte(low8(i));
         if (i == 12) sw <= 4'd4;
      endaction
      await(host.idle);
      pause(20 * bits);
      if (receiver.frames != 40 || receiver.missing != 0)
         $fatal(1, "FAIL: retune: %0d of 40 bytes came back, %0d missing", receiver.frames,
                receiver.missing);
      // idle: the echo takes the new rate
      pause(quiet);
      setRate(4, 0);
      receiver.startCounting;
      for (i <= 0; i < 20; i <= i + 1) action
         hostByte(low8(i));
      endaction
      await(host.idle);
      pause(20 * 10 * (clkHz / baudOf(4)));
      if (receiver.frames != 20 || receiver.missing != 0)
         $fatal(1, "FAIL: %0d Mbaud: %0d of 20 bytes came back, %0d missing", baudOf(4) / 1000000,
                receiver.frames, receiver.missing);
      sw <= 4'd8;
      pause(quiet);
      setRate(8, -30);
      receiver.startCounting;
      for (i <= 0; i < 6; i <= i + 1) action
         hostByte(low8(i));
      endaction
      await(host.idle);
      pause(20 * 10 * (clkHz / baudOf(8)));
      if (receiver.frames != 6 || receiver.missing != 0)
         $fatal(1, "FAIL: %0d baud: %0d of 6 bytes came back, %0d missing", baudOf(8),
                receiver.frames, receiver.missing);
   endseq;

   Stmt script = seq
      note("power", cycle); power;
      note("corners", cycle); corners;
      note("utilization", cycle); utilization;
      note("errors", cycle); errors;
      note("overrun", cycle); overrun;
      note("retune", cycle); retune;
      action
`ifdef FIXED_BAUD
         $display("PASS: fixed baud rate echo checked, switches ignored, %0d cycles (%0d frames back to back)",
                  cycle, receiver.chained);
`else
         $display("PASS: echo checked at 3 baud rates, %0d cycles (%0d frames back to back)", cycle,
                  receiver.chained);
`endif
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

endpackage
