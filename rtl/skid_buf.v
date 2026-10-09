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
// Fixed 16x8 (localparams, no test hooks). No latches, Verilog-2005.
`timescale 1ns / 1ps

module skid_buf (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] wr_data, // pushed byte (tap serializer)
    input  wire       wr_valid, // push strobe
    output wire       wr_ready, // !full (backpressure to the serializer)
    output wire [7:0] rd_data, // FWFT head byte
    output wire       rd_valid, // !empty
    input  wire       rd_ready, // pop strobe (chain sample_ready gate)
    output reg        overflow_out, // STICKY: push-while-full (reset clears)
    output wire [4:0] occ_out // occupancy 0..16 (margin telemetry)
);

  reg [7:0] mem [0:15];
  reg [3:0] head; // write pointer
  reg [3:0] tail; // read pointer
  reg [4:0] count; // 0..16

  integer k;

  wire full = (count >= 5'd16);
  wire empty = (count == 5'd0);
  wire do_write = wr_valid && !full;
  wire do_pop = rd_ready && !empty;

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
    end else begin
      if (wr_valid && full) overflow_out <= 1'b1; // dropped AND flagged
      if (do_write) begin
        mem[head] <= wr_data;
        head <= head + 4'd1;
      end
      if (do_pop) tail <= tail + 4'd1;
      count <= count + (do_write ? 5'd1 : 5'd0) - (do_pop ? 5'd1 : 5'd0);
    end
  end

endmodule
