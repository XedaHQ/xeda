// Self-checking fault-boundary test for the UART-to-link receive adapter.
package TbUartLinkAdapter;

import FIFOF :: *;
import GetPut :: *;
import RegFile :: *;
import StmtFSM :: *;
import Uart :: *;
import UartTypes :: *;
import UartLinkTypes :: *;
import UartLink :: *;

interface PhyModel;
   interface Uart#(8) uart;
   method Action good(Bit#(8) value);
   method Action fault(Bit#(2) kind);
   method UInt#(16) queued;
endinterface

module mkPhyModel(PhyModel);
   FIFOF#(Bit#(8)) rxq <- mkSizedFIFOF(16);
   FIFOF#(Bit#(8)) txq <- mkFIFOF;
   Reg#(UInt#(16)) rxCount <- mkReg(0);
   PulseWire rxPut <- mkPulseWire;
   PulseWire rxGet <- mkPulseWire;
   PulseWire rxByte <- mkPulseWire;
   PulseWire framing <- mkPulseWire;
   PulseWire parity <- mkPulseWire;
   PulseWire brk <- mkPulseWire;
   PulseWire overrun <- mkPulseWire;
   RWire#(Bit#(8)) incoming <- mkRWire;

   rule enqueueIncoming (isValid(incoming.wget));
      rxq.enq(fromMaybe(?, incoming.wget));
      rxPut.send;
      rxByte.send;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule updateRxCount;
      if (rxPut && !rxGet) rxCount <= rxCount + 1;
      else if (!rxPut && rxGet) rxCount <= rxCount - 1;
   endrule

   interface Uart uart;
      interface Put tx = toPut(txq);
      interface Get rx;
         method ActionValue#(Bit#(8)) get if (rxq.notEmpty);
            let value = rxq.first;
            rxq.deq;
            rxGet.send;
            return value;
         endmethod
      endinterface
      method UartFlags flags = unpack(0);
      method Action clearFlags = noAction;
      method UartEvents events = UartEvents {
         rxByte: rxByte,
         frameError: framing,
         parityError: parity,
         brk: brk,
         overrun: overrun,
         txByte: False
      };
      method Bool txBusy = txq.notEmpty;
      interface UartPins pins;
         method Action rxd(Bit#(1) pin) = noAction;
         method Bit#(1) txd = 1;
      endinterface
   endinterface

   method Action good(Bit#(8) value);
      incoming.wset(value);
   endmethod

   method Action fault(Bit#(2) kind);
      case (kind)
         0: framing.send;
         1: parity.send;
         2: brk.send;
         default: overrun.send;
      endcase
   endmethod
   method UInt#(16) queued = rxCount;
endmodule

interface LinkModel;
   interface ReliableLinkIfc link;
   method Action setReady(Bool value);
   method ActionValue#(Bit#(8)) take;
   method UInt#(16) aborts;
   method UInt#(16) faults;
endinterface

module mkLinkModel(LinkModel);
   FIFOF#(Bit#(8)) rxq <- mkSizedFIFOF(64);
   FIFOF#(Bit#(8)) txq <- mkFIFOF;
   RegFile#(Bit#(8), Bit#(8)) captured <- mkRegFileFull;
   Reg#(Bit#(8)) capturedCount <- mkReg(0);
   Reg#(Bit#(8)) readPosition <- mkReg(0);
   Reg#(Bool) ready <- mkReg(True);
   Reg#(UInt#(16)) abortCount <- mkReg(0);
   Reg#(UInt#(16)) faultCount <- mkReg(0);
   PulseWire rxEnqueued <- mkPulseWire;

   rule captureRx (rxq.notEmpty && !rxEnqueued);
      captured.upd(capturedCount, rxq.first);
      capturedCount <= capturedCount + 1;
      rxq.deq;
   endrule

   interface ReliableLinkIfc link;
      interface Put rx;
         method Action put(Bit#(8) value) if (ready && rxq.notFull);
            rxq.enq(value);
            rxEnqueued.send;
         endmethod
      endinterface
      interface Get tx = toGet(txq);
      interface Get requestHeader;
         method ActionValue#(LinkRequestHeader) get if (False);
            return ?;
         endmethod
      endinterface
      interface Get requestPayload;
         method ActionValue#(Bit#(8)) get if (False);
            return ?;
         endmethod
      endinterface
      interface Put responseHeader;
         method Action put(LinkResponseHeader value) if (False);
            noAction;
         endmethod
      endinterface
      interface Put responsePayload;
         method Action put(Bit#(8) value) if (False);
            noAction;
         endmethod
      endinterface
      method Action abortRx;
         abortCount <= abortCount + 1;
      endmethod
      method Action recordPhyFault(Bit#(2) kind);
         faultCount <= faultCount + 1;
      endmethod
      method Bit#(32) stat(Bit#(4) index) = 0;
   endinterface

   method Action setReady(Bool value);
      ready <= value;
   endmethod
   method ActionValue#(Bit#(8)) take if (readPosition != capturedCount);
      let value = captured.sub(readPosition);
      readPosition <= readPosition + 1;
      return value;
   endmethod
   method UInt#(16) aborts = abortCount;
   method UInt#(16) faults = faultCount;
endmodule

(* synthesize *)
module mkTbUartLinkAdapter(Empty);
   PhyModel phy <- mkPhyModel;
   LinkModel sink <- mkLinkModel;
   UartLinkAdapter dut <- mkUartLinkAdapter(phy.uart, sink.link);
   Reg#(UInt#(16)) cycles <- mkReg(0);

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 400)
         $fatal(1, "FAIL: adapter fault-boundary test timed out");
   endrule

   Stmt test = seq
      // Stall the link and buffer a delimiter before the fault. It belongs to the old backlog.
      sink.setReady(False);
      phy.good('h11);
      phy.good('h00);
      phy.good('h22);
      repeat (2) noAction;
      phy.fault(0);
      phy.good('hA1);
      if (sink.aborts != 1 || sink.faults != 1)
         $fatal(1, "FAIL: first PHY fault was not recorded and aborted");

      // A repeated fault restarts the fence while bytes remain buffered. The delimiter from the
      // pre-fault queue must still be drained as old data, even though the link is stalled.
      phy.fault(1);
      repeat (1) noAction;
      if (sink.aborts != 2 || sink.faults != 2)
         $fatal(1, "FAIL: repeated PHY fault did not restart the fence");
      phy.good('h00);
      phy.good('h33);
      phy.good('h00);
      repeat (4) noAction;
      if (phy.queued != 2)
         $fatal(1, "FAIL: quarantine stopped before the rearm delimiter; queued=%0d", phy.queued);
      sink.setReady(True);
      repeat (4) noAction;

      // Only the delimiter after the entire pre-fault backlog reaches the link. A later frame is
      // then forwarded in order. The concurrently enqueued byte is quarantined before that fence.
      action
         let first <- sink.take;
         if (first != 0) $fatal(1, "FAIL: adapter did not send a zero rearm delimiter");
      endaction
      action
         let first <- sink.take;
         if (first != 'h33) $fatal(1, "FAIL: first post-fence byte was %02x", first);
      endaction
      action
         let last <- sink.take;
         if (last != 0) $fatal(1, "FAIL: first post-fence delimiter was %02x", last);
      endaction
      phy.good('h51);
      phy.good('h52);
      phy.good('h00);
      repeat (5) noAction;
      action
         let first <- sink.take;
         if (first != 'h51) $fatal(1, "FAIL: later frame byte 0 was %02x", first);
      endaction
      action
         let second <- sink.take;
         if (second != 'h52) $fatal(1, "FAIL: later frame byte 1 was %02x", second);
      endaction
      action
         let last <- sink.take;
         if (last != 0) $fatal(1, "FAIL: later frame delimiter was %02x", last);
      endaction
      $display("PASS: PHY backlog quarantine, repeated faults, stalled link and rearm");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
