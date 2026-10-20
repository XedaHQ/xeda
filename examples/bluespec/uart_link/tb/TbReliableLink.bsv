package TbReliableLink;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

Vector#(19, Bit#(8)) openFrame = vec(
   'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
   'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);

Vector#(16, Bit#(8)) nopFrame = vec(
   'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);

Vector#(16, Bit#(8)) damagedNopFrame = vec(
   'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h08, 0);

Vector#(15, Bit#(8)) staleAckPoll = vec(
   'h04, 'h60, 'h07, 'hff, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h05, 'hcf, 'h8b, 'h2c, 'he2, 0);

Vector#(16, Bit#(8)) applicationDeliveryAck = vec(
   'h02, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h6b, 'h3c, 'hcf, 'h40, 0);

Vector#(16, Bit#(8)) aheadRequest = vec(
   'h03, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h04, 'h97, 'hcd, 'hfd, 0);

(* synthesize *)
module mkTbReliableLink(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) txBytes <- mkReg(0);
   Reg#(UInt#(8)) txDelimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 2000) begin
         $display("FAIL: timeout, tx bytes=%0d delimiters=%0d rx=%0d req=%0d replay=%0d crc=%0d len=%0d hdr=%0d gap=%0d drop=%0d",
                  txBytes, txDelimiters, link.stat(0), link.stat(6), link.stat(12), link.stat(8),
                  link.stat(9), link.stat(10), link.stat(11), link.stat(13));
         $fatal(1, "FAIL: reliable-link test timeout");
      end
   endrule

   rule drainTx;
      let value <- link.tx.get;
      txBytes <= txBytes + 1;
      if (value == 0) txDelimiters <= txDelimiters + 1;
   endrule

   Stmt test = seq
      // A corrupted request cannot reach the application service.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(damagedNopFrame[i]);
      await (link.stat(8) == 1);
      if (link.stat(6) != 0) $fatal(1, "FAIL: bad CRC request was committed");

      // OPEN chooses the largest frame that leaves the fixed 45-byte recovery reserve.
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (txDelimiters == 1);
      if (link.stat(0) != 35)
         $fatal(1, "FAIL: encoded RX byte counter was %0d, expected 35", link.stat(0));
      // The same OPEN token, content, session, and absolute grant must get a fresh reply.
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (txDelimiters == 2);
      if (txBytes != 38) $fatal(1, "FAIL: repeated OPEN did not emit one bounded reply");

      // A request ahead of the expected sequence gets a header-only GAP response.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(aheadRequest[i]);
      await (txDelimiters == 3);
      if (link.stat(6) != 0 || link.stat(11) != 1)
         $fatal(1, "FAIL: ahead request valid=%0d gap=%0d crc=%0d header=%0d",
                link.stat(6), link.stat(11), link.stat(8), link.stat(10));

      // A valid NOP request reserves one result slot and reaches the service once.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 0 || request.length != 1 || request.maxResponse != 196)
            $fatal(1, "FAIL: request metadata was seq=%0d len=%0d max=%0d",
                   request.seqNo, request.length, request.maxResponse);
      endaction
      action
         let value <- link.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: NOP payload was %02x", value);
      endaction
      link.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 1 });
      link.responsePayload.put('h80);
      await (link.stat(14) == 1);

      // A stale cumulative delivery ACK is rejected with no control reply.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(staleAckPoll[i]);
      await (link.stat(10) == 1);
      if (txDelimiters != 4 || link.stat(10) != 1)
         $fatal(1, "FAIL: stale control ACK reply=%0d header=%0d",
                txDelimiters, link.stat(10));
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      await (txDelimiters == 5);
      await (link.stat(14) == 2);
      if (link.stat(6) != 1 || link.stat(12) != 1)
         $fatal(1, "FAIL: replay dispatched again or was not counted");

      // An application duplicate can carry a delivery ACK and retire its cached result.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(applicationDeliveryAck[i]);
      repeat (20) noAction;
      if (link.stat(6) != 1 || link.stat(14) != 2)
         $fatal(1, "FAIL: application ACK valid=%0d responseTx=%0d duplicate=%0d",
                link.stat(6), link.stat(14), link.stat(12));

      // An old retry cannot dispatch a result after an application ACK retires it.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      repeat (20) noAction;
      if (link.stat(6) != 1)
         $fatal(1, "FAIL: old sequence executed after delivery retirement");
      if (txBytes > 256)
         $fatal(1, "FAIL: FPGA emitted %0d bytes beyond the fixed bridge grant", txBytes);

      // Abort clears decoder and speculative state, then ignores the affected frame.
      link.abortRx;
      repeat (1) noAction;
      link.rx.put('h52); link.rx.put('h61); link.rx.put(0);
      repeat (8) noAction;
      $display("PASS: OPEN, CRC rejection, service streaming, replay, ACK and grant bound");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

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

(* synthesize *)
module mkTbReliableLinkFullCacheAck(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(4)) requestIndex <- mkReg(0);

   Vector#(19, Bit#(8)) open = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(9, Vector#(15, Bit#(8))) requests = vec(
      vec('h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h3a, 'h94, 'h73, 'h8f, 0),
      vec('h03, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h1d, 'he9, 'h4f, 'hc6, 0),
      vec('h03, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h74, 'h6e, 'h0b, 'h1d, 0),
      vec('h03, 'h40, 'h03, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h53, 'h13, 'h37, 'h54, 0),
      vec('h03, 'h40, 'h04, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h57, 'h16, 'h6e, 'hae, 0),
      vec('h03, 'h40, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h70, 'h6b, 'h52, 'he7, 0),
      vec('h03, 'h40, 'h06, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h19, 'hec, 'h16, 'h3c, 0),
      vec('h03, 'h40, 'h07, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h3e, 'h91, 'h2a, 'h75, 0),
      vec('h04, 'h40, 'h08, 'h08, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'hb3, 'h6d, 'h65, 'h83, 0));
   Vector#(15, Bit#(8)) badCrcAck8 = vec(
      'h04, 'h40, 'h08, 'h08, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05,
      'hb3, 'h6d, 'h65, 'h82, 0);
   Vector#(15, Bit#(8)) invalidAck9 = vec(
      'h04, 'h40, 'h08, 'h09, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05,
      'h7b, 'h41, 'h66, 'heb, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 5000) begin
         $display("FAIL: full-cache timeout req=%0d tx=%0d duplicate=%0d drop=%0d hdr=%0d delimiters=%0d",
                  link.stat(6), link.stat(14), link.stat(12), link.stat(13),
                  link.stat(10), delimiters);
         $fatal(1, "FAIL: full-cache ACK test timeout");
      end
   endrule

   rule drainTx;
      let value <- link.tx.get;
      if (value == 0) delimiters <= delimiters + 1;
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(open[i]);
      await (delimiters == 1);

      // Fill every result slot and let each zero-length result leave the FPGA.
      for (requestIndex <= 0; requestIndex < 8; requestIndex <= requestIndex + 1) seq
         for (i <= 0; i < 15; i <= i + 1)
            link.rx.put(requests[requestIndex][i]);
         action
            let request <- link.requestHeader.get;
            if (request.seqNo != zeroExtend(pack(requestIndex)) || request.length != 0)
               $fatal(1, "FAIL: full-cache request metadata mismatch");
         endaction
         link.responseHeader.put(LinkResponseHeader {
            seqNo: zeroExtend(pack(requestIndex)), length: 0
         });
      endseq
      await (link.stat(14) == 8);
      repeat (4) noAction;

      // A bad CRC or an ACK beyond the live window must preserve the cache.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(badCrcAck8[i]);
      await (link.stat(8) == 1);
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(invalidAck9[i]);
      await (link.stat(10) == 1);
      if (link.stat(6) != 8 || link.stat(14) != 8)
         $fatal(1, "FAIL: invalid ACK or CRC changed cache/dispatch state");

      // A duplicate can still replay the cached result before ACK 8 commits.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(requests[7][i]);
      await (link.stat(14) == 9);
      if (link.stat(12) != 1 || link.stat(6) != 8)
         $fatal(1, "FAIL: cached result did not survive rejected ACK/CRC");

      // ACK all eight completed results on the ninth request, without a POLL.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(requests[8][i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 8 || request.length != 0)
            $fatal(1, "FAIL: full-cache ninth request was not dispatched");
      endaction
      if (link.stat(6) != 9 || link.stat(13) != 0)
         $fatal(1, "FAIL: full-cache ACK dispatch=%0d drops=%0d",
                link.stat(6), link.stat(13));
      $display("PASS: full cache retires by ninth-request ACK without POLL");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

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
