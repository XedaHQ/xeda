package TbReliableLinkAckValidation;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkAckValidation(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);

   Vector#(19, Bit#(8)) open = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(16, Bit#(8)) badForwardAckRequest = vec(
      'h02, 'h40, 'h05, 'h01, 'h2c, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h54, 'h87, 'h59, 'h96, 0);
   Vector#(15, Bit#(8)) badRequestFlags = vec(
      'h02, 'h41, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h05, 'h72, 'h42, 'h4d, 'h7b, 0);
   Vector#(15, Bit#(8)) badForwardAckPoll = vec(
      'h07, 'h60, 'h08, 'h01, 'h2c, 'h01, 'h01, 'h01,
      'h01, 'h05, 'h73, 'ha2, 'h5b, 'h18, 0);
   Vector#(16, Bit#(8)) nop = vec(
      'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);
   Vector#(16, Bit#(8)) partialAck = vec(
      'h02, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h6b, 'h3c, 'hcf, 'h40, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 1000) $fatal(1, "FAIL: ACK validation test timeout");
   endrule

   rule drainTx;
      let value <- link.tx.get;
      if (value == 0) delimiters <= delimiters + 1;
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(open[i]);
      await (delimiters == 1);

      for (i <= 0; i < 15; i <= i + 1) link.rx.put(badRequestFlags[i]);
      await (link.stat(10) == 1);
      if (link.stat(6) != 0)
         $fatal(1, "FAIL: request with reserved flags reached the endpoint");

      for (i <= 0; i < 16; i <= i + 1) link.rx.put(badForwardAckRequest[i]);
      await (link.stat(10) == 2);
      if (link.stat(6) != 0)
         $fatal(1, "FAIL: request with invalid ACK reached the endpoint");

      for (i <= 0; i < 15; i <= i + 1) link.rx.put(badForwardAckPoll[i]);
      await (link.stat(10) == 3);
      if (delimiters != 1)
         $fatal(1, "FAIL: control with invalid ACK produced a reply");

      // A partial ACK must retire the one completed result with seven free slots.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nop[i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 0 || request.length != 1)
            $fatal(1, "FAIL: partial-ACK request metadata mismatch");
      endaction
      action
         let value <- link.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: partial-ACK request payload mismatch");
      endaction
      link.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 0 });
      await (link.stat(14) == 1);
      repeat (4) noAction;
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(partialAck[i]);
      repeat (20) noAction;
      if (link.stat(10) != 3 || link.stat(6) != 1)
         $fatal(1, "FAIL: valid partial ACK was rejected header=%0d requests=%0d",
                link.stat(10), link.stat(6));
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nop[i]);
      await (link.stat(10) == 4);
      if (link.stat(6) != 1)
         $fatal(1, "FAIL: stale retry dispatched after partial ACK");
      $display("PASS: fail-closed ACK validation and partial-window retirement");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
