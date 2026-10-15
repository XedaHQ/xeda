// SPDX-License-Identifier: Apache-2.0
// Adapted from BlueLight by Kamyar Mohajerani; see ../NOTICE.md.
// Changed for Xeda: typed word streams, bounded buffering, and segment metadata.
package Ascon;

import FIFOF :: *;
import GetPut :: *;
import Vector :: *;

import AsconCipher :: *;
import AsconRound :: *;
import AsconTypes :: *;

export AsconTypes :: *;
export mkAscon;

typedef enum {
    TopIdle,
    TopLoadKey,
    TopLoadNonce,
    TopAbsorbNonce,
    TopAbsorbAD,
    TopAbsorbText,
    TopAbsorbHash,
    TopSqueezeTag,
    TopSqueezeHash,
    TopDraining
} AsconPhase deriving (Bits, Eq, FShow);

function Bit#(5) countKeep(Bit#(4) keep);
    Bit#(5) count = 0;
    for (Integer i = 0; i < 4; i = i + 1)
        if (keep[i] == 1'b1)
            count = count + 1;
    return count;
endfunction

function Bit#(4) keepForCount(Bit#(3) count);
    case (count)
        0: return 4'b0000;
        1: return 4'b0001;
        2: return 4'b0011;
        3: return 4'b0111;
        default: return 4'b1111;
    endcase
endfunction

function Bit#(32) wireWord(AsconBlock bytes, Integer offset);
    return {bytes[offset + 3], bytes[offset + 2], bytes[offset + 1], bytes[offset]};
endfunction

function Bit#(32) wireWordMasked(AsconBlock bytes, Integer offset, Bit#(3) validBytes);
    Bit#(32) word = 0;
    for (Integer i = 0; i < 4; i = i + 1)
        if (fromInteger(i) < validBytes)
            word[(i * 8) + 7:(i * 8)] = bytes[offset + i];
    return word;
endfunction

function Bit#(32) wireWord64(Bit#(64) word, Integer offset);
    Vector#(8, Bit#(8)) bytes = reverse(unpack(word));
    return {bytes[offset + 3], bytes[offset + 2], bytes[offset + 1], bytes[offset]};
endfunction

function AsconOutput makeOutput(Bit#(32) data, Bit#(4) keep, Bool last,
                                Bool endOfOperation, AsconOutputKind kind);
    return AsconOutput {
        data: data,
        keep: keep,
        last: last,
        endOfOperation: endOfOperation,
        kind: kind
    };
endfunction

(* synthesize *)
module mkAscon(AsconIfc)
    provisos (
        Mul#(UnrollFactor, paCycles, PaRounds),
        Mul#(UnrollFactor, pbCycles, PbRounds)
    );
    AsconCipherIfc cipher <- mkAsconCipher;
    FIFOF#(AsconInput) inputFifo <- mkSizedFIFOF(4);
    FIFOF#(AsconOutput) outputFifo <- mkSizedFIFOF(8);

    Reg#(AsconPhase) phase <- mkReg(TopIdle);
    Reg#(Bool) decryptOperation <- mkReg(False);
    Reg#(Bit#(2)) keyInputCount <- mkReg(0);
    Reg#(AsconBlock) nonceBuffer <- mkReg(replicate(0));
    Reg#(Bit#(2)) nonceInputCount <- mkReg(0);
    Reg#(Bit#(1)) nonceAbsorbCount <- mkReg(0);
    Reg#(AsconBlock) blockBuffer <- mkReg(replicate(0));
    Reg#(Bit#(5)) blockByteCount <- mkReg(0);
    Reg#(Bool) padPending <- mkReg(False);

    Reg#(Vector#(4, AsconOutput)) outputStage <- mkRegU;
    Reg#(Bit#(3)) outputCount <- mkReg(0);
    Reg#(Bit#(3)) outputIndex <- mkReg(0);
    Reg#(Bit#(3)) squeezeIndex <- mkReg(0);

    function Bit#(5) currentRate(Bool isHash);
        return isHash ? 5'd8 : fromInteger(valueOf(AeadRateWords) * 8);
    endfunction

    function CipherFlags makeFlags(Bool nonce, Bool ad, Bool emptyAD,
                                   Bool text, Bool hash, Bool last);
        return CipherFlags {
            nonce: nonce,
            ad: ad,
            emptyAD: emptyAD,
            ct: text && decryptOperation,
            ptct: text,
            hm: hash,
            ptCtHm: text || hash,
            ptCtAd: ad || text
        };
    endfunction

    rule rl_collect_nonce (phase == TopLoadNonce && inputFifo.first.segment == Nonce);
        let word = inputFifo.first;
        inputFifo.deq;
        AsconBlock nextNonce = nonceBuffer;
        Bit#(5) offset = zeroExtend(nonceInputCount) << 2;
        nextNonce[offset] = word.data[7:0];
        nextNonce[offset + 1] = word.data[15:8];
        nextNonce[offset + 2] = word.data[23:16];
        nextNonce[offset + 3] = word.data[31:24];
        nonceBuffer <= nextNonce;
        if (nonceInputCount == 3) begin
            nonceInputCount <= 0;
            nonceAbsorbCount <= 0;
            phase <= TopAbsorbNonce;
        end else begin
            nonceInputCount <= nonceInputCount + 1;
        end
    endrule

    rule rl_absorb_nonce (phase == TopAbsorbNonce);
        AsconBlock nonceBlock = replicate(0);
        AsconValids valids = replicate(False);
`ifdef ASCON128A
        nonceBlock = nonceBuffer;
        for (Integer i = 0; i < 16; i = i + 1)
            valids[i] = True;
`else
        Bit#(5) offset = nonceAbsorbCount == 0 ? 5'd0 : 5'd8;
        for (Integer i = 0; i < 8; i = i + 1) begin
            nonceBlock[i] = nonceBuffer[offset + fromInteger(i)];
            valids[i] = True;
        end
`endif
        CipherFlags flags = makeFlags(True, False, False, False, False, False);
        let unused <- cipher.absorb(nonceBlock, valids, False, flags);
`ifdef ASCON128A
        blockBuffer <= replicate(0);
        blockByteCount <= 0;
        phase <= TopAbsorbAD;
`else
        if (nonceAbsorbCount == 1) begin
            blockBuffer <= replicate(0);
            blockByteCount <= 0;
            phase <= TopAbsorbAD;
        end else begin
            nonceAbsorbCount <= 1;
        end
`endif
    endrule

    rule rl_absorb_input ((phase == TopAbsorbAD || phase == TopAbsorbText
                           || phase == TopAbsorbHash)
                          && outputCount == 0 && !padPending);
        let word = inputFifo.first;
        Bool isAD = phase == TopAbsorbAD;
        Bool isText = phase == TopAbsorbText;
        Bool isHash = phase == TopAbsorbHash;
        AsconSegment expected = isAD ? AssociatedData : isText ? Text : HashMessage;
        if (word.segment == expected) begin
            Bit#(5) addedBytes = countKeep(word.keep);
            Bit#(5) newCount = blockByteCount + addedBytes;
            Bit#(5) rate = currentRate(isHash);
            AsconBlock nextBlock = blockBuffer;
            for (Integer i = 0; i < 4; i = i + 1) begin
                if (word.keep[i] == 1'b1) begin
                    Bit#(5) position = blockByteCount + fromInteger(i);
                    nextBlock[position] = word.data[(i * 8) + 7:(i * 8)];
                end
            end

            Bool emptyAD = isAD && word.last && newCount == 0 && blockByteCount == 0;
            Bool emptyText = isText && word.last && newCount == 0 && blockByteCount == 0;
            if (emptyText) begin
                AsconValids noValids = replicate(False);
                CipherFlags flags = makeFlags(False, False, False, True, False, True);
                AsconBlock padBlock = replicate(0);
                padBlock[0] = 8'h80;
                inputFifo.deq;
                let unused <- cipher.absorb(padBlock, noValids, True, flags);
                phase <= TopSqueezeTag;
                squeezeIndex <= 0;
            end else if (!word.last && newCount < rate) begin
                inputFifo.deq;
                blockBuffer <= nextBlock;
                blockByteCount <= newCount;
            end else begin
                Bool fullFinalBlock = word.last && newCount == rate;
                Bool processNow = newCount == rate || word.last;
                if (processNow) begin
                    AsconBlock absorbBytes = nextBlock;
                    AsconValids valids = replicate(False);
                    for (Integer i = 0; i < 16; i = i + 1)
                        if (fromInteger(i) < newCount)
                            valids[i] = True;
                    if (word.last && newCount < rate)
                        absorbBytes[newCount] = 8'h80;

                    Bool absorbLast = word.last && !fullFinalBlock;
                    CipherFlags flags = makeFlags(False, isAD, emptyAD, isText, isHash,
                                                  absorbLast);
                    let result <- cipher.absorb(absorbBytes, valids, absorbLast, flags);
                    inputFifo.deq;
                    blockBuffer <= replicate(0);
                    blockByteCount <= 0;

                    if (isText && newCount != 0) begin
                        Vector#(4, AsconOutput) staged = replicate(makeOutput(
                            0, 0, False, False, TextOutput));
                        Bit#(3) words = 0;
                        for (Integer i = 0; i < 4; i = i + 1) begin
                            Bit#(5) offset = fromInteger(i * 4);
                            if (offset < newCount) begin
                                Bit#(5) remaining = newCount - offset;
                                Bit#(3) bytesHere = remaining >= 4 ? 3'd4 : truncate(remaining);
                                staged[i] = makeOutput(
                                    wireWordMasked(result, i * 4, bytesHere),
                                    keepForCount(bytesHere),
                                    word.last && remaining <= 4, False, TextOutput);
                                words = words + 1;
                            end
                        end
                        outputStage <= staged;
                        outputCount <= words;
                        outputIndex <= 0;
                    end

                    if (fullFinalBlock) begin
                        padPending <= True;
                    end else if (word.last) begin
                        if (isAD) begin
                            phase <= TopAbsorbText;
                        end else if (isText) begin
                            phase <= TopSqueezeTag;
                            squeezeIndex <= 0;
                        end else begin
                            phase <= TopSqueezeHash;
                            squeezeIndex <= 0;
                        end
                    end
                end else begin
                    inputFifo.deq;
                    blockBuffer <= nextBlock;
                    blockByteCount <= newCount;
                end
            end
        end
    endrule

    rule rl_absorb_padding (padPending && outputCount == 0
                            && (phase == TopAbsorbAD || phase == TopAbsorbText
                                || phase == TopAbsorbHash));
        AsconBlock padBlock = replicate(0);
        AsconValids noValids = replicate(False);
        padBlock[0] = 8'h80;
        Bool isAD = phase == TopAbsorbAD;
        Bool isText = phase == TopAbsorbText;
        Bool isHash = phase == TopAbsorbHash;
        CipherFlags flags = makeFlags(False, isAD, False, isText, isHash, True);
        let unused <- cipher.absorb(padBlock, noValids, True, flags);
        padPending <= False;
        if (isAD) begin
            phase <= TopAbsorbText;
        end else if (isText) begin
            phase <= TopSqueezeTag;
            squeezeIndex <= 0;
        end else begin
            phase <= TopSqueezeHash;
            squeezeIndex <= 0;
        end
    endrule

    rule rl_squeeze ((phase == TopSqueezeTag || phase == TopSqueezeHash)
                     && outputCount == 0);
        Bool isHash = phase == TopSqueezeHash;
        let lane <- cipher.squeeze;
        Vector#(4, AsconOutput) staged = replicate(makeOutput(
            0, 0, False, False, isHash ? DigestOutput : TagOutput));
        Bool finalLane = isHash ? squeezeIndex == 3 : squeezeIndex == 1;
        staged[0] = makeOutput(wireWord64(lane, 0), 4'b1111, False, False,
                               isHash ? DigestOutput : TagOutput);
        staged[1] = makeOutput(wireWord64(lane, 4), 4'b1111, finalLane, finalLane,
                               isHash ? DigestOutput : TagOutput);
        outputStage <= staged;
        outputCount <= 2;
        outputIndex <= 0;
        if (finalLane) begin
            phase <= TopDraining;
        end else begin
            squeezeIndex <= squeezeIndex + 1;
        end
    endrule

    rule rl_flush_output (outputCount != 0);
        outputFifo.enq(outputStage[outputIndex]);
        if (outputIndex + 1 == outputCount) begin
            outputCount <= 0;
            outputIndex <= 0;
        end else begin
            outputIndex <= outputIndex + 1;
        end
    endrule

    rule rl_finish_operation (phase == TopDraining && outputCount == 0
                              && !outputFifo.notEmpty);
        phase <= TopIdle;
    endrule

    method Action start(AsconCommand command)
        if (phase == TopIdle && !outputFifo.notEmpty && outputCount == 0);
        decryptOperation <= command.operation == Decrypt;
        nonceBuffer <= replicate(0);
        nonceInputCount <= 0;
        nonceAbsorbCount <= 0;
        blockBuffer <= replicate(0);
        blockByteCount <= 0;
        padPending <= False;
        keyInputCount <= 0;
        squeezeIndex <= 0;
        if (command.operation == Hash) begin
            cipher.start(True, False);
            phase <= TopAbsorbHash;
        end else begin
            cipher.start(False, command.newKey);
            phase <= command.newKey ? TopLoadKey : TopLoadNonce;
        end
    endmethod

    interface Put key;
        method Action put(Bit#(32) word) if (phase == TopLoadKey);
            cipher.loadKey(word);
            if (keyInputCount == 3) begin
                keyInputCount <= 0;
                phase <= TopLoadNonce;
            end else begin
                keyInputCount <= keyInputCount + 1;
            end
        endmethod
    endinterface

    interface Put inputWord;
        method Action put(AsconInput word);
            inputFifo.enq(word);
        endmethod
    endinterface

    interface Get outputWord;
        method ActionValue#(AsconOutput) get;
            let word = outputFifo.first;
            outputFifo.deq;
            return word;
        endmethod
    endinterface

    method Bool idle;
        return phase == TopIdle && !outputFifo.notEmpty && outputCount == 0;
    endmethod
endmodule

endpackage
