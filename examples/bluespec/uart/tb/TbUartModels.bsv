// Models for the UART testbench: a pseudo-random generator, a behavioral remote that drives a
// line with a chosen bit period and phase, a checker that watches a line the DUT drives, and
// a bench that wires those to one DUT.
//
// Time is fixed point: `Fx` counts 1/65536 clock, so a remote can run at any baud offset and
// start at any phase between two clock edges. The model's "line" is what a pin shows right after
// a clock edge, which is the only time a clocked receiver can look at it.
package TbUartModels;

import FIFOF :: *;
import GetPut :: *;
import Vector :: *;
import Clocks :: *;
import UartTypes :: *;
import Uart :: *;

export Fx;
export oneClock;
export clocksFx;
export clocksOf;
export periodFx;
export Rng(..);
export mkRng;
export frameWord;
export frameLen;
export LineSender(..);
export mkLineSender;
export LineChecker(..);
export mkLineChecker;
export inFlight;
export Counter(..);
export Bench(..);
export mkBench;

module mkUartPinResetBridge#(Integer enabled, UartPins pins, ReadOnly#(Bit#(1)) remoteLine,
                             ReadOnly#(Bool) looping, ReadOnly#(Bool) forcedLow)(Empty);
   if (enabled == 1) begin
      (* fire_when_enabled, no_implicit_conditions *)
      rule driveRx;
         pins.rxd(forcedLow._read ? 0 : (looping._read ? pins.txd : remoteLine._read));
      endrule
   end
endmodule

module mkUartClearResetBridge#(Integer enabled, Uart#(nd) dut, RWire#(Bool) clear)(Empty);
   if (enabled == 1) begin
      rule clearFlags (clear.wget matches tagged Valid True);
         dut.clearFlags;
      endrule
   end
endmodule

module mkUartRxResetBridge#(Integer enabled, Uart#(nd) dut, RWire#(Bit#(nd)) rxData,
                            ReadOnly#(Bool) stalled)(Empty);
   if (enabled == 1) begin
      rule receive (!stalled._read);
         let b <- dut.rx.get;
         rxData.wset(b);
      endrule
   end
endmodule

module mkUartEventsResetBridge#(Integer enabled, Uart#(nd) dut, RWire#(UartEvents) events)(Empty);
   if (enabled == 1) begin
      rule sampleEvents;
         events.wset(dut.events);
      endrule
   end
endmodule

module mkUartTxResetBridge#(Integer enabled, Uart#(nd) dut, FIFOF#(Bit#(nd)) txData)(Empty);
   if (enabled == 1) begin
      rule transmit (txData.notEmpty);
         dut.tx.put(txData.first);
         txData.deq;
      endrule
   end
endmodule

typedef Int#(40) Fx;

Fx oneClock = 65536;

function Fx clocksFx(Integer n) = fromInteger(n) * oneClock;
function Fx clocksOf(UInt#(8) n) = unpack(zeroExtend(pack(n))) * oneClock;

// The most bytes a DUT with these buffer depths can hold at once (its buffers, the transmitter
// and the receiver, and one more); the testbench queues of expected bytes and frames have that
// room, so that no buffer depth can overflow them.
function Integer inFlight(Integer txDepth, Integer rxDepth) = txDepth + rxDepth + 3;

// The bit period of a remote whose baud rate is `permille` per mille off the nominal one.
function Fx periodFx(Integer clockHz, Integer baud, Integer permille) =
   fromInteger((clockHz * 65536 * 1000) / (baud * (1000 + permille)));

// ---------------------------------------------------------------------------------------
// A xorshift generator.

interface Rng;
   method ActionValue#(Bit#(32)) next;
endinterface

module mkRng#(Bit#(32) seed)(Rng);
   Reg#(Bit#(32)) state <- mkReg(seed);

   method ActionValue#(Bit#(32)) next;
      Bit#(32) x = state;
      x = x ^ (x << 13);
      x = x ^ (x >> 17);
      x = x ^ (x << 5);
      state <= x;
      return x;
   endmethod
endmodule

// ---------------------------------------------------------------------------------------
// Frames as bits on the wire, first bit in bit 0: start, data (LSB first), parity, stop bits.
// This is the testbench's own idea of a frame, independent of the DUT's.

function Bit#(16) frameWord(Bit#(nd) data, Parity parity, Integer stops,
                            Bool badParity, Bool badStop);
   Bit#(16) w = 0;
   Integer pos = 1;
   for (Integer i = 0; i < valueOf(nd); i = i + 1) begin
      w[pos] = data[i];
      pos = pos + 1;
   end
   if (parity != NoParity) begin
      Bit#(1) odd = (parity == OddParity) ? 1 : 0;
      w[pos] = reduceXor(data) ^ odd ^ (badParity ? 1 : 0);
      pos = pos + 1;
   end
   for (Integer s = 0; s < stops; s = s + 1) begin
      w[pos] = (s == 0 && badStop) ? 0 : 1;
      pos = pos + 1;
   end
   return w;
