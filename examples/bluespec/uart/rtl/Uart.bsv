// Uart: the UART PHY, transmitter and receiver with byte buffers, status and pins.
//
//   tx (Put) -> transmit buffer -> UartTx -> txd
//   rxd -> UartRx -> receive buffer -> rx (Get)
//
// The interface is the user's: `tx` takes bytes to send and `rx` gives the bytes received. The
// receiver never stalls. A good byte that arrives while the receive buffer is full is dropped
// (the buffered bytes stay, in order) and counted as an overrun; frames with a framing or parity
// error or a break are dropped and reported, never delivered. `flags` are sticky, `events` are
// one-cycle pulses for counters and LEDs.
//
// `mkUartCore` is polymorphic in the frame format, buffer depths and bit period, so a design that
// needs a runtime baud rate instantiates it with `runtimeTiming`, and one that needs another format
// with another `UartFormat`. The receiver's glitch filter depth `hold` is limited by the shortest
// bit, `hold <= maxHold(D)` (see UartRx): checked at elaboration for `staticTiming`; with
// `runtimeTiming` it is checked against the `shortest` the caller promises, so the period must
// never be set to a shorter bit. `mkUart` is a synthesizable 8N1 (by default) UART whose clock
// rate, baud rate and sizes come from preprocessor macros, which Xeda sets with `rtl.defines`:
//
//   CLK_HZ     clock frequency in Hz (default 100000000)
//   BAUD       baud rate (default 12000000)
//   DATA_BITS  data bits, 5 to 9 (default 8)
//   PARITY     0 none, 1 even, 2 odd (default 0)
//   STOP_BITS  stop bits sent, 1 or 2 (default 1; the receiver samples the first only)
//   TX_DEPTH   transmit buffer depth: 1, 2 or a power of two from 4 (default 1)
//   RX_DEPTH   receive buffer depth, same choices (default 4)
//   HOLD       receiver glitch filter depth in clocks, 1 is none (default 3); at most
//              (D + 2) / 3 for D clocks in the shortest bit, 3 at 12 Mbaud / 100 MHz
//   ACC_BITS   most bits of the baud fraction accumulator, at most 8 (default 6); the exact
//              period takes as many as its reduced denominator needs, see `mkUart`
package Uart;

import FIFOF :: *;
import GetPut :: *;
import UartTypes :: *;
import BitTimer :: *;
import UartRx :: *;
import UartTx :: *;
import RingFifo :: *;

export UartTypes :: *;
export BitTimer :: *;
export RingFifo :: *;
export Uart(..);
export mkUartCore;
export mkUart;

interface Uart#(numeric type nData);
   interface Put#(Bit#(nData)) tx;
   interface Get#(Bit#(nData)) rx;
   (* always_ready *)
   method UartFlags flags;
   (* always_ready *)
   method Action clearFlags;
   (* always_ready *)
   method UartEvents events;
   // A frame is on the line, or about to be.
   (* always_ready *)
   method Bool txBusy;
   (* prefix = "" *)
   interface UartPins pins;
endinterface

