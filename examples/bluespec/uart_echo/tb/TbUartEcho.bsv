// Self-checking testbench for mkUartEcho, the board demo, at the pins.
//
// A behavioral host (the line sender of the UART testbench, TbUartModels) drives rxd, the switches
// and the button; a model of a receiver decodes txd, one frame at a time from its own start edge,
// and compares every byte with what was sent. The design is the real one except for two sizes that
// the design file shrinks for the simulation: the echo FIFO (ECHO_DEPTH) so that it can overrun
// in a few thousand clocks, and the activity LED stretch (ACTIVITY_US).
//
//  1. Power-up: the internal reset ends, the LEDs are off, the line is idle.
//  2. Echo of corner and random bytes at 100 % utilization at 12 Mbaud (or the fixed rate).
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
//     With FIXED_BAUD 1 the switches are ignored: the same flips must change nothing, the bursts
//     before and after come back at the one rate.
//  8. A switch is changed in the very clock where a start bit reaches the synchronizers: the
//     frame still comes back whole at the old rate, and the new rate takes effect after it.
//     Not with FIXED_BAUD 1.
//
// The two on/off macros of the design, FIXED_BAUD and ECHO_TRANSFORM, are Bool parameters of
// `mkEcho`, so one simulation runs all four combinations side by side (`mkBench`): the switches
// and the fixed build, each with the plain and the transformed echo, every one with its own host,
// receiver model and script. The combination that the macros select uses the real top `mkUartEcho`
// (with its power-on reset, as built for a board); the three others use `mkEcho`. This file has no
// `ifdef` for the macros: it reads them through `configFixedBaud` and `configEchoTransform`.
//
// ECHO_TRANSFORM: the echo returns f(b) = reverseBits(b) ^ 8'hB1 instead of b. Every check above
// then expects f(b) where it expected b. The testbench has its own copy of f, written differently
// from the design's, and elaboration refuses a build in which f(b) == b for some b, the two copies
// differ, or the inverse is not the inverse. `XEDA_INJECT_BUG_PLAIN` makes the design send b back
// unchanged, which the transformed configurations catch at the first byte.
//
// The top prints `PASS` once, when all four benches are done. Each bench says only that it is done,
// so a bench that finishes early cannot print PASS while another can still fail.
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

// What the echo must send back for the byte b, and the byte that was sent given what came back.
// Independent of the design's `echoByte`, which these are checked against below.
function Bit#(8) expectedEcho(Bool transform, Bit#(8) b);
   return transform ? {b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]} ^ 8'hB1 : b;
endfunction

function Bit#(8) sentFor(Bool transform, Bit#(8) y);
   Bit#(8) x = y ^ 8'hB1;
   return transform ? {x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7]} : y;
endfunction

// Over all 256 bytes and both modes: the design's echo is this one, this one is undone by
// `sentFor`, and with the transform no byte comes back as itself (so a wire loopback fails on
// every byte).
function Bool echoTableOk(Integer i);
   Bit#(8) b = fromInteger(i);
   Bool ok = True;
   for (Integer m = 0; m < 2; m = m + 1) begin
      Bool t = m == 1;
      ok = ok && echoByte(t, b) == expectedEcho(t, b) && sentFor(t, expectedEcho(t, b)) == b;
      if (t) ok = ok && expectedEcho(t, b) != b;
   end
   return ok;
endfunction

Bool echoTableChecked =
   List::all(echoTableOk, List::upto(0, 255))
   ? True
   : error("the echo transform of the design and of the testbench differ, "
           + "or a byte is its own echo");

Integer clkHz = `CLK_HZ;
UInt#(32) maxCycles = 200000;

// The baud rate the switches select (with the fixed rate, whatever they say)
function Integer baudFor(Bool fixedBaud, Integer sw);
   List#(Integer) rates = echoRates(clkHz, `BAUD, `MIN_BAUD);
   return fixedBaud ? `BAUD : rates[min(sw, List::length(rates) - 1)];
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
// start bit, eight data bits and the stop bit are sampled at their centers, with a bit period that
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

