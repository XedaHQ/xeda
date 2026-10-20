package TbReliableLinkControlLiveness;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkControlLiveness(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) txBytes <- mkReg(0);
   Reg#(UInt#(8)) txDelimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);

   Vector#(19, Bit#(8)) openFrame = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(16, Bit#(8)) nopFrame = vec(
      'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);
   Vector#(15, Bit#(8)) pollFrame = vec(
      'h02, 'h60, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h05, 'h84, 'hb3, 'h64, 'h65, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 1200)
         $fatal(1, "FAIL: stalled-payload control-liveness timeout");
   endrule

   rule drainTx;
      let value <- link.tx.get;
      txBytes <= txBytes + 1;
      if (value == 0) txDelimiters <= txDelimiters + 1;
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (txDelimiters == 1);
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 0 || request.length != 1)
            $fatal(1, "FAIL: wrong request metadata seq=%0d len=%0d",
                   request.seqNo, request.length);
      endaction
      action
         let value <- link.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: request payload was %02x", value);
      endaction
      link.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 1 });
      repeat (12) noAction;
      if (txDelimiters != 1)
         $fatal(1, "FAIL: response transmitted before its payload arrived");

      // POLL must still complete while the endpoint stalls the response payload.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(pollFrame[i]);
      await (txDelimiters == 2);
      if (txBytes != 34 || link.stat(6) != 1)
         $fatal(1, "FAIL: POLL reply bytes=%0d requests=%0d",
                txBytes, link.stat(6));

      link.responsePayload.put('h80);
      await (txDelimiters == 3);
      if (link.stat(14) != 1)
         $fatal(1, "FAIL: response did not resume after POLL");
      $display("PASS: POLL control reply stays live while endpoint payload is stalled");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
