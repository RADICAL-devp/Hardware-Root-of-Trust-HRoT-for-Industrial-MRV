// skid_buf.v — 16-deep byte skid FIFO with sticky overflow (Week 5d2).
//
// Production FIFO between the frame tap and chain_ctrl's sample port:
// the tap produces bytes whenever frames validate; the chain consumes
// only in SAMP while the wrap is ready. Depth 16 covers the worst
// wire-rate backlog (non-SAMP spans per record ~= 1100 cycles ~= 10-14
// hash bytes at 880-clock frame pacing) with margin; at 10 kHz line
// rate the realistic peak is one 8-byte sample tick.
//
// FWFT head (rd_data/rd_valid combinational on !empty); pop on
// rd_ready && rd_valid; single-cycle write+pop nets to zero (same
// pattern as sha256_wrap's FIFO count). Push while full sets the STICKY
// overflow flag and DROPS the byte — fail-loud, never silent (top.v
// turns the flag into window_flags bit 7 HASH_OVERFLOW + invalid, and
// counts it in the sidecar). Cleared by reset only (top consumes the
// flag into the descriptor; there is no mid-record clear to race).
// Fixed 16x8 default (test-overridable via top.v SKID_DEPTH, e.g. 4
// for the e2e overflow-flag test; production/CI/synth use 16).
// Pointers stay [3:0]/[4:0] at any depth and wrap explicitly at
// DEPTH-1 (no $clog2: Verilog-2005). No latches, Verilog-2005.
`timescale 1ns / 1ps

module skid_buf #(
    parameter DEPTH = 16 // production depth (test hook via top; unsized: -G safe)
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] wr_data, // pushed byte (tap serializer)
    input  wire       wr_valid, // push strobe
    output wire       wr_ready, // !full (backpressure to the serializer)
    output wire [7:0] rd_data, // FWFT head byte
    output wire       rd_valid, // !empty
    input  wire       rd_ready, // pop strobe (chain sample_ready gate)
    output reg        overflow_out, // STICKY: push-while-full (flag_consume clears)
    output reg  [15:0] drop_cnt_out, // SATURATING dropped-byte count (cnt_clear clears)
    input  wire       flag_consume, // 1-edge: top consumed the flag into a descriptor
    input  wire       cnt_clear, // per-record clear strobe (clear-then-count discipline)
    output wire [4:0] occ_out // occupancy 0..16 (margin telemetry)
);

  reg [7:0] mem [0:15]; // sized for max DEPTH=16 (smaller DEPTH uses 0..DEPTH-1)
  reg [3:0] head; // write pointer (wraps at DEPTH-1)
  reg [3:0] tail; // read pointer (wraps at DEPTH-1)
  reg [4:0] count; // 0..DEPTH

  integer k;

  wire full = (count >= DEPTH[4:0]);
  wire empty = (count == 5'd0);
  wire do_write = wr_valid && !full;
  wire do_pop = rd_ready && !empty;
  wire do_drop = wr_valid && full; // pushed byte dropped AND flagged

  assign wr_ready = !full;
  assign rd_data = mem[tail];
  assign rd_valid = !empty;
  assign occ_out = count;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (k = 0; k < 16; k = k + 1) mem[k] <= 8'd0;
      head <= 4'd0;
      tail <= 4'd0;
      count <= 5'd0;
      overflow_out <= 1'b0;
      drop_cnt_out <= 16'd0;
    end else begin
      // Drop-wins: a consume edge simultaneous with a drop keeps the
      // flag (the new event is not lost); the count is exact bytes.
      if (do_drop) overflow_out <= 1'b1; // dropped AND flagged
      else if (flag_consume) overflow_out <= 1'b0;
      if (cnt_clear) drop_cnt_out <= do_drop ? 16'd1 : 16'd0;
      else if (do_drop && drop_cnt_out != 16'hFFFF)
        drop_cnt_out <= drop_cnt_out + 16'd1;
      if (do_write) begin
        mem[head] <= wr_data;
        head <= (head == DEPTH[3:0] - 4'd1) ? 4'd0 : head + 4'd1;
      end
      if (do_pop) tail <= (tail == DEPTH[3:0] - 4'd1) ? 4'd0 : tail + 4'd1;
      count <= count + (do_write ? 5'd1 : 5'd0) - (do_pop ? 5'd1 : 5'd0);
    end
  end

endmodule
