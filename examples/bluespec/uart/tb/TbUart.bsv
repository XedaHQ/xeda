// Self-checking testbench for the UART PHY (mkUart and mkUartCore).
//
// Five DUTs run their scripts at the same time, each on its own bench (TbUartModels): a
// behavioral remote on rxd that can run at any baud offset and phase and inject glitches and
// bad frames, a checker on txd that holds every bit edge to the ideal grid, and a consumer that
// compares what rx delivers with what was sent.
//
//  A  mkUart, 8N1 at 12 Mbaud / 100 MHz (8.33 clocks per bit, bit period 25/3 clocks):
//     power-up with a low line; TX -> RX loopback at 100 % line utilization with random and corner
//     bytes (the checker verifies the zero gap and the exact long-run baud over the whole train);
//     the remote at baud offsets from -4.5 % to +3.5 %, three phases each, back-to-back, and far
//     outside the tolerance (the test must see errors there); framing error, breaks, glitches of
//     1 and 2 clocks (shorter than the hold filter) on an idle line and at the center of bits
//     (random, and every bit of a frame in turn), false starts, overrun, `clearFlags` in the very
//     cycle of an error event.
//  B  mkUartCore, 7E2 at 8 Mbaud (bit period 25/2 clocks): the same checks for another format,
//     with a parity error, a framing error and a break.
//  C  mkUartCore, 8O1 with a runtime bit period: 12 Mbaud, 3 Mbaud and 921600 baud in turn, each
//     with loopback and the remote slightly off; the error cases at 12 Mbaud. Then reset in the
//     middle of a transmitted and of a received frame, at every phase of the frame.
//  D  mkUartCore, 8N1 at 16 Mbaud (6.25 clocks per bit: the first receiver sample is one clock
//     earlier below 8 clocks per bit, and the hold filter is 2, the most that bit allows).
//  E  mkUartCore, 8N1 at 921600 baud, whose period does not fit the accumulator and is rounded.
//
// The buffer depths of A are the macros TX_DEPTH and RX_DEPTH of mkUart; the testbench follows
// them. A failed check ends the run with $fatal. `XEDA_INJECT_BUG` makes the receiver late to hunt
// again after a frame, which the baud offset tests catch; `XEDA_INJECT_BUG_TX` opens a gap between
// transmitted frames, which the checker catches.
package TbUart;

import Clocks :: *;
import Vector :: *;
import BuildVector :: *;
import StmtFSM :: *;
import UartTypes :: *;
import BitTimer :: *;
import Uart :: *;
import TbUartModels :: *;

`ifndef CLK_HZ
`define CLK_HZ 100000000
`endif
`ifndef BAUD
`define BAUD 12000000
`endif

`ifndef TX_DEPTH
`define TX_DEPTH 1
`endif
`ifndef RX_DEPTH
`define RX_DEPTH 4
`endif

Integer clkHz = `CLK_HZ;
UInt#(32) maxCycles = 600000;

function Action fail(String msg) = action $fatal(1, "FAIL: " + msg); endaction;

// progress messages, with -D TB_TRACE
function Action note(String msg) = action
`ifdef TB_TRACE
   $display(msg);
