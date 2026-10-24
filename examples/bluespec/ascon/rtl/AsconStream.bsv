// SPDX-License-Identifier: Apache-2.0
// Validated 40-bit typed words for the standalone Ascon v1.2 core.
package AsconStream;

import FIFOF::*;
import GetPut::*;
import Ascon::*;
import StreamExchange::*;

export AsconWordStream(..);
export mkAsconWordStream;
export mkAsconStream;

typedef enum { Idle, Key, NonceWords, AD, Message, HashWords, Finishing, Failed }
   StreamPhase deriving (Bits, Eq, FShow);

// All state and the raw core use the module's reset. Reset discards the key.
interface AsconWordStream;
   interface Put#(Bit#(40)) inputWord;
   interface Get#(Bit#(40)) outputWord;
endinterface

function Bool contiguousKeep(Bit#(4) keep);
   return keep == 0 || keep == 1 || keep == 3 || keep == 7 || keep == 15;
endfunction

(* descending_urgency = "emitError, collectOutput, processInput" *)
module mkAsconWordStream(AsconWordStream);
   AsconIfc core <- mkAscon;
   FIFOF#(Bit#(40)) inputs <- mkSizedFIFOF(4);
   FIFOF#(Bit#(40)) outputs <- mkSizedFIFOF(4);
   Reg#(StreamPhase) phase <- mkReg(Idle);
   Reg#(Bool) haveKey <- mkReg(False);
   Reg#(Bit#(2)) wordIndex <- mkReg(0);
   Reg#(Bool) segmentHasData <- mkReg(False);
   Reg#(Bit#(32)) errorCode <- mkReg(0);

   // Error codes: 1 reserved type, 2 reserved metadata, 3 wrong order,
   // 4 no saved key, 5 malformed key, 6 malformed nonce, 7 invalid keep,
   // 8 empty final word after segment data. The failed state consumes input.
   rule processInput ((phase == Failed || errorCode == 0)
                      && !(phase == Finishing && core.idle));
      let word = inputs.first;
      Bit#(8) metadata = word[7:0];
      Bit#(3) kind = metadata[7:5];
      Bit#(32) data = word[39:8];
      Bit#(4) keep = metadata[3:0];
      Bool last = unpack(metadata[4]);
      AsconSegment segment = kind == 2 ? Nonce : kind == 3 ? AssociatedData
                            : kind == 4 ? Text : HashMessage;
      Bit#(32) failure = 0;
      if (phase != Failed) begin
         case (kind)
            0: begin
               Bit#(2) operation = data[1:0];
               Bool newKey = unpack(data[2]);
               if (metadata != 8'h10 || data[31:3] != 0 || operation == 3
                   || (operation == 2 && newKey)) failure = 2;
               else if (phase != Idle) failure = 3;
               else if (operation != 2 && !newKey && !haveKey) failure = 4;
               else begin
                  core.start(AsconCommand { operation: unpack(operation), newKey: newKey });
                  phase <= operation == 2 ? HashWords : newKey ? Key : NonceWords;
                  wordIndex <= 0;
                  segmentHasData <= False;
               end
            end
            1: begin
               if (keep != 15 || last != (wordIndex == 3)) failure = 5;
               else if (phase != Key) failure = 3;
               else begin
                  core.key.put(data);
                  wordIndex <= wordIndex + 1;
                  if (wordIndex == 3) begin
                     phase <= NonceWords;
                     haveKey <= True;
                  end
               end
            end
            2, 3, 4, 5: begin
               Bit#(3) expected = phase == NonceWords ? 2 : phase == AD ? 3
                                    : phase == Message ? 4 : 5;
               if (!(phase == NonceWords || phase == AD || phase == Message
                     || phase == HashWords) || kind != expected) failure = 3;
               else if (phase == NonceWords && (keep != 15 || last != (wordIndex == 3)))
                  failure = 6;
               else if (!contiguousKeep(keep) || (!last && keep != 15)) failure = 7;
               else if (phase != NonceWords && keep == 0 && segmentHasData) failure = 8;
               else begin
                  core.inputWord.put(AsconInput { data: data, keep: keep,
                                                 last: last, segment: segment });
                  if (phase == NonceWords) begin
                     wordIndex <= wordIndex + 1;
                     if (last) begin phase <= AD; segmentHasData <= False; end
                  end else if (last) begin
                     phase <= phase == AD ? Message : Finishing;
                     segmentHasData <= False;
                  end else segmentHasData <= True;
               end
            end
            default: failure = 1;
         endcase
         if (failure != 0) begin
            phase <= Failed;
            errorCode <= failure;
         end
      end
      inputs.deq;
   endrule

   rule emitError (errorCode != 0);
      outputs.enq({errorCode, 8'h7f});
      errorCode <= 0;
   endrule

   // Output makes progress in every input phase. Once an operation fails, its
   // remaining raw output is discarded and the terminal error is authoritative.
   rule collectOutput;
      let word <- core.outputWord.get;
      if (phase != Failed) begin
         Bit#(3) kind = word.kind == TextOutput ? 0
                        : word.kind == TagOutput ? 1 : 2;
         Bit#(8) metadata = {kind, pack(word.last), word.keep};
         outputs.enq({word.data, metadata});

      end
   endrule

   // The raw core becomes idle only after its final output has drained. Keep
   // START ordered behind that transition without sharing state writes with drain.
   rule finishOperation (phase == Finishing && core.idle);
      phase <= Idle;
   endrule

   interface inputWord = toPut(inputs);
   interface outputWord = toGet(outputs);
endmodule

// The exchange reset is an ordered barrier for the core and validation state.
module mkAsconStream#(StreamExchange#(Bit#(40), Bit#(40)) stream)(Empty);
   AsconWordStream application <- mkAsconWordStream(reset_by stream.coreReset);
   rule supplyInput;
      let word <- stream.inputWords.get;
      application.inputWord.put(word);
   endrule
   rule drainOutput;
      let word <- application.outputWord.get;
      stream.outputWords.put(word);
   endrule
endmodule
endpackage
