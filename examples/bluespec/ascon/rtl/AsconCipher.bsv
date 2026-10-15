// SPDX-License-Identifier: Apache-2.0
// Adapted from BlueLight by Kamyar Mohajerani; see ../NOTICE.md.
// Changed for Xeda: standalone dependencies and a fixed Ascon-Hash rate.
package AsconCipher;

import Vector :: *;

import AsconRound :: *;

typedef Bit#(8) AsconByte;
typedef Vector#(16, AsconByte) AsconBlock;
typedef Vector#(16, Bool) AsconValids;

typedef enum { CipherIdle, LoadKey, Absorb, Permute, Squeeze }
    CipherPhase deriving (Bits, Eq, FShow);

typedef struct {
    Bool nonce;
    Bool ad;
    Bool emptyAD;
    Bool ct;
    Bool ptct;
    Bool hm;
    Bool ptCtHm;
    Bool ptCtAd;
} CipherFlags deriving (Bits, Eq, FShow);

interface AsconCipherIfc;
    method Action start(Bool hash, Bool newKey);
    method Action loadKey(Bit#(32) word);
    method ActionValue#(AsconBlock) absorb(
        AsconBlock block, AsconValids valids, Bool last, CipherFlags flags
    );
    method ActionValue#(Bit#(64)) squeeze;
endinterface

function AsconWord laneFromBytes(AsconBlock bytes, Integer offset);
    Vector#(8, AsconByte) lane = newVector;
    for (Integer i = 0; i < 8; i = i + 1)
        lane[i] = bytes[offset + i];
    return pack(reverse(lane));
endfunction

function Vector#(8, AsconByte) laneBytes(AsconWord lane);
    return reverse(unpack(lane));
endfunction

function Bit#(32) reverseBytes32(Bit#(32) word);
    return {word[7:0], word[15:8], word[23:16], word[31:24]};
endfunction

