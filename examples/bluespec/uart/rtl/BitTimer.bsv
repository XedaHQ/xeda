// BitTimer: a restartable fractional bit timer, one per direction of the UART.
//
// A bit lasts D or D+1 clocks, so the bit period averages P = D + R/M clocks exactly (M is the
// modulus of the fraction). A down counter `cnt` counts D-1 .. 0; a fraction accumulator `acc`
// adds R at every bit boundary modulo M, and its carry makes `pause` insert one extra clock.
// The timer runs free and `tick` is high in the last clock of every bit. The receiver restarts
// it at the start edge, the transmitter at the start of a frame from idle, and in between the
// transmitter never restarts it, which keeps the average baud exact across back-to-back frames.
//
// The period reaches the timer as a `BitTiming`, which has one of two origins:
//
//  * `staticTiming(clockHz, baud, adjust)`: constants. P = clockHz/baud is reduced to A/B; if
//    B fits the accumulator (B <= 2**aw) the timer is exact with modulus B, otherwise P is
//    rounded to aw fraction bits. A modulus other than a power of two costs a compare and a
//    conditional subtract of that constant, and the counter needs only as many bits as D-1.
//  * `runtimeTiming(period, adjust, shortest)`: a BitPeriod read at run time, with modulus 2**aw.
//    The same timer, so a period that happens to be constant folds to the static cost.
//
// `adjust` shifts the first sample of the receiver by clocks: 0 for periods of 8 clocks and more,
// -1 below, where the detection delay is a larger part of the bit and sampling one clock earlier
// balances the window (a model of the receiver over all edge phases; `defaultAdjust`).
package BitTimer;

export BitPeriod(..);
export BitTiming;
export BitTimer(..);
export staticTiming;
export runtimeTiming;
export bitPeriod;
export shortestBit;
export shortestOf;
export defaultAdjust;
export staticAccBits;
export StaticCntBits;
export mkBitTimer;

// A bit period D + frac / 2**f clocks, stored as D-1 and frac.
typedef struct {
   Bit#(wc) dm1;
   Bit#(f) frac;
} BitPeriod#(numeric type wc, numeric type f) deriving (Bits, Eq, FShow);

// The period as the timer consumes it. `modulus` is an elaboration-time number, the other
// fields are values: constants or wires. `firstCnt`/`firstAcc` start the counter and the
// accumulator when the receiver restarts at the start edge, so that its first tick falls at
// the centre of the start bit: half of D-1+R/M, rounded down, plus the adjustment.
// `txAcc`/`txPause` are the state the transmitter restarts in: the accumulator at R + M/2, the
// fraction R of the start bit itself (a bit has its extra clock when the accumulator carries
// *before* it, which the start bit would miss) plus one half, which keeps every bit edge of
// a frame within half a clock of the ideal grid. R + M/2 can reach M, hence `txPause`.
// `shortest` is D, the clocks of the shortest bit: the receiver's glitch filter is checked
// against it (`mkUartCore`).
typedef struct {
   Integer modulus;
   Integer shortest;
   Bit#(wc) reload;     // D-1
   Bit#(aw) step;       // R
   Bit#(wc) firstCnt;
   Bit#(aw) firstAcc;
   Bit#(aw) txAcc;
   Bool txPause;
} BitTiming#(numeric type wc, numeric type aw);

interface BitTimer;
   // The bit ends in this clock.
   method Bool tick;
   // Receiver: restart so that the first tick comes about half a bit later (start bit centre).
   method Action restartHalf;
   // Transmitter: restart so that the first tick comes one bit later, with the accumulator
   // centred so the bit edges stay within half a clock of the ideal grid.
   method Action restartFull;
endinterface

function Integer gcd(Integer a, Integer b) = (b == 0) ? a : gcd(b, a % b);

function Integer defaultAdjust(Integer clockHz, Integer baud) = (clockHz < 8 * baud) ? -1 : 0;

// The accumulator bits that make `staticTiming` exact for clockHz/baud: those of the reduced
// denominator, at most maxBits (beyond which the period is rounded).
function Integer staticAccBits(Integer clockHz, Integer baud, Integer maxBits);
   Integer den = baud / gcd(clockHz, baud);
   Integer bits = 0;
   for (Integer m = 1; m < den && bits < maxBits; m = 2 * m)
      bits = bits + 1;
   return bits;
endfunction

// The type-level counterpart of `staticTiming`: the bits that D-1 needs for the period
// clockHz/baud with aw fraction bits, i.e. TLog of floor(P + 2**-(aw+1)), the largest D that
// rounding can give. Size the counter with it from numeric macros:
// `typedef StaticCntBits#(`CLK_HZ, `BAUD, 6) CntBits;`
typedef TLog#(TDiv#(TSub#(TAdd#(TAdd#(TMul#(TMul#(2, clockHz), TExp#(aw)), baud), 1),
                          TMul#(TMul#(2, baud), TExp#(aw))),
                    TMul#(TMul#(2, baud), TExp#(aw))))
        StaticCntBits#(numeric type clockHz, numeric type baud, numeric type aw);
// (With a = 2*clockHz*2**aw + baud and b = 2*baud*2**aw, floor(a/b) is TDiv#(a+1-b, b), since
// TDiv rounds up; a+1-b is the numerator above. Without the 1 the tie where a/b is an integer,
// like 71 Hz / 8 baud with aw = 2 (8.875 rounds up to 9), would be one too small.)

