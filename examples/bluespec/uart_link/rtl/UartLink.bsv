// UartLink joins the existing UART byte stream to the generic reliable link.
package UartLink;

import Uart :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;
import LinkDiagnostics :: *;

export UartLink(..);
export UartLinkAdapter(..);
export mkUartLinkAdapter;
export mkUartLink;

typedef enum { RxForward, RxDrainOld, RxFindDelimiter, RxRearm }
   LinkRxState deriving (Bits, Eq, FShow);

interface UartLinkAdapter;
   interface UartPins pins;
   method UartFlags flags;
   method Bit#(32) stat(Bit#(4) index);
endinterface

// The PHY's events and rx.get methods must be conflict-free, with no dequeue-to-events
// path and no required order between them.
module mkUartLinkAdapter#(Uart#(8) phy, ReliableLinkIfc link)(UartLinkAdapter);
   Reg#(UInt#(16)) phyOutstanding <- mkReg(0);
   Reg#(UInt#(16)) oldRemaining <- mkReg(0);
   Reg#(LinkRxState) rxState <- mkReg(RxForward);
   PulseWire phyDequeued <- mkPulseWire;

   UartEvents events = phy.events;
   Bool phyFault = events.frameError || events.parityError || events.brk || events.overrun;
   Bit#(2) faultKind = events.frameError ? 0 :
                       events.parityError ? 1 :
                       events.brk ? 2 : 3;

   (* descending_urgency = "faultWithBacklog, faultWithoutBacklog, forwardByte, drainOldByte, scanByte" *)
   rule faultWithBacklog (phyFault && phyOutstanding != 0);
      let discarded <- phy.rx.get;
      phyDequeued.send;
      link.recordPhyFault(faultKind);
      link.abortRx;
      oldRemaining <= phyOutstanding - 1;
      rxState <= (phyOutstanding == 1) ? RxFindDelimiter : RxDrainOld;
   endrule

   rule faultWithoutBacklog (phyFault && phyOutstanding == 0);
      link.recordPhyFault(faultKind);
      link.abortRx;
      oldRemaining <= 0;
      rxState <= RxFindDelimiter;
   endrule

   // A simultaneous successful UART enqueue and dequeue leaves one byte outstanding.
   rule forwardByte (rxState == RxForward && !phyFault);
      let value <- phy.rx.get;
      link.rx.put(value);
      phyDequeued.send;
   endrule

   // Bytes already in the UART FIFO when the fault arrived are old, including any delimiters.
   rule drainOldByte (rxState == RxDrainOld && oldRemaining != 0 && !phyFault);
      let discarded <- phy.rx.get;
      phyDequeued.send;
      oldRemaining <= oldRemaining - 1;
      if (oldRemaining == 1)
         rxState <= RxFindDelimiter;
   endrule

   // Continue draining without link readiness. Only the final rearm delimiter waits for the link.
   rule scanByte (rxState == RxFindDelimiter && !phyFault);
      let value <- phy.rx.get;
      phyDequeued.send;
      if (value == 0)
         rxState <= RxRearm;
   endrule

   rule sendRearmDelimiter (rxState == RxRearm && !phyFault);
      link.rx.put(0);
      rxState <= RxForward;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule updateOutstanding;
      if (events.rxByte && !phyDequeued)
         phyOutstanding <= phyOutstanding + 1;
      else if (!events.rxByte && phyDequeued)
         phyOutstanding <= phyOutstanding - 1;
   endrule

   method UartFlags flags = phy.flags;
   method Bit#(32) stat(Bit#(4) index) = link.stat(index);
   interface UartPins pins = phy.pins;
endmodule

interface UartLink;
   interface UartPins pins;
   method UartFlags flags;
   method Bit#(32) stat(Bit#(4) index);
endinterface

`ifndef CLK_HZ
`define CLK_HZ 100000000
`endif
`ifndef BAUD
`define BAUD 12000000
`endif
`ifndef LINK_FRAME_LIMIT
`define LINK_FRAME_LIMIT 240
`endif
`ifndef LINK_WINDOW_LIMIT
`define LINK_WINDOW_LIMIT 8
`endif
`ifndef LINK_SLOT_BYTES
`define LINK_SLOT_BYTES 256
`endif
`ifndef RX_DEPTH
`define RX_DEPTH 4
`endif
`ifndef BUILD_ID
`define BUILD_ID 0
`endif

// The generic top uses the caller's default reset. Board reset and power-up policy stay outside.
(* synthesize *)
module mkUartLink(UartLink);
   if (`RX_DEPTH < 4)
      error("mkUartLink: RX_DEPTH must be at least 4; depths 1 and 2 conflict with PHY fault tracking");
   if (`LINK_SLOT_BYTES < 3)
      error("mkUartLink: diagnostics requires LINK_SLOT_BYTES >= 3");
   Uart#(8) phy <- mkUart;
   ReliableLinkIfc link <- mkReliableLink(`LINK_FRAME_LIMIT, `LINK_WINDOW_LIMIT, `LINK_SLOT_BYTES);
   UartLinkAdapter adapter <- mkUartLinkAdapter(phy, link);
   Empty diagnostics <- mkLinkDiagnostics(link, `CLK_HZ, `BAUD, `BUILD_ID);

   rule linkToPhy;
      let value <- link.tx.get;
      phy.tx.put(value);
   endrule

   method UartFlags flags = adapter.flags;
   method Bit#(32) stat(Bit#(4) index) = adapter.stat(index);
   interface UartPins pins = adapter.pins;
endmodule

endpackage
