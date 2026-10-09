// win_counter.v — attestation monotonic counter (Week 5d1).
//
// Advances EXACTLY on valid record closes (incr_in && close_valid_in
// sampled together); a rejected close (valid=0) never advances (M-C3).
// At 2^32-1 the counter HALTS with a STICKY flag — it never wraps or
// saturates, because wrap would reuse counter-derived nonces (M-C4).
// Reset returns to CNT_INIT (volatile); production hardware needs a
// non-volatile counter (DECISIONS.md 5d1) and the verifier flags any
// second genesis or genesis-with-counter>0 as a reset event.
//
// CNT_INIT is TEST-ONLY (VPI writes do not land on Verilator flops):
// near-max testing via cocotb-test parameters=. top.v and the Yosys run
// MUST use the default 0 — enforced by the 5d2 repo check. CNT_MAX is
// the spec constant (default all-ones), not a test hook.
// No latches, Verilog-2005, rst_n async-assert / sync-release.
`timescale 1ns / 1ps

module win_counter #(
    parameter CNT_INIT = 32'd0, // test-only reset value (default 0)
    parameter CNT_MAX = 32'hFFFFFFFF // spec: halt here, never wrap
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        incr_in, // record-close pulse
    input  wire        close_valid_in, // 1: countable close; 0: rejected
    output reg  [31:0] count_out, // held value (nonce/counter source)
    output reg         halted_out // sticky: incr attempted past MAX
);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count_out <= CNT_INIT;
      halted_out <= 1'b0;
    end else if (incr_in && close_valid_in) begin
      if (count_out == CNT_MAX) halted_out <= 1'b1; // halt: no wrap (M-C4)
      else count_out <= count_out + 32'd1;
    end
    // Rejected closes (valid=0) and idle edges: no state change (M-C3).
  end

endmodule