function BitTiming#(wc, aw) staticTiming(Integer clockHz, Integer baud, Integer adjust);
   Integer m = 2 ** valueOf(aw);
   Integer g = gcd(clockHz, baud);
   Integer den = baud / g;
   // P * mod as an integer: exact when the reduced denominator fits, else rounded
   Integer mod = (den <= m) ? den : m;
   Integer scaled = (den <= m) ? clockHz / g : scaledPeriod(clockHz, baud, valueOf(aw));
   Integer d = scaled / mod;
   Integer r = scaled % mod;
   Integer dm1 = d - 1;
   Integer half = (dm1 * mod + r) / 2;
   Integer first = half / mod + adjust;
   if (d < 4)
      return error("staticTiming: " + integerToString(clockHz) + " Hz / "
                   + integerToString(baud) + " baud is under 4 clocks per bit");
   else if (dm1 >= 2 ** valueOf(wc))
      return error("staticTiming: the bit period of " + integerToString(clockHz) + " Hz / "
                   + integerToString(baud) + " baud needs a counter wider than "
                   + integerToString(valueOf(wc)) + " bits");
   else
      return BitTiming {
         modulus: mod,
         shortest: d,
         reload: fromInteger(dm1),
         step: fromInteger(r),
         firstCnt: fromInteger(first),
         firstAcc: fromInteger(half % mod),
         txAcc: fromInteger((r + mod / 2) % mod),
         txPause: r + mod / 2 >= mod
      };
endfunction

// A runtime period, for example to switch the baud rate. Modulus 2**f. `shortest` is the clocks
// of the shortest bit the period will ever hold (see `shortestBit`): the timing cannot know it,
// so the caller promises it, and `mkUartCore` checks the glitch filter against it.
function BitTiming#(wc, f) runtimeTiming(BitPeriod#(wc, f) p, Integer adjust, Integer shortest);
   Bit#(TAdd#(wc, f)) half = {p.dm1, p.frac} >> 1;
   Bit#(wc) halfCnt = truncateLSB(half);
   Bit#(f) halfAcc = truncate(half);
   Bit#(TAdd#(f, 1)) txStart = zeroExtend(p.frac) + fromInteger(2 ** valueOf(f) / 2);
   return BitTiming {
      modulus: 2 ** valueOf(f),
      shortest: shortest,
      reload: p.dm1,
      step: p.frac,
      firstCnt: halfCnt + fromInteger(adjust),
      firstAcc: halfAcc,
      txAcc: truncate(txStart),
      txPause: msb(txStart) == 1
   };
endfunction

// D of a timing: the clocks in its shortest bit.
function Integer shortestOf(BitTiming#(wc, aw) t) = t.shortest;

// The period clockHz/baud in units of 2**-f clocks, rounded.
function Integer scaledPeriod(Integer clockHz, Integer baud, Integer f) =
   (2 * clockHz * 2 ** f + baud) / (2 * baud);

// D, the clocks in the shortest bit of clockHz/baud rounded to f fraction bits.
function Integer shortestBit(Integer clockHz, Integer baud, Integer f) =
   scaledPeriod(clockHz, baud, f) / 2 ** f;

// The period clockHz/baud rounded to f fraction bits, as a constant BitPeriod to put in a table
// or a register.
function BitPeriod#(wc, f) bitPeriod(Integer clockHz, Integer baud);
   Integer scaled = scaledPeriod(clockHz, baud, valueOf(f));
   Integer d = scaled / 2 ** valueOf(f);
   if (d < 4 || d > 2 ** valueOf(wc))
      return error("bitPeriod: " + integerToString(clockHz) + " Hz / " + integerToString(baud)
                   + " baud needs " + integerToString(d) + " clocks per bit, outside 4 to "
                   + integerToString(2 ** valueOf(wc)));
   else
      return BitPeriod { dm1: fromInteger(d - 1), frac: fromInteger(scaled % 2 ** valueOf(f)) };
endfunction

module mkBitTimer#(BitTiming#(wc, aw) t)(BitTimer);
   Reg#(Bit#(wc)) cnt <- mkRegU;
   Reg#(Bit#(aw)) acc <- mkRegU;
   Reg#(Bool) pause <- mkRegU;
   PulseWire half <- mkPulseWire;
   PulseWire full <- mkPulseWire;

   Bool isTick = !pause && cnt == 0;

   (* fire_when_enabled, no_implicit_conditions *)
   rule step;
      // acc + step modulo the modulus; a power of two needs only the adder's carry out
      Bit#(TAdd#(aw, 1)) sum = zeroExtend(acc) + zeroExtend(t.step);
      Bool carry = (t.modulus == 2 ** valueOf(aw)) ? msb(sum) == 1 : sum >= fromInteger(t.modulus);
      Bit#(aw) wrapped = truncate(carry && t.modulus != 2 ** valueOf(aw)
                                  ? sum - fromInteger(t.modulus) : sum);

      if (half) begin
         cnt <= t.firstCnt;
         acc <= t.firstAcc;
         pause <= False;
      end
      else if (full) begin
         cnt <= t.reload;
         acc <= t.txAcc;
         pause <= t.txPause;
      end
      else if (pause)
         pause <= False;
      else if (cnt != 0)
         cnt <= cnt - 1;
      else begin
         cnt <= t.reload;
         acc <= wrapped;
         pause <= carry;
      end
   endrule

   method Bool tick = isTick;
   method Action restartHalf = half.send;
   method Action restartFull = full.send;
endmodule

endpackage
