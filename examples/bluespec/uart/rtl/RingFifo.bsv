// RingFifo: small FIFOs for byte buffers, sized by a numeric type.
//
// `mkRingFIFOF` is a power-of-two ring over a RegFile (LUTRAM on FPGAs). It costs about half the
// logic of the library's `mkSizedFIFOF`: 18 LUT for 16 entries against 42 (yosys, xc7). The price
// is an asynchronous read in the consumer's path instead of a registered output.
//
// `mkSmallFIFOF` picks the cheapest implementation for a depth: 1 is `mkLoopyFIFOF` (2 LUT),
// 2 the library's `mkFIFOF` (13 LUT) and a power of two from 4 up the ring.
//
// What a full FIFO does in the cycle it is dequeued. `enq` is ready when the FIFO is not full, and
// for the ring and for `mkFIFOF` that is a function of registers only: a FIFO that is full at the
// start of a cycle does not take an `enq` in that cycle, even when a `deq` happens in that same
// cycle; the freed slot is usable from the next cycle on. That costs no throughput (a FIFO below
// d entries takes one `enq` and one `deq` in every cycle, and one that is kept full settles at d-1
// entries and runs at the full rate), only one entry of capacity at full rate and a cycle of
// latency. `mkLoopyFIFOF` (depth 1) is the exception: it takes an `enq` while full when a `deq`
// comes in the same cycle (like the library's `mkLFIFOF`, a pipeline FIFO), at the price that its
// `RDY_enq` and `notFull` depend combinationally on `EN_deq`, which turns into a scheduling cycle
// or a long path for a caller whose consumer waits for the producer. The ring does not make that
// trade: `enq` and `deq` are conflict free, there is no path from `EN_deq` to `RDY_enq`, and it
// is safe in any feedback loop.
//
// Where it shows: a UART receive buffer. A byte that completes in the very cycle in which a full
// buffer is read is dropped and counted as an overrun when the buffer is a ring or `mkFIFOF`
// (RX_DEPTH 2 or more), and kept when RX_DEPTH is 1. A consumer that keeps the buffer full has
// lost bytes anyway; the difference is one byte in the cycle of a coincidence.
package RingFifo;

import FIFOF :: *;
import RegFile :: *;

export Depth(..);
export depth;
export mkRingFIFOF;
export mkLoopyFIFOF;
export mkSmallFIFOF;

// A value that carries a depth as a type, since a numeric type that appears in no interface
// cannot be inferred. Write `Depth#(16) d = depth;`.
typedef struct { } Depth#(numeric type n) deriving (Bits, Eq, FShow);

Depth#(n) depth = Depth { };

// A FIFO of d entries; d must be a power of two, 2 or more. The methods only record what they
// do in wires and one rule updates the pointers (a `clear` wins over everything), so `enq` and
// `deq` never conflict and a FIFO that is neither full nor empty takes both in one cycle.
module mkRingFIFOF#(Depth#(d) _depth)(FIFOF#(t))
   provisos (Bits#(t, tw),
             Log#(TMax#(d, 2), n),  // n index bits (TMax only keeps depth 1 well typed)
             Add#(n, 1, n1));       // plus a wrap bit per pointer
   if (valueOf(d) < 2 || 2 ** valueOf(n) != valueOf(d))
      error("mkRingFIFOF: the depth must be a power of two of at least 2, not "
            + integerToString(valueOf(d)));

   RegFile#(UInt#(n), t) mem <- mkRegFileFull;
   Reg#(UInt#(n1)) wp <- mkReg(0);
   Reg#(UInt#(n1)) rp <- mkReg(0);
   PulseWire enqW <- mkPulseWire;
   PulseWire deqW <- mkPulseWire;
   PulseWire clearW <- mkPulseWire;

   Bool empty = wp == rp;
   Bool full = (wp ^ rp) == fromInteger(valueOf(d)); // same index, wrap bits differ
   function UInt#(n) idx(UInt#(n1) p) = truncate(p);

   (* fire_when_enabled, no_implicit_conditions *)
   rule update;
      if (clearW) begin
         wp <= 0;
         rp <= 0;
      end
      else begin
         if (enqW) wp <= wp + 1;
         if (deqW) rp <= rp + 1;
      end
   endrule

   method Action enq(t x) if (!full);
      mem.upd(idx(wp), x);
      enqW.send;
   endmethod

   method Action deq() if (!empty);
      deqW.send;
   endmethod

   method t first() if (!empty) = mem.sub(idx(rp));
   method Bool notFull = !full;
   method Bool notEmpty = !empty;
   method Action clear = clearW.send;
endmodule

// One entry that accepts a new element in the cycle it is dequeued ("loopy", like the library's
// `mkLFIFOF`, but a few lines of BSV, so there is no Verilog library module, and 1 LUT less).
// `enq` and `notFull` therefore depend on the `deq` of the same cycle (see the top of the file).
module mkLoopyFIFOF(FIFOF#(t))
   provisos (Bits#(t, tw));
   Reg#(t) item <- mkRegU;
   Reg#(Bool) occupied <- mkReg(False);
   RWire#(t) enqW <- mkRWire;
   PulseWire deqW <- mkPulseWire;
   PulseWire clearW <- mkPulseWire;

   (* fire_when_enabled, no_implicit_conditions *)
   rule update;
      if (enqW.wget matches tagged Valid .x) item <= x;
      occupied <= !clearW && (isValid(enqW.wget) || (occupied && !deqW));
   endrule

   method Action enq(t x) if (!occupied || deqW);
      enqW.wset(x);
   endmethod

   method Action deq() if (occupied);
      deqW.send;
   endmethod

   method t first() if (occupied) = item;
   method Bool notFull = !occupied || deqW;
   method Bool notEmpty = occupied;
   method Action clear = clearW.send;
endmodule

// The cheapest FIFO of depth d: 1, 2, or a power of two from 4.
module mkSmallFIFOF#(Depth#(d) dpt)(FIFOF#(t))
   provisos (Bits#(t, tw), Log#(TMax#(d, 2), n), Add#(n, 1, n1));
   FIFOF#(t) f;
   if (valueOf(d) == 1)
      f <- mkLoopyFIFOF;
   else if (valueOf(d) == 2)
      f <- mkFIFOF;
   else if (valueOf(d) < 1 || 2 ** valueOf(n) != valueOf(d))
      f = error("mkSmallFIFOF: the depth must be 1, 2 or a power of two, not "
                + integerToString(valueOf(d)));
   else
      f <- mkRingFIFOF(dpt);
   return f;
endmodule

endpackage
