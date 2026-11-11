`default_nettype none

// The Trivium stream cipher: a port of examples/vhdl/Trivium/trivium.vhdl with the same ports and
// parameters, so the cocotb testbench of the VHDL example checks this design too.

// The state is s[1:288], as in the specification of Trivium and in the VHDL.
/* verilator lint_off ASCRANGE */
module trivium #(
    parameter int G_SETUP_ROUNDS = 4,  // 4 is the standard value
    parameter int G_IN_BITS = 64,  // at most 80
    parameter int G_OUT_BITS = 64,  // must divide G_SETUP_ROUNDS * 288
    parameter int G_KEY_IV_BITS = 64  // 64 to 80
) (
    input logic clk,
    input logic rst,
    input logic [G_IN_BITS-1:0] din_data,
    input logic din_valid,
    output logic din_ready,
    output logic [G_OUT_BITS-1:0] ks_data,
    output logic ks_valid,
    input logic ks_ready,
    // stop keystream generation and start over (assert for one cycle when ks_valid)
    input logic rekey
);
  localparam int TRIVIUM_SIZE = 288;
  localparam int SETUP_CYCLES = G_SETUP_ROUNDS * TRIVIUM_SIZE / G_OUT_BITS;
  localparam int WORDS = G_KEY_IV_BITS / G_IN_BITS;  // input words of the key, and of the IV
  localparam int COUNT = SETUP_CYCLES > WORDS ? SETUP_CYCLES : WORDS;
  localparam int COUNTER_BITS = COUNT > 1 ? $clog2(COUNT) : 1;

  if (G_SETUP_ROUNDS * TRIVIUM_SIZE % G_OUT_BITS != 0 ||
      G_KEY_IV_BITS % G_IN_BITS != 0) begin : g_check
    $error("G_OUT_BITS must divide G_SETUP_ROUNDS * 288, and G_IN_BITS must divide G_KEY_IV_BITS");
  end

  typedef enum logic [1:0] {
    S_INIT_K,
    S_INIT_IV,
    S_SETUP,
    S_KEYSTREAM
  } state_t;

  // registers
  logic [1:TRIVIUM_SIZE] s;
  state_t state;
  logic [COUNTER_BITS-1:0] counter;
  // wires
  logic [1:TRIVIUM_SIZE] nxt_s;
  logic [1:G_IN_BITS] kiv;  // key/iv
  int ctr;  // the word of the key or IV that din_data holds: the last word comes first

  assign din_ready = state == S_INIT_K || state == S_INIT_IV;
  assign ks_valid = state == S_KEYSTREAM;
  assign ctr = WORDS == 1 ? 0 : WORDS - 1 - int'(counter);

  always_comb begin
    for (int i = 1; i <= G_IN_BITS; i++) kiv[i] = din_data[i-1];
  end

  // G_OUT_BITS rounds in one clock: the first round makes the MSB of ks_data.
  always_comb begin : update_state
    logic [1:TRIVIUM_SIZE] v;
    logic t1, t2, t3;
    v = s;
    for (int i = G_OUT_BITS - 1; i >= 0; i--) begin
      t1 = v[66] ^ v[93];
      t2 = v[162] ^ v[177];
      t3 = v[243] ^ v[288];
      ks_data[i] = t1 ^ t2 ^ t3;
      t1 = t1 ^ (v[91] & v[92]) ^ v[171];
      t2 = t2 ^ (v[175] & v[176]) ^ v[264];
      t3 = t3 ^ (v[286] & v[287]) ^ v[69];
      v = {t3, v[1:92], t1, v[94:176], t2, v[178:287]};
    end
    nxt_s = v;
  end

  always_ff @(posedge clk) begin
    if (rst) begin
      counter <= '0;
      state <= S_INIT_K;
    end else begin
      case (state)
        S_INIT_K: begin
          if (din_valid) begin
            if (ctr == 0) begin
              counter <= '0;
              state <= S_INIT_IV;
            end else begin
              counter <= counter + 1'b1;
            end
          end
          s[1+ctr*G_IN_BITS+:G_IN_BITS] <= kiv;
          s[G_KEY_IV_BITS+1:93] <= '0;
          s[94+G_KEY_IV_BITS:TRIVIUM_SIZE-3] <= '0;
          s[TRIVIUM_SIZE-2:TRIVIUM_SIZE] <= '1;
        end
        S_INIT_IV: begin
          if (din_valid) begin
            if (ctr == 0) begin
              counter <= '0;
              state <= S_SETUP;
            end else begin
              counter <= counter + 1'b1;
            end
          end
          s[94+ctr*G_IN_BITS+:G_IN_BITS] <= kiv;
        end
        S_SETUP: begin
          counter <= counter + 1'b1;
          s <= nxt_s;
          if (counter == COUNTER_BITS'(SETUP_CYCLES - 1)) begin
            counter <= '0;
            state <= S_KEYSTREAM;
          end
        end
        S_KEYSTREAM: begin
          if (rekey) state <= S_INIT_K;
          if (ks_ready) s <= nxt_s;
        end
        default: state <= S_INIT_K;
      endcase
    end
  end

endmodule

`default_nettype wire