`endif
endaction;

// The counters of a bench against what a test expects.
function Action expectCounts(String where, Bench#(nd) b, Integer delivered, Integer fe,
                             Integer pe, Integer brk, Integer ov) = action
   if (b.count(Delivered) != fromInteger(delivered) || b.count(FrameErrors) != fromInteger(fe)
       || b.count(ParityErrors) != fromInteger(pe) || b.count(Breaks) != fromInteger(brk)
       || b.count(Overruns) != fromInteger(ov) || b.count(Mismatch) != 0)
      $fatal(1, "FAIL: " + where + ": delivered %0d (want %0d), framing %0d (%0d), "
                + "parity %0d (%0d), break %0d (%0d), overrun %0d (%0d), mismatches %0d",
             b.count(Delivered), delivered, b.count(FrameErrors), fe, b.count(ParityErrors), pe,
             b.count(Breaks), brk, b.count(Overruns), ov, b.count(Mismatch));
endaction;

function Action expectFlags(String where, Bench#(nd) b, Bool fe, Bool pe, Bool brk,
                            Bool ov) = action
   UartFlags f = b.flags;
   if (f.frameError != fe || f.parityError != pe || f.brk != brk || f.overrun != ov)
      $fatal(1, "FAIL: " + where + ": flags %b, want %b", pack(f), pack(UartFlags {
         frameError: fe, parityError: pe, brk: brk, overrun: ov }));
endaction;

Stmt noStatement = seq noAction; endseq;

Vector#(8, Bit#(8)) corner = vec(8'h00, 8'hFF, 8'h55, 8'h80, 8'h01, 8'hAA, 8'h7F, 8'hFE);

// ---------------------------------------------------------------------------------------
// Bench A: mkUart at 12 Mbaud.

module mkScriptA#(Bench#(8) b, Rng rng)(FSM);
   Integer baud = `BAUD;
   Integer bitClocks = (clkHz + baud - 1) / baud;          // whole clocks per bit, rounded up
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(16)) phaseNo <- mkReg(0);
   Reg#(UInt#(16)) j <- mkReg(0);
   Reg#(UInt#(32)) t <- mkReg(0);
   Reg#(UInt#(8)) glitchWait <- mkReg(0);
   Reg#(UInt#(8)) glitchLength <- mkReg(1);
   Reg#(UInt#(4)) glitchBit <- mkReg(0);

   function Stmt pause(Integer n) = seq
      t <= 0;
      while (t < fromInteger(n)) t <= t + 1;
   endseq;

   // let the DUT finish what the remote sent: the last bit, the frame, the delivery
   Stmt settle = seq
      await(b.lineIdle);
      pause(14 * bitClocks);
   endseq;

   function Action sendGood(Bit#(8) d) = action
      b.expectByte(d);
      b.sendLine(frameWord(d, NoParity, 1, False, False), 10);
   endaction;

   function Action sendRaw(Bit#(16) bits, Integer n) = action
      b.sendLine(bits, fromInteger(n));
   endaction;

   function Action sendUnexpected(Bit#(8) d) = sendRaw(frameWord(d, NoParity, 1, False, False), 10);

   Integer loopN = 240;
   Stmt loopback = seq
      b.loopback(True);
      b.resetCounters;
      for (i <= 0; i < fromInteger(loopN); i <= i + 1) action
         let r <- rng.next;
         Bit#(8) d = (i < 8) ? corner[i] : r[7:0];
         b.sendByte(d, i != 0);
         b.expectByte(d);
      endaction
      await(b.txIdle);
      pause(200);
      expectCounts("A loopback", b, loopN, 0, 0, 0, 0);
      if (b.count(RxBytes) != fromInteger(loopN) || b.count(TxBytes) != fromInteger(loopN))
         fail("A loopback: wrong event counts");
      if (b.txFrames != fromInteger(loopN)) fail("A loopback: the checker saw too few frames");
      b.loopback(False);
   endseq;

   // The remote at baud offsets, in per mille; the last two are far outside the tolerance. Each
   // takes three phases, with a run of back-to-back frames in each.
   Vector#(9, Integer) permilles = vec(-45, -30, -15, 0, 15, 30, 35, 70, -80);
   Vector#(9, Fx) offsetPeriods = map(periodFx(clkHz, baud), permilles);
   Reg#(UInt#(8)) o <- mkReg(0);

   Stmt offsets = seq
   for (o <= 0; o < 9; o <= o + 1) seq
      action
         Bool outside = o >= 7;
         note("A: offset");
         b.rxPeriod(offsetPeriods[o]);
         b.resetCounters;
         b.tolerant(outside);
      endaction
      for (phaseNo <= 0; phaseNo < 3; phaseNo <= phaseNo + 1) seq
         action
            let r <- rng.next;
            b.phase(unpack(r[15:0]));
         endaction
         for (j <= 0; j < ((o >= 7) ? 12 : 30); j <= j + 1) action
            let r <- rng.next;
            sendGood((j == 0) ? corner[phaseNo] : r[7:0]);
         endaction
         settle;
      endseq
      if (o >= 7) seq
         // far outside the tolerance the receiver must get frames wrong
         if (b.count(Mismatch) == 0 && b.count(FrameErrors) == 0 && b.count(Delivered) == 36)
            fail("A: a baud offset far outside the tolerance caused no error");
         b.flushExpected;
         b.tolerant(False);
         b.clearFlags;
         pause(20 * bitClocks);
      endseq
      else action
         if (b.count(Delivered) != 90 || b.count(FrameErrors) != 0 || b.count(ParityErrors) != 0
             || b.count(Breaks) != 0 || b.count(Overruns) != 0 || b.count(Mismatch) != 0)
            $fatal(1, "FAIL: A baud offset run %0d (%0d per mille): delivered %0d of 90, "
                      + "framing %0d, parity %0d, break %0d, overrun %0d, mismatches %0d",
                   o, permilles[o], b.count(Delivered), b.count(FrameErrors), b.count(ParityErrors),
                   b.count(Breaks), b.count(Overruns), b.count(Mismatch));
      endaction
   endseq
   endseq;

   Stmt framing = seq
      // a framing error, then a line that idles, then good frames: one error, no byte, resync
      b.resetCounters;
      b.rxPeriod(periodFx(clkHz, baud, 0));
      b.clearAtEvent;   // and `clearFlags` comes in the very cycle of the framing error
      action
         b.phase(16'h4000);
         sendRaw(frameWord(8'hA5, NoParity, 1, False, True), 10);
      endaction
      sendRaw(16'hFFFF, 3);
      for (i <= 0; i < 5; i <= i + 1) action
         let r <- rng.next;
         sendGood(r[7:0]);
      endaction
      settle;
      expectCounts("A framing error", b, 5, 1, 0, 0, 0);
      expectFlags("A framing error", b, True, False, False, False);
      if (!b.collided) fail("A framing error: the flags were not cleared in the cycle of the event");
      b.clearFlags;
      pause(2);
      expectFlags("A cleared flags", b, False, False, False, False);
   endseq;

   Stmt breaks = seq
      // breaks: one event per break, however long, and no byte; a 12 bit long one first
      b.resetCounters;
      b.clearAtEvent;
      sendRaw(16'h0000, 12);
      sendRaw(16'hFFFF, 4);
      sendGood(8'h3C);
      settle;
      expectCounts("A short break", b, 1, 0, 0, 1, 0);
      expectFlags("A short break", b, False, False, True, False);
      if (!b.collided) fail("A short break: the flags were not cleared in the cycle of the event");
      b.clearFlags;
      b.resetCounters;
      for (i <= 0; i < 3; i <= i + 1) sendRaw(16'h0000, 16);
      sendRaw(16'hFFFF, 4);
      sendGood(8'hC3);
      sendGood(8'h96);
      settle;
      expectCounts("A long break", b, 2, 0, 0, 1, 0);
      expectFlags("A long break", b, False, False, True, False);
      b.clearFlags;
   endseq;

   Stmt glitches = seq
      // low glitches on an idle line, 1 and 2 clocks (the filter holds 3), and false starts of 3
      // and 4 clocks, which pass the filter but are no longer low at the center of the start bit
      b.resetCounters;
      for (i <= 0; i < 12; i <= i + 1) seq
         action b.glitch(0, truncate(i % 2) + 1); endaction
         pause(40);
      endseq
      for (i <= 0; i < 4; i <= i + 1) seq
         action b.glitch(0, 3 + truncate(i % 2)); endaction
         pause(60);
      endseq
      expectCounts("A idle glitches", b, 0, 0, 0, 0, 0);

      // glitches of 1 and 2 clocks at the center of a random bit of a frame (or just before it)
      b.resetCounters;
      for (i <= 0; i < 60; i <= i + 1) seq
         action
            let r <- rng.next;
            sendGood(r[7:0]);
            glitchWait <= unpack(zeroExtend(r[11:8])) % 11;   // 0 to 10 bits later
            glitchLength <= (r[16] == 1) ? 2 : 1;
         endaction
         while (glitchWait != 0) seq
            pause(bitClocks);
            glitchWait <= glitchWait - 1;
         endseq
         action b.glitchMidBit(glitchLength); endaction
         settle;
      endseq
      expectCounts("A glitches in frames", b, 60, 0, 0, 0, 0);
      if (b.midBitGlitches != 60) fail("A: the glitches were not all put on the line");

      // the same at the center of every bit of a frame in turn, start bit to stop bit
      b.resetCounters;
      for (glitchLength <= 1; glitchLength <= 2; glitchLength <= glitchLength + 1)
         for (glitchBit <= 0; glitchBit < 10; glitchBit <= glitchBit + 1) seq
            action
               b.glitchFrameBit(glitchBit, glitchLength);
               sendGood(8'h39);
            endaction
            settle;
         endseq
      expectCounts("A glitch matrix", b, 20, 0, 0, 0, 0);
      if (b.midBitGlitches != 80) fail("A: the matrix glitches were not all put on the line");
   endseq;

   Integer rxDepth = `RX_DEPTH;
   Stmt overrun = seq
      // the consumer stalls: the first RX_DEPTH bytes are kept, in order, the rest is dropped
      b.resetCounters;
      b.stall(True);
      for (i <= 0; i < fromInteger(rxDepth + 3); i <= i + 1) action
         Bit#(8) d = 8'h10 + truncate(pack(i));
         if (i < fromInteger(rxDepth)) b.expectByte(d);
         sendUnexpected(d);
      endaction
      settle;
      expectCounts("A overrun, stalled", b, 0, 0, 0, 0, 3);
      expectFlags("A overrun", b, False, False, False, True);
      // one more overrun, with the flags cleared in the very cycle of its event: still flagged
      b.clearFlags;
      pause(2);
      expectFlags("A cleared flags", b, False, False, False, False);
      b.clearAtEvent;
      sendUnexpected(8'h77);
      settle;
      expectCounts("A overrun, cleared at the event", b, 0, 0, 0, 0, 4);
      expectFlags("A overrun, cleared at the event", b, False, False, False, True);
      if (!b.collided) fail("A overrun: the flags were not cleared in the cycle of the event");
      b.stall(False);
      pause(20);
      expectCounts("A overrun, drained", b, rxDepth, 0, 0, 0, 4);
      b.clearFlags;
      // and it recovers
      sendGood(8'h99);
      sendGood(8'h66);
      settle;
      expectCounts("A after overrun", b, rxDepth + 2, 0, 0, 0, 4);
      expectFlags("A after overrun", b, False, False, False, False);
   endseq;

   Stmt start = seq
      b.txPeriod(periodFx(clkHz, baud, 0));
      // power-up: the line is low from reset on and then goes high; no frame, no event
      pause(300);
      expectCounts("A low line at power-up", b, 0, 0, 0, 0, 0);
      b.releaseLine;
      pause(300);
      expectCounts("A line high after power-up", b, 0, 0, 0, 0, 0);
      loopback;
      pause(100);
   endseq;

   FSM fsm <- mkFSM(seq start; offsets; framing; breaks; glitches; overrun; endseq);
   return fsm;
endmodule

// ---------------------------------------------------------------------------------------
// Benches B and C: any format, one or more baud rates. Per rate: loopback with the checker on
// txd, and the remote a little off in both directions with frames of one and of the full number of
// stop bits mixed; at the first rate also, if `extras`, parity and framing errors and a break.

module mkFormatScript#(String name, Bench#(nd) b, Rng rng, Parity parity, Integer stops,
                       Integer clk, Vector#(nr, Integer) bauds, Vector#(nr, Integer) loopFrames,
                       Vector#(nr, Integer) runFrames, Integer permille, Bool extras,
                       function Action selectRate(Integer r),
                       function Fx txPeriodOf(Integer r), Stmt tail)(FSM)
   provisos (Add#(nd, _a, 32));
   Integer nData = valueOf(nd);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(32)) t <- mkReg(0);

   function Stmt pause(Integer n) = seq
      t <= 0;
      while (t < fromInteger(n)) t <= t + 1;
   endseq;

   function Bit#(nd) pick(UInt#(16) k, Bit#(32) r);
      Bit#(nd) top = 1 << (nData - 1);
      Bit#(nd) c = case (k)
                      0: 0;
                      1: '1;
                      2: truncate(32'h55555555);
                      3: top;
                      4: 1;
                      default: truncate(r);
                   endcase;
      return c;
   endfunction

   function Action sendLine(Bit#(nd) d, Bool badParity, Bool badStop, Integer st) = action
      b.sendLine(frameWord(d, parity, st, badParity, badStop),
                 fromInteger(1 + nData + (parity == NoParity ? 0 : 1) + st));
   endaction;

   function Stmt rateSection(Integer r, Bool withExtras);
      Integer baud = bauds[r];
      Integer bitClocks = (clk + baud - 1) / baud;
      Stmt settle = seq
         await(b.lineIdle);
         pause(16 * bitClocks);
      endseq;
      function Stmt remoteRun(Integer permille) = seq
         b.rxPeriod(periodFx(clk, baud, permille));
         b.resetCounters;
         action
            let x <- rng.next;
            b.phase(unpack(x[15:0]));
         endaction
         for (i <= 0; i < fromInteger(runFrames[r]); i <= i + 1) action
            let x <- rng.next;
            Bit#(nd) d = pick(i, x);
            b.expectByte(d);
            if (i % 2 == 0) sendLine(d, False, False, stops);
            else sendLine(d, False, False, 1);
         endaction
         settle;
         expectCounts(name + " remote off at " + integerToString(baud), b, runFrames[r],
                      0, 0, 0, 0);
      endseq;
      Stmt s = seq
         note(name + ": rate " + integerToString(baud));
         selectRate(r);
         b.txPeriod(txPeriodOf(r));
         b.rxPeriod(periodFx(clk, baud, 0));
         pause(20 * bitClocks);

         // loopback with the checker on txd
         b.loopback(True);
         b.resetCounters;
         for (i <= 0; i < fromInteger(loopFrames[r]); i <= i + 1) action
            let x <- rng.next;
            Bit#(nd) d = pick(i, x);
            b.sendByte(d, i != 0);
            b.expectByte(d);
         endaction
         await(b.txIdle);
         pause(10 * bitClocks);
         expectCounts(name + " loopback at " + integerToString(baud), b, loopFrames[r], 0, 0, 0, 0);
         b.loopback(False);

         // the remote a little fast, then a little slow
         remoteRun(permille);
         remoteRun(-permille);
         b.rxPeriod(periodFx(clk, baud, 0));
      endseq;
      if (withExtras) s = seq
         s;
         // a parity error: the byte is dropped, the next ones are fine; the flags are cleared in
         // the very cycle of the error event, and it still sets its flag
         b.resetCounters;
         b.clearAtEvent;
         action
            let x <- rng.next;
            Bit#(nd) d = truncate(x);
            b.expectByte(d);
            sendLine(d, False, False, stops);
         endaction
         action sendLine('1, True, False, stops); endaction
         action
            let x <- rng.next;
            Bit#(nd) d = truncate(x);
            b.expectByte(d);
            sendLine(d, False, False, stops);
         endaction
         settle;
         expectCounts(name + " parity error", b, 2, 0, 1, 0, 0);
         expectFlags(name + " parity error", b, False, True, False, False);
         if (!b.collided) fail(name + " parity error: the flags were not cleared in the cycle of the event");
         b.clearFlags;
         // a framing error with good parity, then idle, then a good frame
         b.resetCounters;
         action sendLine(truncate(32'h2D), False, True, stops); endaction
         b.sendLine('1, 3);
         action
            b.expectByte(truncate(32'h1B));
            sendLine(truncate(32'h1B), False, False, stops);
         endaction
         settle;
         expectCounts(name + " framing error", b, 1, 1, 0, 0, 0);
         expectFlags(name + " framing error", b, True, False, False, False);
         b.clearFlags;
         // a break
         b.resetCounters;
         b.sendLine(0, 16);
         b.sendLine(0, 16);
         b.sendLine('1, 4);
         action
            b.expectByte(truncate(32'h66));
            sendLine(truncate(32'h66), False, False, stops);
         endaction
         settle;
         expectCounts(name + " break", b, 1, 0, 0, 1, 0);
         b.clearFlags;
      endseq;
      return s;
   endfunction

   Stmt script = rateSection(0, extras);
   for (Integer r = 1; r < valueOf(nr); r = r + 1)
      script = seq
         script;
         rateSection(r, False);
      endseq;

   FSM fsm <- mkFSM(seq script; tail; note(name + ": done"); endseq);
   return fsm;
endmodule

// ---------------------------------------------------------------------------------------
// Reset in the middle of a frame (an 8O1 DUT at 12 Mbaud, 11 bits or 92 clocks per frame), cut
// every 6 clocks from the start to the end of the frame.
//  * transmit: two bytes are queued, one is on the line; two clocks after the reset txd is high
//    and the transmitter idle, the second byte never appears (the checker would see it), and the
//    next frame is clean and on the bit grid.
//  * receive: the remote sends 0xFF, a start bit followed by ones. Whatever part the receiver
//    has seen, the reset throws it away: no byte, no event, no flag after it (what the DUT did
//    before the reset reached it does not count).

function Stmt resetScript(Bench#(8) b, MakeResetIfc rst, Reg#(UInt#(8)) cut, Reg#(UInt#(32)) t,
                          Action prepare, Integer bitClocks);
   function Stmt pause(Integer n) = seq
      t <= 0;
      while (t < fromInteger(n)) t <= t + 1;
   endseq;

   Stmt waitCut = seq
      t <= 0;
      while (t < zeroExtend(cut)) t <= t + 1;
   endseq;

   Stmt transmit = seq
      action b.sendByte(8'hA5, False); endaction
      action b.sendByte(8'h5A, True); endaction
      waitCut;
      action
         rst.assertReset;
         b.forgetFrames;
      endaction
      pause(2);
      if (b.txLine != 1 || b.txBusy) fail("reset: the transmitter is not idle two clocks later");
      // nothing but silence on the line, then a clean frame
      pause(20 * bitClocks);
      action b.sendByte(8'h3C, False); endaction
      await(b.txIdle);
   endseq;

   Stmt receive = seq
      b.tolerant(True);   // a reset after the stop bit comes too late: 0xFF has been delivered
      action
         b.phase(0);   // the frame starts in the next clock
         b.sendLine(frameWord(8'hFF, OddParity, 1, False, False), 11);
      endaction
      waitCut;
      rst.assertReset;
      pause(3);   // what the DUT did before the reset reached it does not count
      b.resetCounters;
      await(b.lineIdle);
      pause(20 * bitClocks);
      expectCounts("reset in a received frame", b, 0, 0, 0, 0, 0);
      expectFlags("reset in a received frame", b, False, False, False, False);
      b.tolerant(False);
   endseq;

   Stmt after = seq
      b.resetCounters;
      action
         b.expectByte(8'h96);
         b.sendLine(frameWord(8'h96, OddParity, 1, False, False), 11);
      endaction
      await(b.lineIdle);
      pause(14 * bitClocks);
      expectCounts("after a reset in a frame", b, 1, 0, 0, 0, 0);
   endseq;

   return seq
      prepare;
      for (cut <= 3; cut < 100; cut <= cut + 6) seq
         transmit;
         receive;
      endseq
      after;
   endseq;
endfunction

// ---------------------------------------------------------------------------------------

(* synthesize *)
module mkTbUart(Empty);
   Reset benchReset <- exposeCurrentReset;
   // A: the synthesizable mkUart
   Rng rngA <- mkRng(32'h2545F491);
   Uart#(8) dutA <- mkUart;
   Bench#(8) benchA <- mkBench("A", dutA, benchReset, 0, NoParity, 1, True,
                               inFlight(`TX_DEPTH, `RX_DEPTH));
   FSM scriptA <- mkScriptA(benchA, rngA);

   function Action keepRate(Integer r) = noAction;
   function Fx exactPeriod(Integer r) = periodFx(100000000, 8000000, 0);

   // B: 7E2 at 8 Mbaud, buffers of other depths, a hold filter of 2
   Rng rngB <- mkRng(32'h9E3779B9);
   UartFormat#(7, 1, 2) fmtB = uartFormat(EvenParity);
   BitTiming#(StaticCntBits#(100000000, 8000000, 1), 1) timingB =
      staticTiming(100000000, 8000000, defaultAdjust(100000000, 8000000));
   Depth#(1) depthTxB = depth;
   Depth#(2) depthRxB = depth;
   Uart#(7) dutB <- mkUartCore(timingB, fmtB, 2, depthTxB, depthRxB);
   Bench#(7) benchB <- mkBench("B", dutB, benchReset, 0, EvenParity, 2, False, inFlight(1, 2));
   FSM scriptB <- mkFormatScript("B", benchB, rngB, EvenParity, 2, 100000000,
                                 vec(8000000), vec(60), vec(20), 30, True, keepRate, exactPeriod,
                                 noStatement);

   // C: 8O1 with a runtime bit period
   Rng rngC <- mkRng(32'h6A09E667);
   UartFormat#(8, 1, 1) fmtC = uartFormat(OddParity);
   Vector#(3, Integer) bauds = vec(12000000, 3000000, 921600);
   Vector#(3, BitPeriod#(10, 6)) periods = map(bitPeriod(100000000), bauds);
   Reg#(BitPeriod#(10, 6)) period <- mkRegU;   // no reset: the DUT is in another reset domain
   Depth#(2) depthTxC = depth;
   Depth#(4) depthRxC = depth;
   // the fastest rate is 12 Mbaud: 8 clocks in the shortest bit
   Clock clk <- exposeCurrentClock;
   MakeResetIfc resetC <- mkReset(0, True, clk);   // to reset C in the middle of a frame
   Uart#(8) dutC <- mkUartCore(runtimeTiming(period, 0, shortestBit(100000000, bauds[0], 6)),
                               fmtC, 3, depthTxC, depthRxC, reset_by resetC.new_rst);
   Bench#(8) benchC <- mkBench("C", dutC, resetC.new_rst, 1, OddParity, 1, False,
                               inFlight(2, 4));
   function Action selectRate(Integer r) = action period <= periods[r]; endaction;
   // the period register holds D-1 and 6 fraction bits: the DUT's baud is rounded to that
   function Fx registered(Integer r) =
      (fromInteger(valueOf(TExp#(6))) * (unpack(zeroExtend(periods[r].dm1)) + 1)
       + unpack(zeroExtend(periods[r].frac))) * oneClock / fromInteger(valueOf(TExp#(6)));
   // after its rates, C goes back to 12 Mbaud and is reset in the middle of frames
   Reg#(UInt#(8)) cutC <- mkReg(0);
   Reg#(UInt#(32)) waitC <- mkReg(0);
   Action twelveMbaud = action
      period <= periods[0];
      benchC.txPeriod(registered(0));
      benchC.rxPeriod(periodFx(100000000, bauds[0], 0));
   endaction;
   FSM scriptC <- mkFormatScript("C", benchC, rngC, OddParity, 1, 100000000, bauds,
                                 vec(24, 16, 6), vec(24, 10, 4), 30, True, selectRate, registered,
                                 resetScript(benchC, resetC, cutC, waitC, twelveMbaud, 9));

   // D: 8N1 at 16 Mbaud, 6.25 clocks per bit. Below 8 clocks per bit the receiver samples one
   // clock earlier (defaultAdjust), and 6 clocks in the shortest bit allow a hold filter of 2.
   Rng rngD <- mkRng(32'hBB67AE85);
   UartFormat#(8, 0, 1) fmtD = uartFormat(NoParity);
   BitTiming#(StaticCntBits#(100000000, 16000000, 2), 2) timingD =
      staticTiming(100000000, 16000000, defaultAdjust(100000000, 16000000));
   Depth#(1) depthTxD = depth;
   Depth#(4) depthRxD = depth;
   Uart#(8) dutD <- mkUartCore(timingD, fmtD, 2, depthTxD, depthRxD);
   Bench#(8) benchD <- mkBench("D", dutD, benchReset, 0, NoParity, 1, False, inFlight(1, 4));
   function Fx exactPeriodD(Integer r) = periodFx(100000000, 16000000, 0);
   FSM scriptD <- mkFormatScript("D", benchD, rngD, NoParity, 1, 100000000, vec(16000000),
                                 vec(40), vec(20), 20, False, keepRate, exactPeriodD,
                                 noStatement);

   // E: 8N1 at 921600 baud, a bit period of 108.507 clocks that does not fit the accumulator:
   // it is rounded to 108.5 clocks
   Rng rngE <- mkRng(32'h3C6EF372);
   BitTiming#(StaticCntBits#(100000000, 921600, 6), 6) timingE =
      staticTiming(100000000, 921600, defaultAdjust(100000000, 921600));
   Depth#(2) depthE = depth;
   Uart#(8) dutE <- mkUartCore(timingE, fmtD, 3, depthE, depthE);
   Bench#(8) benchE <- mkBench("E", dutE, benchReset, 0, NoParity, 1, False, inFlight(2, 2));
   function Fx roundedPeriodE(Integer r) = clocksFx(108) + oneClock / 2;
   FSM scriptE <- mkFormatScript("E", benchE, rngE, NoParity, 1, 100000000, vec(921600),
                                 vec(12), vec(6), 20, False, keepRate, roundedPeriodE,
                                 noStatement);

   Reg#(UInt#(32)) cycle <- mkReg(0);
   Reg#(Bool) started <- mkReg(False);

   (* fire_when_enabled, no_implicit_conditions *)
   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles)
         $fatal(1, "FAIL: timeout after %0d cycles", cycle);
   endrule

   rule go (!started);
      scriptA.start;
      scriptB.start;
      scriptC.start;
      scriptD.start;
      scriptE.start;
      started <= True;
   endrule

   rule finish (started && cycle > 8 && scriptA.done && scriptB.done && scriptC.done
                   && scriptD.done && scriptE.done);
      $display("PASS: UART PHY checked in %0d cycles; A: %0d frames on txd, worst edge %0d/65536",
               cycle, benchA.txFrames, benchA.maxEdgeDeviation);
      $finish(0);
   endrule
endmodule

endpackage