module mkUartCore#(BitTiming#(wc, aw) timing,
                   UartFormat#(nd, np, ns) fmt,
                   Integer hold,
                   Depth#(txDepth) txDepth,
                   Depth#(rxDepth) rxDepth)(Uart#(nd))
   provisos (Add#(nd, np, nb), Add#(nb, 1, rw),           // receiver
             Add#(1, nd, ta), Add#(ta, np, tb), Add#(tb, ns, tw), Add#(np, _p, 1),   // transmitter
             Log#(TMax#(txDepth, 2), txn), Add#(txn, 1, _t),     // buffers
             Log#(TMax#(rxDepth, 2), rxn), Add#(rxn, 1, _r));
   Integer limit = maxHold(shortestOf(timing));
   if (hold > limit)
      error("mkUartCore: a hold filter of " + integerToString(hold) + " clocks needs bits of at "
            + "least " + integerToString(3 * hold - 2) + " clocks, the shortest bit has "
            + integerToString(shortestOf(timing)) + ": use a hold of " + integerToString(limit)
            + " or less");
   BitTimer rxTimer <- mkBitTimer(timing);
   BitTimer txTimer <- mkBitTimer(timing);
   UartRx#(nd) receiver <- mkUartRx(rxTimer, fmt, hold);
   UartTx#(nd) transmitter <- mkUartTx(txTimer, fmt);
   FIFOF#(Bit#(nd)) txq <- mkSmallFIFOF(txDepth);
   FIFOF#(Bit#(nd)) rxq <- mkSmallFIFOF(rxDepth);

   Reg#(UartFlags) sticky <- mkReg(unpack(0));
   PulseWire clearing <- mkPulseWire;
   PulseWire sending <- mkPulseWire;

   // The frame that just ended, by kind.
   function Bool frameIs(RxKind kind) =
      isValid(receiver.frame) && fromMaybe(?, receiver.frame).kind == kind;
   Bool good = frameIs(Data);

   UartEvents pulses = UartEvents {
      rxByte: good && rxq.notFull,
      frameError: frameIs(FrameError),
      parityError: frameIs(ParityError),
      brk: frameIs(Break),
      overrun: good && !rxq.notFull,
      txByte: sending
   };

   (* fire_when_enabled *)
   rule deliver (good);
      rxq.enq(fromMaybe(?, receiver.frame).data);
   endrule

   (* fire_when_enabled *)
   rule feed;
      transmitter.put(txq.first);
      txq.deq;
      sending.send;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule keepFlags;
      UartFlags old = clearing ? unpack(0) : sticky;
      sticky <= UartFlags {
         frameError: old.frameError || pulses.frameError,
         parityError: old.parityError || pulses.parityError,
         brk: old.brk || pulses.brk,
         overrun: old.overrun || pulses.overrun
      };
   endrule

   interface tx = toPut(txq);
   interface rx = toGet(rxq);
   method UartFlags flags = sticky;
   method Action clearFlags = clearing.send;
   method UartEvents events = pulses;
   method Bool txBusy = transmitter.busy || txq.notEmpty;

   interface UartPins pins;
      method Action rxd(Bit#(1) pin) = receiver.rxd(pin);
      method Bit#(1) txd = transmitter.txd;
   endinterface
endmodule

// The synthesizable UART, sized by macros.

`ifndef CLK_HZ
`define CLK_HZ 100000000
`endif
`ifndef BAUD
`define BAUD 12000000
`endif
`ifndef DATA_BITS
`define DATA_BITS 8
`endif
`ifndef PARITY
`define PARITY 0
`endif
`ifndef STOP_BITS
`define STOP_BITS 1
`endif
`ifndef TX_DEPTH
`define TX_DEPTH 1
`endif
`ifndef RX_DEPTH
`define RX_DEPTH 4
`endif
`ifndef HOLD
`define HOLD 3
`endif
`ifndef ACC_BITS
`define ACC_BITS 6
`endif

typedef TMin#(`PARITY, 1) ParityBits;
typedef `STOP_BITS StopBits;

// The counter width that the period of CLK_HZ and BAUD needs, with a accumulator bits
typedef StaticCntBits#(`CLK_HZ, `BAUD, a) CntBits#(numeric type a);

(* synthesize *)
module mkUart(Uart#(`DATA_BITS));
   if (`PARITY > 2)
      error("mkUart: PARITY is 0 (none), 1 (even) or 2 (odd), not " + integerToString(`PARITY));
   UartFormat#(`DATA_BITS, ParityBits, StopBits) fmt =
      uartFormat(`PARITY == 0 ? NoParity : (`PARITY == 1 ? EvenParity : OddParity));
   Depth#(`TX_DEPTH) txDepth = depth;
   Depth#(`RX_DEPTH) rxDepth = depth;
   Integer adjust = defaultAdjust(`CLK_HZ, `BAUD);
   function module#(Uart#(`DATA_BITS)) core(BitTiming#(wc, aw) t) =
      mkUartCore(t, fmt, `HOLD, txDepth, rxDepth);

   // Counter and accumulator get exactly the bits the period needs, which is a choice of types
   // and so a choice of instance; what it costs is in the README.
   Uart#(`DATA_BITS) uart;
   case (staticAccBits(`CLK_HZ, `BAUD, `ACC_BITS))
      0: uart <- core(BitTiming#(CntBits#(0), 0)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      1: uart <- core(BitTiming#(CntBits#(1), 1)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      2: uart <- core(BitTiming#(CntBits#(2), 2)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      3: uart <- core(BitTiming#(CntBits#(3), 3)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      4: uart <- core(BitTiming#(CntBits#(4), 4)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      5: uart <- core(BitTiming#(CntBits#(5), 5)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      6: uart <- core(BitTiming#(CntBits#(6), 6)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      7: uart <- core(BitTiming#(CntBits#(7), 7)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      8: uart <- core(BitTiming#(CntBits#(8), 8)'(staticTiming(`CLK_HZ, `BAUD, adjust)));
      default: uart = error("mkUart: ACC_BITS is at most 8");
   endcase
   return uart;
endmodule

endpackage
