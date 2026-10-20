package LinkDiagnostics;

import GetPut :: *;
import RegFile :: *;
import Vector :: *;
import UartLinkTypes :: *;

export mkLinkDiagnostics;

typedef enum {
   DiagHeader,
   DiagRead,
   DiagCheckPrbs,
   DiagOutput
} DiagState deriving (Bits, Eq, FShow);

typedef struct {
   Bit#(8) value;
   Bit#(31) nextState;
} PrbsByte deriving (Bits, Eq, FShow);

function PrbsByte prbsNext(Bit#(31) state);
   Bit#(8) value = 0;
   Bit#(31) next = state;
   for (Integer bitIndex = 0; bitIndex < 8; bitIndex = bitIndex + 1) begin
      value = {value[6:0], next[30]};
      next = {next[29:0], next[30] ^ next[27]};
   end
   return PrbsByte { value: value, nextState: next };
endfunction

// The supplied service must allow response payloads of at least three bytes.
module mkLinkDiagnostics#(ReliableLinkIfc link,
                          Integer clockHz,
                          Integer baud,
                          Integer buildId)(Empty);
   Reg#(DiagState) state <- mkReg(DiagHeader);
   Reg#(LinkRequestHeader) currentRequest <- mkRegU;
   Reg#(Bit#(16)) requestReadPos <- mkReg(0);
   Reg#(Bit#(16)) responseWritePos <- mkReg(0);
   Reg#(Bit#(16)) responseLength <- mkReg(0);
   Reg#(Bit#(16)) checkLength <- mkReg(0);
   Reg#(Bit#(16)) checkPosition <- mkReg(0);
   Reg#(Bit#(16)) mismatchCount <- mkReg(0);
   Reg#(Bit#(8)) operation <- mkReg(0);
   Reg#(Bit#(8)) errorCode <- mkReg(0);
   Reg#(Bit#(31)) prbsState <- mkReg(1);
   Reg#(Bit#(8)) statsIndex <- mkReg(0);
   Reg#(Bit#(32)) statsSnapshot <- mkReg(0);
   Reg#(Bool) headerSent <- mkReg(False);
   Vector#(7, Reg#(Bit#(8))) requestPrefix <- replicateM(mkReg(0));
   RegFile#(Bit#(8), Bit#(8)) requestMemory <- mkRegFileFull;

   function Bit#(8) requestByte(Bit#(16) index);
      case (index)
         0: return requestPrefix[0];
         1: return requestPrefix[1];
         2: return requestPrefix[2];
         3: return requestPrefix[3];
         4: return requestPrefix[4];
         5: return requestPrefix[5];
         6: return requestPrefix[6];
         default: return requestMemory.sub(truncate(index));
      endcase
   endfunction

   function Bit#(8) wordByte(Integer value, Bit#(2) index);
      Bit#(32) word = fromInteger(value);
      case (index)
         0: return word[7:0];
         1: return word[15:8];
         2: return word[23:16];
         default: return word[31:24];
      endcase
   endfunction

   function Bit#(16) requestCount();
      return {requestByte(2), requestByte(1)};
   endfunction

   function Bit#(32) requestSeed();
      return {requestByte(4), requestByte(3), requestByte(2), requestByte(1)};
   endfunction

   rule getHeader (state == DiagHeader);
      let header <- link.requestHeader.get;
      currentRequest <= header;
      requestReadPos <= 0;
      state <= DiagRead;
   endrule

   rule readPayload (state == DiagRead && requestReadPos < currentRequest.length);
      let value <- link.requestPayload.get;
      requestMemory.upd(truncate(requestReadPos), value);
      for (Integer i = 0; i < 7; i = i + 1)
         if (requestReadPos == fromInteger(i)) requestPrefix[i] <= value;
      requestReadPos <= requestReadPos + 1;
   endrule

   rule dispatch (state == DiagRead && requestReadPos == currentRequest.length);
      Bit#(8) op = requestByte(0);
      Bit#(16) length = currentRequest.length;
      Bit#(16) maxResponse = currentRequest.maxResponse;
      responseWritePos <= 0;
      headerSent <= False;

      if (length == 0) begin
         operation <= 0;
         errorCode <= 2;
         responseLength <= 3;
         state <= DiagOutput;
      end
      else begin
         operation <= op;
         case (op)
         8'h00: begin
            if (length != 1) begin errorCode <= 2; responseLength <= 3; end
            else begin errorCode <= 0; responseLength <= 1; end
            state <= DiagOutput;
         end
         8'h02: begin
            if (length != 1) begin errorCode <= 2; responseLength <= 3; end
            else if (maxResponse < 13) begin errorCode <= 2; responseLength <= 3; end
            else begin errorCode <= 0; responseLength <= 13; end
            state <= DiagOutput;
         end
         8'h03: begin
            if (length != 2) begin errorCode <= 2; responseLength <= 3; end
            else if (requestByte(1) > 15) begin errorCode <= 3; responseLength <= 3; end
            else if (maxResponse < 6) begin errorCode <= 2; responseLength <= 3; end
            else begin
               errorCode <= 0;
               statsIndex <= requestByte(1);
               statsSnapshot <= link.stat(unpack(requestByte(1)[3:0]));
               responseLength <= 6;
            end
            state <= DiagOutput;
         end
         8'h30: begin
            Bit#(16) count = requestCount;
            Bit#(17) expectedLength = zeroExtend(count) + 17'd3;
            Bit#(17) responseSize = zeroExtend(count) + 17'd1;
            Bool shapeOk = length >= 3 && zeroExtend(length) == expectedLength;
            if (!shapeOk) begin errorCode <= 2; responseLength <= 3; state <= DiagOutput; end
            else if (count >= 16'd256 || count >= maxResponse) begin
               errorCode <= 2; responseLength <= 3; state <= DiagOutput;
            end
            else begin
               errorCode <= 0;
               responseLength <= truncate(responseSize);
               state <= DiagOutput;
            end
         end
         8'h31: begin
            Bit#(32) seed = requestSeed;
            Bit#(16) count = {requestByte(6), requestByte(5)};
            Bool shapeOk = length == 7;
            if (!shapeOk) begin errorCode <= 2; responseLength <= 3; state <= DiagOutput; end
            else if (seed[31] == 1 || seed[30:0] == 0) begin
               errorCode <= 3; responseLength <= 3; state <= DiagOutput;
            end
            else if (count >= maxResponse || count >= 16'd256) begin
               errorCode <= 2; responseLength <= 3; state <= DiagOutput;
            end
            else begin
               errorCode <= 0;
               prbsState <= truncate(seed);
               responseLength <= count + 1;
               state <= DiagOutput;
            end
         end
         8'h32: begin
            Bit#(32) seed = requestSeed;
            Bit#(16) count = {requestByte(6), requestByte(5)};
            Bit#(17) expectedLength = zeroExtend(count) + 17'd7;
            Bool shapeOk = zeroExtend(length) == expectedLength;
            if (!shapeOk) begin errorCode <= 2; responseLength <= 3; state <= DiagOutput; end
            else if (seed[31] == 1 || seed[30:0] == 0) begin
               errorCode <= 3; responseLength <= 3; state <= DiagOutput;
            end
            else begin
               errorCode <= 0;
               prbsState <= truncate(seed);
               checkLength <= count;
               checkPosition <= 0;
               mismatchCount <= 0;
               responseLength <= 3;
               state <= DiagCheckPrbs;
            end
         end
         default: begin errorCode <= 1; responseLength <= 3; state <= DiagOutput; end
         endcase
      end
   endrule

   rule checkPrbs (state == DiagCheckPrbs && checkPosition < checkLength);
      PrbsByte expected = prbsNext(prbsState);
      if (requestByte(checkPosition + 7) != expected.value)
         mismatchCount <= mismatchCount + 1;
      checkPosition <= checkPosition + 1;
      prbsState <= expected.nextState;
   endrule

   rule finishPrbsCheck (state == DiagCheckPrbs && checkPosition == checkLength);
      state <= DiagOutput;
      responseWritePos <= 0;
   endrule

   function Bit#(8) responseByte(Bit#(16) index);
      if (errorCode != 0) begin
         case (index)
            0: return 8'hFF;
            1: return errorCode;
            default: return operation;
         endcase
      end
      else case (operation)
         8'h00: return 8'h80;
         8'h02: begin
            case (index)
               0: return 8'h82;
               1: return wordByte(clockHz, 0);
               2: return wordByte(clockHz, 1);
               3: return wordByte(clockHz, 2);
               4: return wordByte(clockHz, 3);
               5: return wordByte(baud, 0);
               6: return wordByte(baud, 1);
               7: return wordByte(baud, 2);
               8: return wordByte(baud, 3);
               9: return wordByte(buildId, 0);
               10: return wordByte(buildId, 1);
               11: return wordByte(buildId, 2);
               default: return wordByte(buildId, 3);
            endcase
         end
         8'h03: begin
            case (index)
               0: return 8'h83;
               1: return statsIndex;
               2: return statsSnapshot[7:0];
               3: return statsSnapshot[15:8];
               4: return statsSnapshot[23:16];
               default: return statsSnapshot[31:24];
            endcase
         end
         8'h30: begin
            if (index == 0) return 8'hB0;
            else return requestByte(index + 2);
         end
         8'h31: begin
            if (index == 0) return 8'hB1;
            else return 0;
         end
         8'h32: begin
            case (index)
               0: return 8'hB2;
               1: return truncate(mismatchCount);
               default: return truncate(mismatchCount >> 8);
            endcase
         end
         default: return 0;
      endcase
   endfunction

   rule writeResponseHeader (state == DiagOutput && !headerSent);
      Bit#(16) len = errorCode != 0 ? 3 : responseLength;
      link.responseHeader.put(LinkResponseHeader {
         seqNo: currentRequest.seqNo,
         length: len
      });
      headerSent <= True;
   endrule

   rule writeResponsePayload (state == DiagOutput && headerSent &&
                              responseWritePos < responseLength);
      Bit#(8) value;
      if (errorCode == 0 && operation == 8'h31 && responseWritePos != 0) begin
         PrbsByte generated = prbsNext(prbsState);
         value = generated.value;
         prbsState <= generated.nextState;
      end
      else value = responseByte(responseWritePos);
      link.responsePayload.put(value);
      responseWritePos <= responseWritePos + 1;
      if (responseWritePos + 1 == responseLength) begin
         state <= DiagHeader;
         headerSent <= False;
      end
   endrule
endmodule

endpackage