module mkEchoReceiver#(String name, Bool transform)(EchoReceiver);
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
               Bit#(8) sentByte = sentFor(transform, data);
               nextLost = lost + extend(unpack(sentByte - nextData));
               nextNext = sentByte + 1;
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
      want.enq(expectedEcho(transform, b));
   endmethod
   method Action startCounting = restart.send;
   method UInt#(32) frames = done;
   method UInt#(32) missing = lost;
   method UInt#(32) chained = joined;
   method Bool idle = !busy && !want.notEmpty;
endmodule

// ---------------------------------------------------------------------------------------

// One configuration of the echo, with its own host and receiver model, and the script that checks
// it. `dut` is the echo that was built with `fixedBaud` and `transform`. The result says that the
// script is done (it has not failed: a failed check ends the simulation).
module mkBench#(String name, Bool fixedBaud, Bool transform, UartEcho dut)(ReadOnly#(Bool));
   function Action fail(String msg) = action $fatal(1, "FAIL: " + name + ": " + msg); endaction;
   function Integer baudOf(Integer sw) = baudFor(fixedBaud, sw);

   LineSender host <- mkLineSender;
   EchoReceiver receiver <- mkEchoReceiver(name + " txd", transform);
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
   rule count;
      cycle <= cycle + 1;
`ifdef TB_TRACE
      if (cycle % 2000 == 0)
         $display("%0d: %s: leds %b frames %0d txd %b rxd %b", cycle, name, dut.led,
                  receiver.frames, dut.txd, host.line);
`endif
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
         $fatal(1, "FAIL: " + name + ": %s: leds %b, want %b under the mask %b", where, dut.led, value, mask);
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
      if (!echoTableChecked) fail("echo transform table");
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
         $fatal(1, "FAIL: " + name + ": only %0d of 100 echoed frames are back to back", receiver.chained);
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
         $fatal(1, "FAIL: " + name + ": overrun: sent %0d bytes, %0d came back, %0d missing", sent,
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
         $fatal(1, "FAIL: " + name + ": retune: %0d of 40 bytes came back, %0d missing", receiver.frames,
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
         $fatal(1, "FAIL: " + name + ": %0d Mbaud: %0d of 20 bytes came back, %0d missing", baudOf(4) / 1000000,
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
         $fatal(1, "FAIL: " + name + ": %0d baud: %0d of 6 bytes came back, %0d missing", baudOf(8),
                receiver.frames, receiver.missing);
   endseq;

   // The switch changes in the very clock where the line is first seen low: the start bit is on
   // the line one clock after the sender's phase is set (`u` = 2), `sw` is a register, and both go
   // through two flops. In `u` = 3 the two arrive together. The echo has been idle, so it takes
   // the new rate, but only after this frame, which it must echo at the old one: a rate change
   // landing in the clock where the line is seen low would start the frame with the old period
   // (HOLD=1) or the new one (HOLD=3), and the host's frame would be lost. One clock earlier the
   // switch does win, and the frame is lost on purpose (not tried). The frame is tried from
   // 921600 baud to 12 Mbaud and back.
   Reg#(Bit#(4)) current <- mkReg(8);
   Reg#(UInt#(4)) trial <- mkReg(0);
   Reg#(UInt#(4)) u <- mkReg(0);
   Stmt collide = seq
      for (trial <= 0; trial < 2; trial <= trial + 1) seq
         pause(quiet);
         action
            if (current == 8) setRate(8, 0); else setRate(0, 0);
            receiver.startCounting;
         endaction
         u <= 0;
         while (u < 8) action
            if (u == 1) hostByte(8'h00);
            if (u == 2) host.setPhase(0);
            if (u == 3) sw <= (current == 8) ? 4'd0 : 4'd8;
            u <= u + 1;
         endaction
         action hostByte(8'h01); endaction
         action hostByte(8'h02); endaction
         settle;
         pause(5 * 10 * (clkHz / baudOf(8)));   // the three frames come back at the old rate
         if (receiver.frames != 3 || receiver.missing != 0)
            $fatal(1, "FAIL: " + name + ": a switch meeting a start bit (%0d): %0d of 3 bytes came back", trial,
                   receiver.frames);
         current <= (current == 8) ? 4'd0 : 4'd8;
      endseq
   endseq;

   Stmt script = seq
      note("power", cycle); power;
      note("corners", cycle); corners;
      note("utilization", cycle); utilization;
      note("errors", cycle); errors;
      note("overrun", cycle); overrun;
      note("retune", cycle); retune;
      if (!fixedBaud) seq
         note("collide", cycle); collide;
      endseq
      action
         $display("done: %s, %0d cycles (%0d frames back to back)", name, cycle,
                  receiver.chained);
      endaction
   endseq;

   FSM fsm <- mkFSM(script);
   Reg#(Bool) started <- mkReg(False);

   rule begin_script (!started);
      fsm.start;
      started <= True;
   endrule

   method Bool _read = started && fsm.done;
endmodule

// The echo in the configuration `fixedBaud`, `transform`: the real top if the macros say so,
// `mkEcho` otherwise.
module mkBenchFor#(Bool fixedBaud, Bool transform)(ReadOnly#(Bool));
   Bool isTop = fixedBaud == configFixedBaud && transform == configEchoTransform;
   String name = (fixedBaud ? "fixed baud" : "switches") + ", "
                 + (transform ? "transformed" : "plain")
                 + " echo (" + (isTop ? "mkUartEcho" : "mkEcho") + ")";
   UartEcho dut;
   if (isTop)
      dut <- mkUartEcho;
   else
      dut <- mkEcho(fixedBaud, transform);
   ReadOnly#(Bool) bench <- mkBench(name, fixedBaud, transform, dut);
   return bench;
endmodule

// The four configurations, each a module of its own: bsc then schedules each by itself, which
// takes a fraction of the time of one module that holds all four.
interface BenchDone;
   (* always_ready *)
   method Bool done;
endinterface

module mkBenchModule#(Bool fixedBaud, Bool transform)(BenchDone);
   ReadOnly#(Bool) bench <- mkBenchFor(fixedBaud, transform);
   method Bool done = bench;
endmodule

(* synthesize *)
module mkBenchSwitchesPlain(BenchDone);
   BenchDone b <- mkBenchModule(False, False);
   return b;
endmodule

(* synthesize *)
module mkBenchSwitchesTransformed(BenchDone);
   BenchDone b <- mkBenchModule(False, True);
   return b;
endmodule

(* synthesize *)
module mkBenchFixedPlain(BenchDone);
   BenchDone b <- mkBenchModule(True, False);
   return b;
endmodule

(* synthesize *)
module mkBenchFixedTransformed(BenchDone);
   BenchDone b <- mkBenchModule(True, True);
   return b;
endmodule

(* synthesize *)
module mkTbUartEcho(Empty);
   BenchDone switchesPlain <- mkBenchSwitchesPlain;
   BenchDone switchesTransformed <- mkBenchSwitchesTransformed;
   BenchDone fixedPlain <- mkBenchFixedPlain;
   BenchDone fixedTransformed <- mkBenchFixedTransformed;
   Reg#(UInt#(32)) cycle <- mkReg(0);

   (* fire_when_enabled, no_implicit_conditions *)
   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles) fail("timeout");
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule finish (switchesPlain.done && switchesTransformed.done && fixedPlain.done
                && fixedTransformed.done);
      $display("PASS: switches and fixed baud rate, plain and transformed echo (four configurations, %0d cycles)",
               cycle);
      $finish(0);
   endrule
endmodule

endpackage