function AsconBlock wordsToBytes(Vector#(2, AsconWord) words);
    AsconBlock bytes = replicate(0);
    Vector#(8, AsconByte) first = laneBytes(words[0]);
    Vector#(8, AsconByte) second = laneBytes(words[1]);
    for (Integer i = 0; i < 8; i = i + 1) begin
        bytes[i] = first[i];
        bytes[i + 8] = second[i];
    end
    return bytes;
endfunction

function AsconBlock clearInvalid(AsconBlock block, AsconValids valids);
    AsconBlock cleared = replicate(0);
    for (Integer i = 0; i < 16; i = i + 1)
        if (valids[i])
            cleared[i] = block[i];
    return cleared;
endfunction

(* synthesize *)
module mkAsconCipher(AsconCipherIfc)
    provisos (
        Mul#(UnrollFactor, paCycles, PaRounds),
        Mul#(UnrollFactor, pbCycles, PbRounds)
    );
    Reg#(AsconState) asconState <- mkRegU;
    Reg#(CipherPhase) phase <- mkReg(CipherIdle);
    Reg#(CipherPhase) postPermutePhase <- mkReg(Absorb);
    Reg#(Vector#(4, Bit#(32))) keyWords <- mkRegU;
    Reg#(Vector#(2, AsconWord)) keyLanes <- mkRegU;
    Reg#(Bit#(2)) keyWordCount <- mkReg(0);
    Reg#(Bit#(TLog#(paCycles))) roundCounter <- mkReg(0);
    Reg#(RoundConstant) roundConstant <- mkReg(0);
    Reg#(Bit#(2)) squeezeCounter <- mkReg(0);
    Reg#(Bit#(1)) nonceBlockCounter <- mkReg(0);
    Reg#(Bool) squeezeHash <- mkReg(False);
    Reg#(Bool) firstBlock <- mkReg(False);

    rule rl_permutation (phase == Permute);
        match {.nextState, .nextRC} = permutation(asconState, roundConstant);
        asconState <= nextState;
        roundConstant <= nextRC;
        roundCounter <= roundCounter + 1;
        Bit#(TLog#(paCycles)) finalRound = fromInteger(valueOf(paCycles) - 1);
        if (roundCounter == finalRound)
            phase <= postPermutePhase;
    endrule

    function AsconState keyNonceInit(AsconBlock nonce, Vector#(2, AsconWord) keys);
        AsconState s = asconState;
`ifdef ASCON128A
        s[0] = 64'h80800c0800000000;
        s[1] = keys[0];
        s[2] = keys[1];
        s[3] = laneFromBytes(nonce, 0);
        s[4] = laneFromBytes(nonce, 8);
`else
        s[0] = 64'h80400c0600000000;
        s[1] = keys[0];
        s[2] = keys[1];
        s[4] = laneFromBytes(nonce, 0);
`endif
        return s;
    endfunction

    method Action start(Bool hash, Bool newKey) if (phase == CipherIdle);
        squeezeHash <= hash;
        firstBlock <= True;
        roundCounter <= 0;
        squeezeCounter <= 0;
        nonceBlockCounter <= 0;
        if (hash) begin
            AsconState initialState = replicate(0);
            initialState[0] = 64'h00400c0000000100;
            asconState <= initialState;
            roundConstant <= initRC(False);
            postPermutePhase <= Absorb;
            phase <= Permute;
        end else begin
            roundConstant <= initRC(False);
            if (newKey) begin
                keyWordCount <= 0;
                phase <= LoadKey;
            end else begin
                phase <= Absorb;
            end
        end
    endmethod

    method Action loadKey(Bit#(32) word) if (phase == LoadKey);
        Vector#(4, Bit#(32)) nextWords = keyWords;
        nextWords[keyWordCount] = word;
        keyWords <= nextWords;
        if (keyWordCount == 3) begin
            Vector#(2, AsconWord) keys = newVector;
            keys[0] = {reverseBytes32(keyWords[0]), reverseBytes32(keyWords[1])};
            keys[1] = {reverseBytes32(keyWords[2]), reverseBytes32(word)};
            keyLanes <= keys;
            keyWordCount <= 0;
            phase <= Absorb;
        end else begin
            keyWordCount <= keyWordCount + 1;
        end
    endmethod

    method ActionValue#(AsconBlock) absorb(
        AsconBlock block, AsconValids valids, Bool last, CipherFlags flags
    ) if (phase == Absorb);
        Bit#(5) rateWords = flags.hm ? 5'd1 : fromInteger(valueOf(AeadRateWords));
        AsconBlock rateBytes = wordsToBytes(take(asconState));
        AsconBlock xoredBlock = replicate(0);
        AsconBlock ctBlock = replicate(0);
        for (Integer i = 0; i < 16; i = i + 1) begin
            xoredBlock[i] = block[i] ^ rateBytes[i];
            ctBlock[i] = valids[i] ? block[i] : xoredBlock[i];
        end

        AsconState absorbed = asconState;
        if (!flags.emptyAD)
            for (Integer i = 0; i < 2; i = i + 1) begin
                if (fromInteger(i) < rateWords)
                    absorbed[i] = laneFromBytes(flags.ct ? ctBlock : xoredBlock, i * 8);
            end

        Bool firstAD = flags.ad && firstBlock;
        Bool firstPtCt = flags.ptct && firstBlock;
        Bool lastPtCt = flags.ptct && last;
        Bool lastPtCtHash = last && flags.ptCtHm;
        Bool pb = flags.ptCtAd && !(flags.ptct && last);

        if (lastPtCt) begin
            for (Integer i = 0; i < 2; i = i + 1)
                absorbed[rateWords + fromInteger(i)] =
                    asconState[rateWords + fromInteger(i)] ^ keyLanes[i];
        end
        if (firstAD) begin
            absorbed[3] = asconState[3] ^ keyLanes[0];
            absorbed[4] = asconState[4] ^ keyLanes[1];
        end
        absorbed[4][0] = absorbed[4][0] ^ pack(firstPtCt);

        if (flags.nonce) begin
`ifdef ASCON128A
            asconState <= keyNonceInit(block, keyLanes);
            phase <= Permute;
            postPermutePhase <= Absorb;
`else
            nonceBlockCounter <= nonceBlockCounter + 1;
            if (nonceBlockCounter == 1) begin
                asconState <= keyNonceInit(block, keyLanes);
                phase <= Permute;
                postPermutePhase <= Absorb;
            end else begin
                asconState[3] <= laneFromBytes(block, 0);
            end
`endif
        end else begin
            asconState <= absorbed;
            if (!flags.emptyAD)
                phase <= Permute;
            postPermutePhase <= lastPtCtHash ? Squeeze : Absorb;
            squeezeHash <= flags.hm;
            roundConstant <= initRC(pb);
            if (pb)
                roundCounter <= fromInteger(valueOf(paCycles) - valueOf(pbCycles));
            else
                roundCounter <= 0;
        end
        if (last || flags.nonce)
            firstBlock <= True;
        else
            firstBlock <= False;
        return xoredBlock;
    endmethod

    method ActionValue#(Bit#(64)) squeeze if (phase == Squeeze);
        Bit#(64) outputWord = squeezeHash
            ? asconState[0]
            : (squeezeCounter[0] == 1'b1 ? asconState[4] ^ keyLanes[1]
                                         : asconState[3] ^ keyLanes[0]);
        squeezeCounter <= squeezeCounter + 1;
        if (squeezeHash) begin
            if (squeezeCounter == 3) begin
                phase <= CipherIdle;
            end else begin
                roundCounter <= 0;
                roundConstant <= initRC(False);
                postPermutePhase <= Squeeze;
                phase <= Permute;
            end
        end else if (squeezeCounter == 1) begin
            phase <= CipherIdle;
        end
        return outputWord;
    endmethod
endmodule

endpackage
