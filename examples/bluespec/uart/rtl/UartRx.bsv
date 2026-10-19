// UartRx: the receiver.
//
//   rxd -> 2-FF synchronizer -> hold filter -> start hunt -> one sample per bit -> frame event
//
// * The synchronizer (`rxSync1`, `rxSync2`) and the filter reset to 0, a line that has not been
//   seen high yet: together with the state `WaitHigh` after reset that makes a line that is low
//   at power-up (or a pin that floats) start no frame, whatever the length of the reset.
//   Constrain the two synchronizer registers as ASYNC_REG (see the README).
// * The hold filter changes the line it passes on only after `hold` equal consecutive samples,
//   so a glitch shorter than `hold` clocks is invisible to the start detect and to every sample.
//   hold = 1 is no filter, and the filter's registers are gone. The depth is limited by the bit
//   period, see `maxHold`; `mkUartCore` checks it. The filter delays every edge alike, so it
//   does not move the sampling point: the baud tolerance is the same for every hold.
// * The hunt is level sensitive: any low sample starts a frame. The first sample, at the center of
//   the start bit, aborts silently if the line is high again (a false start). Then there is one
//   sample per bit; the bits shift into a register whose leading marker bit arrives at bit 0 when
//   the frame is complete, so no bit counter is needed.
// * Only the first stop bit is sampled, at its center, and the receiver hunts again one clock
//   later. Every clock later costs about 1.2 % of baud tolerance at 8.33 clocks per bit, and
//   hunting only after the stop bit has ended fails at 100 % line utilization even for a perfect
//   remote.
// * After a frame with the stop bit low, the receiver waits until the line is high before it hunts
//   again (and does so after reset), so a break gives one frame and a line stuck low gives none.
package UartRx;

import List :: *;
import UartTypes :: *;
import BitTimer :: *;

export UartRx(..);
export maxHold;
export mkUartRx;

interface UartRx#(numeric type nData);
   (* always_ready, always_enabled *)
   method Action rxd(Bit#(1) pin);
   // Valid for one cycle, the cycle after the stop bit was sampled.
   (* always_ready *)
   method Maybe#(RxFrame#(nData)) frame;
endinterface

typedef enum {
   WaitHigh,   // after reset or a frame error: wait for the line to be high
   Hunt,       // idle: any low sample is a start bit
   Start,      // until the center of the start bit
   Data        // data, parity, then the stop bit
} RxState deriving (Bits, Eq, FShow);

// The deepest hold filter that survives a glitch of hold-1 clocks at the center of any bit of at
// least `bitClocks` clocks (D, the shortest bit). The glitch leaves ceil((D - hold + 1) / 2) clocks
// of the bit's level before it, and the filter has switched to that level before the glitch only
// if it saw `hold` of them: D >= 3 * hold - 2. With the testbench's glitch matrix, this is the
// deepest filter that passes for every D from 4 to 20, and one more fails.
function Integer maxHold(Integer bitClocks) = (bitClocks + 2) / 3;

// `hold` must not exceed `maxHold` of the shortest bit of the timer's period, or glitches that the
// filter should hide corrupt bits. `mkUartCore` checks it; this module cannot see the period.
module mkUartRx#(BitTimer timer, UartFormat#(nd, np, ns) fmt, Integer hold)(UartRx#(nd))
   provisos (Add#(nd, np, nb),   // data and parity bits
             Add#(nb, 1, w));    // plus the marker
   if (hold < 1)
      error("mkUartRx: the hold filter depth is 1 (off) or more");

   Wire#(Bit#(1)) pin <- mkBypassWire;
   Reg#(Bit#(1)) rxSync1 <- mkReg(0);
   Reg#(Bit#(1)) rxSync2 <- mkReg(0);
   // The older samples of the synchronized line and the filter's output.
   List#(Reg#(Bit#(1))) older <- List::replicateM(hold - 1, mkReg(0));
   Reg#(Bit#(1)) filtered <- mkReg(0);

   Reg#(RxState) state <- mkReg(WaitHigh);
   Reg#(Bit#(w)) sr <- mkRegU;       // marker and bits, shifted in from the top
   Reg#(Bool) valid <- mkReg(False); // a frame ended in the last cycle
   Reg#(Bit#(1)) stopBit <- mkRegU;  // the stop bit of that frame

   Bit#(1) line = (hold == 1) ? rxSync2 : filtered;

   (* fire_when_enabled, no_implicit_conditions *)
   rule step;
      rxSync1 <= pin;
      rxSync2 <= rxSync1;
      if (hold > 1) begin
         Bool allHigh = rxSync2 == 1;
         Bool allLow = rxSync2 == 0;
         for (Integer i = 0; i < hold - 1; i = i + 1) begin
            allHigh = allHigh && older[i] == 1;
            allLow = allLow && older[i] == 0;
            older[i] <= (i == 0) ? rxSync2 : older[i - 1];
         end
         filtered <= allHigh ? 1 : (allLow ? 0 : filtered);
      end

      RxState nextState = state;
      Bit#(w) nextSr = sr;
      Bool stopSampled = False;
      case (state)
         WaitHigh:
            if (line == 1) nextState = Hunt;
         Hunt:
            if (line == 0) begin
               timer.restartHalf;
               nextState = Start;
            end
         Start:
            if (timer.tick) begin
               if (line == 1) nextState = Hunt;   // false start
               else begin
                  nextSr = {1'b1, 0};
                  nextState = Data;
               end
            end
         Data:
            if (timer.tick) begin
               if (sr[0] == 0)
                  nextSr = {line, sr[valueOf(w) - 1:1]};
               else begin
                  stopSampled = True;
`ifdef XEDA_INJECT_BUG
                  // deliberate bug for negative tests: the receiver is late to hunt again, so at
                  // full line utilization it misses the next start bit
                  nextState = Start;
`else
                  nextState = (line == 1) ? Hunt : WaitHigh;
`endif
               end
            end
      endcase
      state <= nextState;
      sr <= nextSr;
      valid <= stopSampled;
      stopBit <= line;   // only read in the cycle after stopSampled
   endrule

   method Action rxd(Bit#(1) x) = pin._write(x);

   method Maybe#(RxFrame#(nd)) frame;
      Bit#(nb) bits = sr[valueOf(w) - 1:1];
      Bit#(1) parityBit = (parityKind(fmt) == OddParity) ? 1 : 0;
      Bool parityBad = valueOf(np) == 1 && reduceXor(bits) != parityBit;
      RxKind kind = (stopBit == 0) ? ((bits == 0) ? Break : FrameError)
                                   : (parityBad ? ParityError : Data);
      return valid ? tagged Valid RxFrame { kind: kind, data: truncate(bits) } : tagged Invalid;
   endmethod
endmodule

endpackage
