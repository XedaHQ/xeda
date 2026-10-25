// Bounded packet-to-word adapter for a stream-processing core.
package StreamExchange;

import BRAMCore :: *;
import Clocks :: *;
import FIFOF_ :: *;
import GetPut :: *;
import LeanLink :: *;


// FIFOL1 is the standard one-entry pipeline FIFO. Its FULL_N includes DEQ.
// The endpoint reset tags differ, but their shared physical reset clears storage.
import "BVI" FIFOL1 =
module mkStreamFIFOF1(Reset sRstIn, Reset dRstIn,
                       Clock realClock, Reset realReset,
                       FIFOF_#(a) ifc)
   provisos (Bits#(a, sa));
   default_clock clk(CLK, (*unused*) CLK_GATE) = realClock;
   default_reset _rst__(RST) = realReset;
   input_reset srst() clocked_by(clk) = sRstIn;
   input_reset drst() clocked_by(clk) = dRstIn;
   parameter width = valueOf(sa);
   method enq((* reg *) D_IN) enable(ENQ) clocked_by(clk) reset_by(srst);
   method deq() enable(DEQ) clocked_by(clk) reset_by(drst);
   method (* reg *) D_OUT first clocked_by(clk) reset_by(drst);
   method FULL_N notFull clocked_by(clk) reset_by(srst);
   method FULL_N i_notFull clocked_by(clk) reset_by(srst);
   method (* reg *) EMPTY_N notEmpty clocked_by(clk) reset_by(drst);
   method (* reg *) EMPTY_N i_notEmpty clocked_by(clk) reset_by(drst);
   method clear() enable(CLR) clocked_by(clk) reset_by(_rst__);
   schedule i_notEmpty SB (deq, enq, clear);
   schedule deq SBR i_notFull;
   schedule i_notFull SB (enq, clear);
   schedule (i_notEmpty, i_notFull, notEmpty, notFull, first) CF
            (i_notEmpty, i_notFull, notEmpty, notFull, first);
   schedule deq SBR enq;
   schedule (deq, enq, clear) SBR clear;
   schedule first SB (deq, clear);
   schedule first CF enq;
   schedule notEmpty SB (deq, enq, clear);
   schedule deq SBR notFull;
   schedule notFull SB (enq, clear);
   schedule deq C deq;
   schedule enq C enq;
   path (DEQ, FULL_N);
endmodule

export StreamExchange(..);
export mkStreamExchange;

function Integer makeLengthMask(Integer inBytes);
   Integer mask = 0;
   for (Integer length = 1; length < 256; length = length + 1)
      if ((length - 1) % inBytes == 0) mask = mask + (2 ** length);
   return mask;
endfunction

function Integer acceptedCountMask(Integer maxWords);
   Integer width = 0;
   Integer value = maxWords;
   while (value > 0) begin
      width = width + 1;
      value = value / 2;
   end
   return (2 ** width) - 1;
endfunction

interface StreamExchange#(type in_t, type out_t);
   interface Get#(in_t) inputWords;
   interface Put#(out_t) outputWords;
   interface Reset coreReset;
endinterface

typedef enum { FastCollect, FastResetWait,
                  FastReplyEmpty, FastReplyStatus, FastReplyCount,
                  FastReplyStartOutput, FastReplyOutput } FastState deriving (Bits, Eq);

typedef enum {
   Collect,
   ScanOpcode,
   ScanBody,
   ScanWait,
   AdmitStart,
   AdmitRead,
   AdmitWait,
   ResetWait,
   ReplyEmpty,
   ReplyStatus,
   ReplyCount,
   ReplyStartOutput,
   ReplyOutput,
   ReplyIdle
} ExchangeState deriving (Bits, Eq);


// A request is received completely into one bounded BRAM page before validation or admission.
// The response is emitted only after input admission stops; output collection never waits for
// the core because it checks the output FIFO and the complete-word budget before each dequeue.
module mkStreamExchangeBuffered#(LeanLink#(logW, logP) link)
   (StreamExchange#(in_t, out_t))
   provisos (Bits#(in_t, inWidth), Bits#(out_t, outWidth),
             Add#(inPad, 1, inWidth), Add#(outPad, 1, outWidth));

   Integer inBytes = (valueOf(inWidth) + 7) / 8;
   Integer outBytes = (valueOf(outWidth) + 7) / 8;
   Integer maxFrameLength = min(255, (2 ** valueOf(logP)) + 7);
   Integer maxAcceptedWords = (maxFrameLength - 13) / inBytes;
   Integer countMask = acceptedCountMask(maxAcceptedWords);

   Clock clk <- exposeCurrentClock;
   Reset parentReset <- exposeCurrentReset;
   MakeResetIfc coreResetIfc <- mkReset(2, True, clk);
   Reset appReset <- mkResetEither(parentReset, coreResetIfc.new_rst);
   ReadOnly#(Bool) coreInReset <- isResetAssertedDirect(reset_by appReset);
   FIFOF_#(in_t) inputQ <- mkStreamFIFOF1(parentReset, appReset, clk, appReset);
   FIFOF_#(out_t) outputQ <- mkStreamFIFOF1(appReset, parentReset, clk, appReset);
   BRAM_DUAL_PORT#(Bit#(8), Bit#(8)) scratch <- mkBRAMCore2(256, False);

   Reg#(ExchangeState) state <- mkReg(Collect);
   Reg#(UInt#(9)) packetLength <- mkReg(0);
   Reg#(UInt#(9)) packetPosition <- mkReg(0);
   Reg#(Bool) packetEmpty <- mkReg(False);
   Reg#(Bool) packetTooLong <- mkReg(False);
   Reg#(Bool) validPacket <- mkReg(False);
   Reg#(Bool) scanBad <- mkReg(False);
   Reg#(Bool) dropInput <- mkReg(False);
   Reg#(UInt#(9)) scanPosition <- mkReg(0);
   Reg#(UInt#(9)) admitPosition <- mkReg(0);
   Reg#(UInt#(8)) admitByteIndex <- mkReg(0);
   Reg#(Bit#(TMax#(8, inWidth))) inputAssembly <- mkReg(0);
   Reg#(UInt#(8)) acceptedCount <- mkReg(0);
   Reg#(UInt#(9)) responseRemaining <- mkReg(0);
   Reg#(out_t) outputHold <- mkRegU;
   Reg#(Bit#(8)) outputByte <- mkReg(0);
   Reg#(Bit#(TMax#(8, outWidth))) outputShift <- mkReg(0);

   function LinkTok token(Bit#(8) data, Bool last) =
      LinkTok { data: data, last: last, empty: False };

   rule receivePacket (state == Collect && !coreInReset);
      let payloadLength = link.requestPayloadLength;
      let item <- link.request.get;
      packetPosition <= (item.empty || item.last) ? 0
                        : (packetPosition == 0 ? 1 : packetPosition + 1);
      if (valueOf(inWidth) % 8 == 0) begin
         if (packetPosition == 0) begin
            packetLength <= zeroExtend(payloadLength) + 1;
            dropInput <= False;
            admitByteIndex <= 0;
            inputAssembly <= 0;
            acceptedCount <= 0;
            if (item.empty) begin
               packetEmpty <= False;
               packetTooLong <= False;
               validPacket <= False;
               state <= link.responseMax < 2 ? ReplyEmpty : ReplyStatus;
            end else if (item.data == 0 && payloadLength == 1 && link.responseMax >= 2) begin
               packetEmpty <= False;
               packetTooLong <= False;
               validPacket <= True;
               coreResetIfc.assertReset;
               state <= ResetWait;
            end else if (item.data == 1 && payloadLength >= 1
                       && (payloadLength - 1) % fromInteger(inBytes) == 0
                       && link.responseMax >= 2) begin
               validPacket <= True;
               packetTooLong <= False;
               if (item.last) begin
                  state <= ReplyStatus;
               end
            end else if (item.last) begin
               validPacket <= False;
               state <= link.responseMax < 2 ? ReplyEmpty : ReplyStatus;
            end else begin
               validPacket <= False;
               packetTooLong <= True;
            end
         end else if (packetTooLong) begin
            if (item.last) begin
               packetTooLong <= False;
               acceptedCount <= 0;
               state <= link.responseMax < 2 ? ReplyEmpty : ReplyStatus;
            end
         end else begin
            Bit#(TMax#(8, inWidth)) assembled =
               (zeroExtend(item.data) << (valueOf(TMax#(8, inWidth)) - 8))
               | (inputAssembly >> 8);
            if (admitByteIndex == fromInteger(inBytes - 1)) begin
               if (!dropInput && inputQ.notFull) begin
                  inputQ.enq(unpack(truncate(assembled)));
                  acceptedCount <= (acceptedCount + 1) & fromInteger(countMask);
               end else dropInput <= True;
               inputAssembly <= 0;
               admitByteIndex <= 0;
            end else begin
               inputAssembly <= assembled;
               admitByteIndex <= admitByteIndex + 1;
            end
            if (item.last) begin
               state <= ReplyStatus;
            end
         end
      end else begin
         if (packetPosition < 243 && !item.empty)
            scratch.a.put(True, truncate(pack(packetPosition)), item.data);
         if (item.empty || item.last) begin
            packetLength <= item.empty ? 0 : packetPosition + 1;
            packetEmpty <= item.empty;
            packetTooLong <= packetTooLong || packetPosition >= 243;
            scanPosition <= 0;
            scanBad <= False;
            state <= ScanOpcode;
         end else begin
            if (packetPosition >= 243) packetTooLong <= True;
         end
      end
   endrule

   // BRAM reads are registered. One request validates its opcode and all padding before a
   // second pass can enqueue any typed input word.
   rule readOpcode (state == ScanOpcode && !packetEmpty && !packetTooLong);
      scratch.b.put(False, 0, ?);
      state <= ScanWait;
   endrule

   rule checkOpcode (state == ScanWait && scanPosition == 0);
      Bit#(8) op = scratch.b.read;
      if (packetLength == 1 && op == 0) begin
         if (link.responseMax < 2) state <= ReplyEmpty;
         else begin
            validPacket <= True;
            coreResetIfc.assertReset;
            state <= ResetWait;
         end
      end else if (op == 1 && packetLength >= 1
                   && ((packetLength - 1) % fromInteger(inBytes)) == 0) begin
         scanPosition <= 1;
         scanBad <= False;
         if (link.responseMax < 2) state <= ReplyEmpty;
         else if (packetLength == 1) begin
            validPacket <= True;
            acceptedCount <= 0;
            state <= ReplyStatus;
         end else begin
            scratch.b.put(False, 1, ?);
            state <= ScanBody;
         end
      end else begin
         validPacket <= False;
         acceptedCount <= 0;
         state <= (link.responseMax < 2) ? ReplyEmpty : ReplyStatus;
      end
   endrule

   rule scanBodyByte (state == ScanBody);
      Bit#(8) b = scratch.b.read;
      Bool finalByte = ((scanPosition - 1) % fromInteger(inBytes))
                       == fromInteger(inBytes - 1);
      Bit#(8) usedMask = 8'h01 << (valueOf(inWidth) % 8);
      usedMask = usedMask - 1;
      Bit#(8) padMask = ~usedMask;
      Bool badPad = finalByte && (valueOf(inWidth) % 8 != 0) && (b & padMask) != 0;
      scanBad <= scanBad || badPad;
      scanPosition <= scanPosition + 1;
      if (scanPosition + 1 == packetLength) begin
         validPacket <= !(scanBad || badPad || packetTooLong);
         acceptedCount <= 0;
         if (scanBad || badPad || packetTooLong) state <= ReplyStatus;
         else begin
            admitPosition <= 1;
            admitByteIndex <= 0;
            inputAssembly <= 0;
            state <= AdmitStart;
         end
      end else begin
         scratch.b.put(False, truncate(pack(scanPosition + 1)), ?);
      end
   endrule

   rule beginAdmission (state == AdmitStart && !coreInReset);
      if (!inputQ.notFull || admitPosition >= packetLength) begin
         state <= ReplyStatus;
      end else begin
         scratch.b.put(False, truncate(pack(admitPosition)), ?);
         state <= AdmitWait;
      end
   endrule

   rule admitByte (state == AdmitWait);
      Bit#(8) b = scratch.b.read;
      Bit#(TMax#(8, inWidth)) assembled;
      if (valueOf(inWidth) % 8 == 0)
         assembled = (zeroExtend(b) << (valueOf(TMax#(8, inWidth)) - 8))
                     | (inputAssembly >> 8);
      else begin
         // The bit offset reaches 8 * (inBytes - 1), which does not fit in 8 bits.
         Bit#(11) bitOffset = {pack(admitByteIndex), 3'b000};
         Bit#(TMax#(8, inWidth)) shifted = zeroExtend(b) << bitOffset;
         assembled = inputAssembly | shifted;
      end
      if (admitByteIndex == fromInteger(inBytes - 1)) begin
         inputQ.enq(unpack(truncate(assembled)));
         inputAssembly <= 0;
         admitByteIndex <= 0;
         acceptedCount <= (acceptedCount + 1) & fromInteger(countMask);
         admitPosition <= admitPosition + 1;
         state <= AdmitStart;
      end else begin
         inputAssembly <= assembled;
         admitByteIndex <= admitByteIndex + 1;
         admitPosition <= admitPosition + 1;
         scratch.b.put(False, truncate(pack(admitPosition + 1)), ?);
      end
   endrule

   rule waitForReset (state == ResetWait && !coreInReset);
      acceptedCount <= 0;
      validPacket <= True;
      state <= ReplyStatus;
   endrule

   rule rejectPacket (state == ScanOpcode && (packetEmpty || packetTooLong));
      validPacket <= False;
      acceptedCount <= 0;
      state <= (link.responseMax < 2) ? ReplyEmpty : ReplyStatus;
   endrule

   rule replyEmpty (state == ReplyEmpty && link.responseReady);
      link.response.put(LinkTok { data: 0, last: True, empty: True });
      packetEmpty <= False;
      packetTooLong <= False;
      state <= Collect;
   endrule

   rule replyStatus (state == ReplyStatus && link.responseReady);
      link.response.put(token(validPacket ? 0 : 1, False));
      responseRemaining <= zeroExtend(link.responseMax) - 2;
      state <= ReplyCount;
   endrule

   rule replyCount (state == ReplyCount && link.responseReady);
      Bool haveOutput = outputQ.notEmpty && responseRemaining >= fromInteger(outBytes);
      link.response.put(token(pack(acceptedCount), !haveOutput));
      if (haveOutput) state <= ReplyStartOutput;
      else begin
         packetEmpty <= False;
         packetTooLong <= False;
         state <= Collect;
      end
   endrule

   rule startOutput (state == ReplyStartOutput);
      outputHold <= outputQ.first;
      outputQ.deq;
      outputByte <= 0;
      outputShift <= zeroExtend(pack(outputQ.first));
      state <= ReplyOutput;
   endrule

   rule emitOutput (state == ReplyOutput && link.responseReady);
      Bit#(8) b;
      if (valueOf(outWidth) % 8 == 0) begin
         b = truncate(outputShift);
         outputShift <= outputShift >> 8;
      end else begin
         Bit#(TMax#(8, outWidth)) bits = zeroExtend(pack(outputHold));
         // The bit offset reaches 8 * (outBytes - 1), which does not fit in 8 bits.
         Bit#(11) bitOffset = {outputByte, 3'b000};
         b = truncate(bits >> bitOffset);
      end
      Bool lastByte = outputByte + 1 == fromInteger(outBytes);
      if (!lastByte) begin
         link.response.put(token(b, False));
         outputByte <= outputByte + 1;
      end else begin
         Bool more = outputQ.notEmpty
                     && responseRemaining >= fromInteger(outBytes * 2);
         link.response.put(token(b, !more));
         responseRemaining <= responseRemaining - fromInteger(outBytes);
         if (more) state <= ReplyStartOutput;
         else begin
            packetEmpty <= False;
            packetTooLong <= False;
            state <= Collect;
         end
      end
   endrule

   interface Get inputWords;
      method ActionValue#(in_t) get if (inputQ.notEmpty);
         let word = inputQ.first;
         inputQ.deq;
         return word;
      endmethod
   endinterface

   interface Put outputWords;
      method Action put(out_t word) if (outputQ.notFull);
         outputQ.enq(word);
      endmethod
   endinterface

   interface coreReset = appReset;
endmodule

module mkStreamExchangeByteAligned#(LeanLink#(logW, logP) link)
   (StreamExchange#(in_t, out_t))
   provisos (Bits#(in_t, inWidth), Bits#(out_t, outWidth),
             Add#(inPad, 1, inWidth), Add#(outPad, 1, outWidth));

   Integer inBytes = (valueOf(inWidth) + 7) / 8;
   Integer outBytes = (valueOf(outWidth) + 7) / 8;
   Integer maxFrameLength = min(255, (2 ** valueOf(logP)) + 7);
   Integer maxAcceptedWords = (maxFrameLength - 13) / inBytes;
   Integer countMask = acceptedCountMask(maxAcceptedWords);
   Bit#(256) validLengthMask = fromInteger(makeLengthMask(inBytes));

   Clock clk <- exposeCurrentClock;
   Reset parentReset <- exposeCurrentReset;
   MakeResetIfc coreResetIfc <- mkReset(2, True, clk);
   Reset appReset <- mkResetEither(parentReset, coreResetIfc.new_rst);
   ReadOnly#(Bool) coreInReset <- isResetAssertedDirect(reset_by appReset);
   FIFOF_#(in_t) inputQ <- mkStreamFIFOF1(parentReset, appReset, clk, appReset);
   FIFOF_#(out_t) outputQ <- mkStreamFIFOF1(appReset, parentReset, clk, appReset);

   Reg#(Bool) expectOpcode <- mkReg(True);
   Reg#(Bool) dropping <- mkReg(False);
   Reg#(Bool) commandOK <- mkReg(False);
   Reg#(UInt#(TMax#(1, TLog#(TMax#(2, TDiv#(TMax#(8, inWidth), 8)))))) inputByte <- mkReg(0);
   Reg#(Bit#(TMax#(8, inWidth))) inputShift <- mkRegU;
   Reg#(UInt#(8)) acceptedCount <- mkReg(0);
   Reg#(UInt#(8)) responseRemaining <- mkReg(0);
   Reg#(Bit#(outWidth)) outputHold <- mkRegU;
   // Type-level TDiv rounds up; the index spans 0 through outBytes - 1.
   Reg#(UInt#(TMax#(1, TLog#(TDiv#(outWidth, 8))))) outputByte <- mkReg(0);

   Reg#(FastState) state <- mkReg(FastCollect);

   function LinkTok token(Bit#(8) data, Bool last) =
      LinkTok { data: data, last: last, empty: False };

   rule receivePacket (state == FastCollect && !coreInReset);
      let payloadLength = link.requestPayloadLength;
      let item <- link.request.get;
      Bool validReset = !item.empty && item.data == 0 && payloadLength == 1
                        && item.last && link.responseMax >= 2;
      Bool validLength;
      if (inBytes == 1 || inBytes == 2 || inBytes == 4 || inBytes == 8
          || inBytes == 16 || inBytes == 32 || inBytes == 64)
         validLength = payloadLength >= 1
                       && (payloadLength - 1) % fromInteger(inBytes) == 0;
      // Since 16 mod 5 is 1, the two hexadecimal digits have the same residue.
      // Their sum is at most 30; these are exactly the sums congruent to 1 mod 5.
      else if (inBytes == 5) begin
         Bit#(8) lengthBits = pack(payloadLength);
         UInt#(5) nibbleSum = zeroExtend(unpack(lengthBits[3:0]))
                             + zeroExtend(unpack(lengthBits[7:4]));
         validLength = nibbleSum == 1 || nibbleSum == 6 || nibbleSum == 11
                       || nibbleSum == 16 || nibbleSum == 21 || nibbleSum == 26;
      end else validLength = validLengthMask[pack(payloadLength)] == 1;
      Bool validData = !item.empty && item.data == 1 && validLength
                       && link.responseMax >= 2;
      Bool nextDropping = expectOpcode ? (item.empty || (!validReset && !validData)) : dropping;
      expectOpcode <= item.last;
      if (expectOpcode) begin
         acceptedCount <= 0;
         inputByte <= 0;
         if (item.empty) begin
            commandOK <= False;
            state <= link.responseMax < 2 ? FastReplyEmpty : FastReplyStatus;
         end else if (validReset) begin
            commandOK <= True;
            coreResetIfc.assertReset;
            state <= FastResetWait;
         end else if (validData) begin
            commandOK <= True;
            if (item.last) state <= FastReplyStatus;
         end else if (item.last) begin
            commandOK <= False;
            state <= link.responseMax < 2 ? FastReplyEmpty : FastReplyStatus;
         end else begin
            commandOK <= False;
         end
      end else if (dropping) begin
         if (item.last) begin
            state <= link.responseMax < 2 ? FastReplyEmpty : FastReplyStatus;
         end
      end else begin
         Bit#(TMax#(8, inWidth)) assembled = (inputShift >> 8)
                               | (zeroExtend(item.data) << (valueOf(TMax#(8, inWidth)) - 8));
         if (inputByte == fromInteger(inBytes - 1)) begin
            if (inputQ.notFull) begin
               inputQ.enq(unpack(truncate(assembled)));
               acceptedCount <= (acceptedCount + 1) & fromInteger(countMask);
            end else nextDropping = True;
            if (item.last) state <= FastReplyStatus;
            if (inBytes == 1) inputByte <= 0;
            else if (inBytes == 2 || inBytes == 4 || inBytes == 8
                     || inBytes == 16 || inBytes == 32 || inBytes == 64)
               inputByte <= inputByte + 1;
            else inputByte <= 0;
         end else begin
            inputShift <= assembled;
            inputByte <= inputByte + 1;
         end
      end
      dropping <= nextDropping;
   endrule

   rule waitForReset (state == FastResetWait && !coreInReset);
      acceptedCount <= 0;
      state <= FastReplyStatus;
   endrule

   rule replyEmpty (state == FastReplyEmpty && link.responseReady);
      link.response.put(LinkTok { data: 0, last: True, empty: True });
      state <= FastCollect;
   endrule

   rule replyStatus (state == FastReplyStatus && link.responseReady);
      link.response.put(token(commandOK ? 0 : 1, False));
      responseRemaining <= link.responseMax;
      state <= FastReplyCount;
   endrule

   rule replyCount (state == FastReplyCount && link.responseReady);
      Bool haveOutput = outputQ.notEmpty && responseRemaining >= fromInteger(outBytes + 2);
      link.response.put(token(pack(acceptedCount), !haveOutput));
      if (haveOutput) state <= FastReplyStartOutput;
      else state <= FastCollect;
   endrule

   rule startOutput (state == FastReplyStartOutput);
      outputQ.deq;
      outputHold <= pack(outputQ.first);
      outputByte <= 0;
      state <= FastReplyOutput;
   endrule

   rule emitOutput (state == FastReplyOutput);
      Bit#(8) b;
      if (outBytes == 5) begin
         Bit#(TMax#(40, outWidth)) bits = zeroExtend(pack(outputHold));
         case (outputByte)
            0: b = bits[7:0];
            1: b = bits[15:8];
            2: b = bits[23:16];
            3: b = bits[31:24];
            default: b = bits[39:32];
         endcase
      end else begin
         // A byte select from the held word costs less logic than a loadable shift register.
         Bit#(TAdd#(outWidth, 7)) bits = zeroExtend(pack(outputHold));
         b = bits[7:0];
         for (Integer i = 1; i < outBytes; i = i + 1)
            if (outputByte == fromInteger(i)) b = bits[8 * i + 7 : 8 * i];
      end
      Bool lastByte = outputByte == fromInteger(outBytes - 1);
      if (!lastByte) begin
         link.response.put(token(b, False));
         outputByte <= outputByte + 1;
      end else begin
         // Two words may exceed the 8-bit reply budget even when one word fits.
         Integer twoWords = outBytes * 2 + 2;
         Bool more = outputQ.notEmpty && twoWords <= 255
                     && responseRemaining >= fromInteger(min(255, twoWords));
         link.response.put(token(b, !more));
         responseRemaining <= responseRemaining - fromInteger(outBytes);
         if (more) state <= FastReplyStartOutput;
         else state <= FastCollect;
      end
   endrule

   interface Get inputWords;
      method ActionValue#(in_t) get if (inputQ.notEmpty);
         let word = inputQ.first;
         inputQ.deq;
         return word;
      endmethod
   endinterface

   interface Put outputWords;
      method Action put(out_t word) if (outputQ.notFull);
         outputQ.enq(word);
      endmethod
   endinterface

   interface coreReset = appReset;
endmodule

module mkStreamExchange#(LeanLink#(logW, logP) link)
   (StreamExchange#(in_t, out_t))
   provisos (Bits#(in_t, inWidth), Bits#(out_t, outWidth),
             Add#(inPad, 1, inWidth), Add#(outPad, 1, outWidth));
   if (valueOf(inWidth) % 8 == 0) begin
      StreamExchange#(in_t, out_t) fast <- mkStreamExchangeByteAligned(link);
      interface inputWords = fast.inputWords;
      interface outputWords = fast.outputWords;
      interface coreReset = fast.coreReset;
   end else begin
      StreamExchange#(in_t, out_t) buffered <- mkStreamExchangeBuffered(link);
      interface inputWords = buffered.inputWords;
      interface outputWords = buffered.outputWords;
      interface coreReset = buffered.coreReset;
   end
endmodule

endpackage
