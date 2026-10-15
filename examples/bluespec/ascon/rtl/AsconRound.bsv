// SPDX-License-Identifier: Apache-2.0
// Adapted from BlueLight by Kamyar Mohajerani; see ../NOTICE.md.
// Changed for Xeda: word operations replace the orphan Vector typeclass instance.
package AsconRound;

import Vector :: *;

typedef Bit#(64) AsconWord;
typedef 12 PaRounds;
typedef Vector#(5, AsconWord) AsconState;
typedef Bit#(8) RoundConstant;

`ifdef ASCON128A
typedef 8 PbRounds;
typedef 2 AeadRateWords;
`else
typedef 6 PbRounds;
typedef 1 AeadRateWords;
`endif

`ifndef UNROLL_FACTOR
`define UNROLL_FACTOR 1
`endif
typedef `UNROLL_FACTOR UnrollFactor;

function RoundConstant initRC(Bool pb);
    return pb ? (valueOf(PbRounds) == 8 ? 8'hb4 : 8'h96) : 8'hf0;
endfunction

function RoundConstant nextRC(RoundConstant rc);
    Tuple2#(Bit#(4), Bit#(4)) halves = split(rc);
    return {tpl_1(halves) - 1, tpl_2(halves) + 1};
endfunction

function AsconWord rotateRight(AsconWord word, Bit#(6) amount);
    return (word >> amount) | (word << (6'd0 - amount));
endfunction

function AsconState linearDiffusion(AsconState s);
    AsconState x = newVector;
    x[0] = s[0] ^ rotateRight(s[0], 6'd19) ^ rotateRight(s[0], 6'd28);
    x[1] = s[1] ^ rotateRight(s[1], 6'd61) ^ rotateRight(s[1], 6'd39);
    x[2] = s[2] ^ rotateRight(s[2], 6'd1) ^ rotateRight(s[2], 6'd6);
    x[3] = s[3] ^ rotateRight(s[3], 6'd10) ^ rotateRight(s[3], 6'd17);
    x[4] = s[4] ^ rotateRight(s[4], 6'd7) ^ rotateRight(s[4], 6'd41);
    return x;
endfunction

function Vector#(5, Bit#(w__)) chi(Vector#(5, Bit#(w__)) s);
    Vector#(5, Bit#(w__)) shifted = rotate(s);
    Vector#(5, Bit#(w__)) mixed = newVector;
    for (Integer i = 0; i < 5; i = i + 1)
        mixed[i] = ~s[i] & shifted[i];
    Vector#(5, Bit#(w__)) shiftedMixed = rotate(mixed);
    Vector#(5, Bit#(w__)) result = newVector;
    for (Integer i = 0; i < 5; i = i + 1)
        result[i] = s[i] ^ shiftedMixed[i];
    return result;
endfunction

function AsconState substitution(AsconState s);
    Vector#(5, AsconWord) chiInput = newVector;
    chiInput[0] = s[0] ^ s[4];
    chiInput[1] = s[1];
    chiInput[2] = s[2] ^ s[1];
    chiInput[3] = s[3];
    chiInput[4] = s[4] ^ s[3];
    let x = chi(chiInput);
    AsconState transformed = newVector;
    transformed[0] = x[0] ^ x[4];
    transformed[1] = x[1] ^ x[0];
    transformed[2] = ~x[2];
    transformed[3] = x[3] ^ x[2];
    transformed[4] = x[4];
    return transformed;
endfunction

function Tuple2#(AsconState, RoundConstant) permutation(AsconState s, RoundConstant rc);
    for (Integer i = 0; i < valueOf(UnrollFactor); i = i + 1) begin
        s[2][7:0] = s[2][7:0] ^ rc;
        s = substitution(s);
        s = linearDiffusion(s);
        rc = nextRC(rc);
    end
    return tuple2(s, rc);
endfunction

endpackage