endfunction

function UInt#(5) frameLen(Integer nData, Parity parity, Integer stops) =
   fromInteger(1 + nData + (parity != NoParity ? 1 : 0) + stops);

// ---------------------------------------------------------------------------------------
// The remote: sends words of raw bits at a bit period, idles high in between and can force
// the line for a few clocks (a glitch). Its bit clock runs all the time and carries its
// remainder from bit to bit, so a back-to-back stream has the exact baud offset.

interface LineSender;
   method Bit#(1) line;
   method Action setPeriod(Fx p);
   // Align the bit clock: the next bit boundary is the next clock edge, and the real edge
   // lies f/65536 clock before it. Call when the sender is idle.
   method Action setPhase(UInt#(16) f);
   method Action send(Bit#(16) bits, UInt#(5) n);
   method Action glitch(Bit#(1) level, UInt#(8) clocks);
   // Invert the line for `clocks` clocks at the center of the next bit that starts: the farthest
   // a glitch can be from the edges of that bit.
   method Action glitchMidBit(UInt#(8) clocks);
   // The same in bit `k` of the next frame that is sent (0 is the start bit), however long the
   // line idles first.
   method Action glitchFrameBit(UInt#(4) k, UInt#(8) clocks);
   // The number of mid-bit glitches that have been put on the line.
   method UInt#(16) midBitGlitches;
   // Nothing is queued and the last bit has started.
   method Bool idle;
endinterface

module mkLineSender(LineSender);
   Reg#(Fx) period <- mkReg(clocksFx(8));
   Reg#(Fx) remaining <- mkReg(clocksFx(8));
   Reg#(Bit#(1)) current <- mkReg(1);
   Reg#(Bit#(16)) word <- mkReg(0);
   Reg#(UInt#(5)) left <- mkReg(0);
   FIFOF#(Tuple2#(Bit#(16), UInt#(5))) queue <- mkUGFIFOF;
   Reg#(UInt#(8)) glitchLeft <- mkReg(0);
   Reg#(Bit#(1)) glitchLevel <- mkReg(1);
   Reg#(Bit#(1)) pin <- mkReg(1);
   RWire#(Fx) phase <- mkRWire;
   RWire#(Tuple2#(Bit#(1), UInt#(8))) glitchReq <- mkRWire;
   Reg#(UInt#(2)) mid <- mkReg(0);               // 1: armed, 2: in the bit that gets the glitch
   Reg#(UInt#(8)) midClocks <- mkReg(1);
   Reg#(UInt#(4)) midSkip <- mkReg(0);           // frame bits to let pass first
   Reg#(Bool) midInFrame <- mkReg(False);        // count frame bits only, not idle bits
   Reg#(UInt#(16)) midCount <- mkReg(0);
   RWire#(Tuple3#(UInt#(8), UInt#(4), Bool)) midReq <- mkRWire;

   (* fire_when_enabled, no_implicit_conditions *)
   rule run;
      Fx rem = remaining - oneClock;
      Bool boundary = rem <= 0;
      Bit#(1) nextBit = current;
      Bit#(16) nextWord = word;
      UInt#(5) nextLeft = left;
      Bool frameBit = False;
      if (boundary) begin
         rem = rem + period;
         if (left != 0) begin
            nextBit = word[0];
            nextWord = word >> 1;
            nextLeft = left - 1;
            frameBit = True;
         end
         else if (queue.notEmpty) begin
            match {.bits, .n} = queue.first;
            queue.deq;
            nextBit = bits[0];
            nextWord = bits >> 1;
            nextLeft = n - 1;
            frameBit = True;
         end
         else
            nextBit = 1;
      end
      remaining <= fromMaybe(rem, phase.wget);
      current <= nextBit;
      word <= nextWord;
      left <= nextLeft;

      // A glitch in the middle of a bit: the bit that starts after `glitchMidBit` gets it, as soon
      // as its remaining time has fallen to half a period plus half the glitch.
      UInt#(2) nextMid = mid;
      Maybe#(Tuple2#(Bit#(1), UInt#(8))) req = glitchReq.wget;
      if (midReq.wget matches tagged Valid {.clocks, .skip, .inFrame}) begin
         nextMid = 1;
         midClocks <= clocks;
         midSkip <= skip;
         midInFrame <= inFrame;
      end
      else if (mid == 1 && boundary && (frameBit || !midInFrame)) begin
         nextMid = (midSkip == 0) ? 2 : 1;
         if (midSkip != 0) midSkip <= midSkip - 1;
      end
      else if (mid == 2 && !boundary && rem <= period / 2 + (clocksOf(midClocks) / 2)) begin
         req = tagged Valid tuple2(~nextBit, midClocks);
         nextMid = 0;
         midCount <= midCount + 1;
      end
      mid <= nextMid;

      UInt#(8) gl = (glitchLeft == 0) ? 0 : glitchLeft - 1;
      Bit#(1) gv = glitchLevel;
      if (req matches tagged Valid {.level, .clocks}) begin
         gl = clocks - 1;
         gv = level;
      end
      glitchLeft <= gl;
      glitchLevel <= gv;
      pin <= (glitchLeft != 0 || isValid(req)) ? gv : nextBit;   // `clocks` clocks in all
   endrule

   method Bit#(1) line = pin;
   method Action setPeriod(Fx p) = period._write(p);
   method Action setPhase(UInt#(16) f) = phase.wset(unpack(zeroExtend(pack(f))) + 1);
   method Action send(Bit#(16) bits, UInt#(5) n) if (queue.notFull);
      queue.enq(tuple2(bits, n));
   endmethod
   method Action glitch(Bit#(1) level, UInt#(8) clocks) = glitchReq.wset(tuple2(level, clocks));
   method Action glitchMidBit(UInt#(8) clocks) = midReq.wset(tuple3(clocks, 0, False));
   method Action glitchFrameBit(UInt#(4) k, UInt#(8) clocks) = midReq.wset(tuple3(clocks, k, True));
   method UInt#(16) midBitGlitches = midCount;
   method Bool idle = left == 0 && !queue.notEmpty;
endmodule

// ---------------------------------------------------------------------------------------
// Watches a line that the DUT drives and checks it against the frames it is told to expect.
// A train of frames starts at the first low clock after idle, which is the time reference.
// From then on
//  * every edge must lie within half a clock of the ideal bit grid, and
//  * every bit, sampled at its ideal center, must be the expected one.
// A frame marked "continuous" must follow the previous one with no gap, on the same grid, so
// the checker verifies the exact long-run baud rate and the zero gap at the same time.

typedef struct {
   Bit#(16) bits;
   UInt#(5) n;
   Bool continuous;
} Expected deriving (Bits, Eq, FShow);

interface LineChecker;
   (* always_ready, always_enabled *)
   method Action line(Bit#(1) v);
   method Action setPeriod(Fx p);
   method Action expectFrame(Bit#(16) bits, UInt#(5) n, Bool continuous);
   // Forget the frame in progress and every expected one, and ignore the line for a few clocks:
   // the DUT was reset.
   method Action abort;
   method UInt#(32) frames;
   // The largest distance of an edge from the grid so far, in 1/65536 clock.
   method Fx maxDeviation;
   // No train in progress and nothing expected.
   method Bool idle;
endinterface

module mkLineChecker#(String name, Integer room)(LineChecker);
   Wire#(Bit#(1)) pin <- mkBypassWire;
   Reg#(Bit#(1)) previous <- mkReg(1);
   Reg#(Fx) now <- mkReg(0);
   Reg#(Fx) period <- mkReg(clocksFx(8));
   Reg#(Bool) inTrain <- mkReg(False);
   Reg#(Fx) start <- mkReg(0);          // the time reference of the train
   Reg#(Fx) nextSample <- mkReg(0);
   Reg#(Bit#(16)) bits <- mkReg(0);
   Reg#(UInt#(5)) length <- mkReg(0);
   Reg#(UInt#(5)) index <- mkReg(0);
   Reg#(UInt#(32)) done <- mkReg(0);
   Reg#(Fx) worst <- mkReg(0);
   FIFOF#(Expected) queue <- mkUGSizedFIFOF(room);
   PulseWire aborting <- mkPulseWire;
   Reg#(UInt#(3)) blind <- mkReg(0);   // after an abort the DUT's reset takes a few clocks to act

   (* fire_when_enabled, no_implicit_conditions *)
   rule check;
      Bit#(1) l = pin;
      previous <= l;
      now <= now + oneClock;
      Fx half = period / 2;

      if (aborting) begin
         queue.clear;
         inTrain <= False;
         blind <= 4;
      end
      else if (blind != 0)
         blind <= blind - 1;
      else if (now < clocksFx(4)) begin
         // reset: a register that is not reset yet shows its power-up value
      end
      else if (!inTrain) begin
         if (l == 0) begin
            if (!queue.notEmpty)
               $fatal(1, "FAIL: %s: unexpected start bit at clock %0d", name, now / oneClock);
            else begin
               let e = queue.first;
               queue.deq;
               bits <= e.bits;
               length <= e.n;
               index <= 0;
               start <= now;
               nextSample <= now + half;
               inTrain <= True;
            end
         end
      end
      else begin
         Fx a = now - start;
         if (l != previous) begin
            Fx grid = ((a + half) / period) * period;
            Fx dev = (a > grid) ? a - grid : grid - a;
            if (dev > oneClock / 2 + 700)
               $fatal(1, "FAIL: %s: edge at clock %0d is %0d/65536 clock off the bit grid",
                      name, now / oneClock, dev);
            if (dev > worst) worst <= dev;
         end
         if (now + oneClock > nextSample) begin   // the center of the next bit is in this clock
            Bit#(16) b = bits;
            UInt#(5) n = length;
            UInt#(5) i = index;
            Bool ended = False;
            if (i == n) begin   // past the frame: the next bit is for a following frame, if any
               done <= done + 1;
               if (queue.notEmpty && queue.first.continuous) begin
                  b = queue.first.bits;
                  n = queue.first.n;
                  i = 0;
                  queue.deq;
               end
               else begin
                  ended = True;
                  inTrain <= False;
                  if (l == 0)
                     $fatal(1, "FAIL: %s: an unexpected frame follows the last, at clock %0d",
                            name, now / oneClock);
               end
            end
            if (!ended) begin
               if (l != b[i])
                  $fatal(1, "FAIL: %s: bit %0d of frame %0d is %b, expected %b (clock %0d)",
                         name, i, done, l, b[i], now / oneClock);
               bits <= b;
               length <= n;
               index <= i + 1;
               nextSample <= nextSample + period;
            end
         end
      end
   endrule

   method Action line(Bit#(1) v) = pin._write(v);
   method Action setPeriod(Fx p) = period._write(p);
   method Action expectFrame(Bit#(16) b, UInt#(5) n, Bool continuous) if (queue.notFull);
      queue.enq(Expected { bits: b, n: n, continuous: continuous });
   endmethod
   method Action abort = aborting.send;
   method UInt#(32) frames = done;
   method Fx maxDeviation = worst;
   method Bool idle = !inTrain && !queue.notEmpty;
endmodule

// ---------------------------------------------------------------------------------------
// A bench: one DUT with the remote on its rxd (or, in loopback, its own txd; or low from reset
// until released, to test power-up), a checker on
// its txd and a consumer that compares every byte the DUT delivers with the expected ones.

typedef enum {
   Delivered,    // bytes taken from rx
   Mismatch,     // delivered bytes that were not the expected ones
   RxBytes, FrameErrors, ParityErrors, Breaks, Overruns, TxBytes   // the DUT's events
} Counter deriving (Bits, Eq, FShow, Bounded);

interface Bench#(numeric type nd);
   // rxd of the DUT: the remote (default) or its own txd
   method Action releaseLine;   // end the forced low level that rxd has from reset on
   method Action loopback(Bool on);
   method Action rxPeriod(Fx p);
   method Action phase(UInt#(16) f);
   method Action sendLine(Bit#(16) bits, UInt#(5) n);
   method Action glitch(Bit#(1) level, UInt#(8) clocks);
   method Action glitchMidBit(UInt#(8) clocks);
   method Action glitchFrameBit(UInt#(4) k, UInt#(8) clocks);
   method UInt#(16) midBitGlitches;
   method Bool lineIdle;
   // txd of the DUT: send a byte and expect its frame on the line
   method Action txPeriod(Fx p);
   method Action sendByte(Bit#(nd) b, Bool continuous);
   method Bool txIdle;
   method Bit#(1) txLine;
   method Bool txBusy;
   method UInt#(32) txFrames;
   method Fx maxEdgeDeviation;
   // the bytes delivered by rx
   method Action expectByte(Bit#(nd) b);
   method Action flushExpected;
   method Action stall(Bool on);
   method Action tolerant(Bool on);   // count mismatches instead of failing
   method UInt#(16) count(Counter c);
   method Action resetCounters;
   method UartFlags flags;
   method Action clearFlags;
   // Call `clearFlags` in the very cycle of the next error event (framing, parity, break or
   // overrun), to test that the event still sets its flag.
   method Action clearAtEvent;
   method Bool collided;   // the event came, and `clearFlags` with it
   // The DUT was reset: forget the frames the checker expected from it.
   method Action forgetFrames;
endinterface

// `room` is the most bytes that can be in flight at once, see `inFlight`.
module mkBench#(String name, Uart#(nd) dut, Reset pinsReset, Integer resetSplit,
                Parity parity, Integer stops, Bool lowAtPowerUp, Integer room)(Bench#(nd));
   LineSender remote <- mkLineSender;
   LineChecker checker <- mkLineChecker(name + " txd", room);
   FIFOF#(Bit#(nd)) expected <- mkUGSizedFIFOF(room);
   Reg#(Bool) forcedLow <- mkReg(lowAtPowerUp);
   Reg#(Bool) looping <- mkReg(False);
   Reg#(Bool) stalled <- mkReg(False);
   Reg#(Bool) lenient <- mkReg(False);
   Vector#(8, Reg#(UInt#(16))) cs <- replicateM(mkReg(0));
   PulseWire resetting <- mkPulseWire;
   PulseWire clearing <- mkPulseWireOR;
   PulseWire arming <- mkPulseWire;
   Reg#(UInt#(2)) collision <- mkReg(0);          // 1: armed, 2: an event met a clear
   RWire#(Bool) delivery <- mkRWire;   // a byte was taken from rx: was it the expected one?
   Clock benchClock <- exposeCurrentClock;
   ReadOnly#(Bit#(1)) txd <- mkNullCrossingWire(benchClock, dut.pins.txd);
   ReadOnly#(Bit#(1)) remoteLine <- mkNullCrossingWire(benchClock, remote.line);
   ReadOnly#(Bool) loopingCross <- mkNullCrossingWire(benchClock, looping);
   ReadOnly#(Bool) forcedLowCross <- mkNullCrossingWire(benchClock, forcedLow);
   ReadOnly#(Bool) stalledCross <- mkNullCrossingWire(benchClock, stalled);
   RWire#(UartEvents) events <- mkRWire(reset_by noReset);
   ReadOnly#(UartFlags) flagsCross <- mkNullCrossingWire(benchClock, dut.flags);
   ReadOnly#(Bool) busy <- mkNullCrossingWire(benchClock, dut.txBusy);
   FIFOF#(Bit#(nd)) txData <- mkSizedFIFOF(1, reset_by noReset);
   Empty pinResetBridge <- mkUartPinResetBridge(resetSplit, dut.pins, remoteLine, loopingCross,
                                                forcedLowCross, reset_by pinsReset);
   RWire#(Bool) clearCross <- mkRWire(reset_by noReset);
   Empty clearResetBridge <- mkUartClearResetBridge(resetSplit, dut, clearCross,
                                                     reset_by pinsReset);
   RWire#(Bit#(nd)) rxData <- mkRWire(reset_by noReset);
   Empty rxResetBridge <- mkUartRxResetBridge(resetSplit, dut, rxData, stalledCross,
                                              reset_by pinsReset);
   Empty txResetBridge <- mkUartTxResetBridge(resetSplit, dut, txData, reset_by pinsReset);
   Empty eventsResetBridge <- mkUartEventsResetBridge(resetSplit, dut, events,
                                                      reset_by pinsReset);

   (* fire_when_enabled, no_implicit_conditions *)
   rule wires;
      if (resetSplit == 1) begin
         checker.line(txd._read);
      end else begin
         dut.pins.rxd(forcedLow ? 0 : (looping ? dut.pins.txd : remote.line));
         checker.line(dut.pins.txd);
      end
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule countEvents;
      UartEvents ev = resetSplit == 1 ? fromMaybe(unpack(0), events.wget) : dut.events;
      function UInt#(16) bump(Counter c, Bool hit) =
         resetting ? 0 : (hit ? cs[pack(c)] + 1 : cs[pack(c)]);
      cs[pack(RxBytes)] <= bump(RxBytes, ev.rxByte);
      cs[pack(FrameErrors)] <= bump(FrameErrors, ev.frameError);
      cs[pack(ParityErrors)] <= bump(ParityErrors, ev.parityError);
      cs[pack(Breaks)] <= bump(Breaks, ev.brk);
      cs[pack(Overruns)] <= bump(Overruns, ev.overrun);
      cs[pack(TxBytes)] <= bump(TxBytes, ev.txByte);
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule driveClear (clearing);
      if (resetSplit == 1) clearCross.wset(True);
      else dut.clearFlags;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule collide;
      UartEvents ev = resetSplit == 1 ? fromMaybe(unpack(0), events.wget) : dut.events;
      Bool hit = collision == 1 && (ev.frameError || ev.parityError || ev.brk || ev.overrun);
      if (hit) clearing.send;
      collision <= arming ? 1 : (hit ? 2 : collision);
   endrule

   (* fire_when_enabled *)
   rule consume (!stalled);
      if (resetSplit == 1) begin
         if (rxData.wget matches tagged Valid .d) begin
            Bool good = expected.notEmpty && expected.first == d;
            if (expected.notEmpty) expected.deq;
            if (!good && !lenient)
               $fatal(1, "FAIL: %s: received %h, expected %s", name, d,
                      expected.notEmpty ? "another byte" : "nothing");
            delivery.wset(good);
         end
      end else begin
         let d <- dut.rx.get;
         Bool good = expected.notEmpty && expected.first == d;
         if (expected.notEmpty) expected.deq;
         if (!good && !lenient)
            $fatal(1, "FAIL: %s: received %h, expected %s", name, d,
                   expected.notEmpty ? "another byte" : "nothing");
         delivery.wset(good);
      end
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule tally;
      Bool got = isValid(delivery.wget);
      Bool bad = delivery.wget == tagged Valid False;
      cs[pack(Delivered)] <= resetting ? 0 : (got ? cs[pack(Delivered)] + 1 : cs[pack(Delivered)]);
      cs[pack(Mismatch)] <= resetting ? 0 : (bad ? cs[pack(Mismatch)] + 1 : cs[pack(Mismatch)]);
   endrule

   method Action releaseLine = forcedLow._write(False);
   method Action loopback(Bool on) = looping._write(on);
   method Action rxPeriod(Fx p) = remote.setPeriod(p);
   method Action phase(UInt#(16) f) = remote.setPhase(f);
   method Action sendLine(Bit#(16) bits, UInt#(5) n) = remote.send(bits, n);
   method Action glitch(Bit#(1) level, UInt#(8) clocks) = remote.glitch(level, clocks);
   method Action glitchMidBit(UInt#(8) clocks) = remote.glitchMidBit(clocks);
   method Action glitchFrameBit(UInt#(4) k, UInt#(8) clocks) = remote.glitchFrameBit(k, clocks);
   method UInt#(16) midBitGlitches = remote.midBitGlitches;
   method Bool lineIdle = remote.idle;

   method Action txPeriod(Fx p) = checker.setPeriod(p);
   method Action sendByte(Bit#(nd) b, Bool continuous);
      if (resetSplit == 1) txData.enq(b);
      else dut.tx.put(b);
      checker.expectFrame(frameWord(b, parity, stops, False, False),
                     frameLen(valueOf(nd), parity, stops), continuous);
   endmethod
   method Bool txIdle = checker.idle && !(resetSplit == 1 ? busy._read : dut.txBusy);
   method Bit#(1) txLine = resetSplit == 1 ? txd._read : dut.pins.txd;
   method Bool txBusy = resetSplit == 1 ? busy._read : dut.txBusy;
   method UInt#(32) txFrames = checker.frames;
   method Fx maxEdgeDeviation = checker.maxDeviation;

   method Action expectByte(Bit#(nd) b);
      if (expected.notFull) expected.enq(b);
      else if (!lenient)
         $fatal(1, "FAIL: %s: more than %0d bytes expected and not delivered", name, room);
   endmethod
   method Action flushExpected = expected.clear;
   method Action stall(Bool on) = stalled._write(on);
   method Action tolerant(Bool on) = lenient._write(on);
   method UInt#(16) count(Counter c) = cs[pack(c)];
   method Action resetCounters = resetting.send;
   method UartFlags flags = resetSplit == 1 ? flagsCross._read : dut.flags;
   method Action clearFlags = clearing.send;
   method Action clearAtEvent = arming.send;
   method Bool collided = collision == 2;
   method Action forgetFrames = checker.abort;
endmodule

endpackage
