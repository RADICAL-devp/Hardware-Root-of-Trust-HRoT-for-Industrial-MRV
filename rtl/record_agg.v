// record_agg.v — 5-sub-window record aggregation + NEG_ENERGY clamp (Week 4b2).
//
// This is where the RTL implements the record-level NEG_ENERGY policy
// (DECISIONS.md Week 4a2/4b2): per completed sub-window (w_done pulse,
// wired to power_calc out_valid in Week 5 top.v) it accumulates the SIGNED
// energy increments of valid slots only, then clamps ONCE:
//   total = energy_prev_in + sum(w_energy over valid slots)
//   total < 0  ->  rec_energy = 0, rec_flags bit 5 set (NEG_ENERGY)
//   total >= 0 ->  rec_energy = total, bit 5 clear
// P_AVG stays signed-exact: rec_p_sum / rec_m_total (exact half-away mean
// division is Week 5 descriptor scope, like the divider FSMs) are carried
// for the descriptor; invalid slots contribute nothing (M recorded 0).
//
// Ports: w_energy = raw signed per-window increment (may be negative —
// that is the whole point); w_p_sum = exact sub-window sum; w_m = M;
// w_ok = slot valid; w_overrun_cnt = power_calc's running overrun snapshot
// sampled WITH w_done (data windows and tombstones alike; monotonic
// within a record because top.v clears only at record close).
// energy_prev_in = u64 cumulative energy, constrained < 2^63 (Week 4a3
// headroom: ~37,700 yr at 1 Hz — never binding); all arithmetic is
// signed 64-bit. rec_zc packs the five u12 M values (slot k at bits
// [12k+11 : 12k], 0 when invalid). rec_overrun_cnt is the 5th slot's
// snapshot, u8-saturating (255 reads as ">= 255"); rec_flags bit 6 =
// OVERRUN is set iff rec_overrun_cnt > 0. rec_done pulses with the
// outputs; state then clears, so back-to-back records need no gap.
//
// No latches, no delays, Verilog-2005.
`timescale 1ns / 1ps

module record_agg #(
    parameter N_SLOTS = 5,
    parameter NEG_ENERGY_BIT = 5,
    parameter OVERRUN_BIT = 6
) (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               w_done, // 1-clk pulse with slot data
    input  wire signed [31:0] w_energy, // raw signed increment (valid slots)
    input  wire signed [63:0] w_p_sum, // exact sub-window sum (valid slots)
    input  wire        [11:0] w_m, // sub-window M
    input  wire               w_ok, // slot valid
    input  wire        [15:0] w_overrun_cnt, // running overrun snapshot
    input  wire        [63:0] energy_prev_in, // u64 cumulative, < 2^63
    output reg         [63:0] rec_energy, // clamped cumulative record energy
    output reg  signed [63:0] rec_p_sum, // exact record power sum
    output reg         [13:0] rec_m_total, // valid sample count (<= 11150)
    output reg         [59:0] rec_zc, // five u12 M values, slot k at [12k+11:12k]
    output reg          [7:0] rec_flags, // bits k valid, bit 5 NEG_ENERGY, bit 6 OVERRUN
    output reg          [7:0] rec_overrun_cnt, // u8-saturating drop count
    output reg                rec_done // 1-clk pulse with the record outputs
);

  reg  [2:0] count; // slots accumulated in the open record (0..4)
  reg signed [63:0] e_sum; // signed energy increments, slots so far
  reg signed [63:0] p_sum; // exact power sums, valid slots so far
  reg        [13:0] m_total; // valid sample counts so far
  reg         [7:0] flags; // validity bits so far
  reg        [11:0] zc_mem [0:4]; // per-slot M (0 when invalid)

  // Sign-extended increment for the 64-bit sums (explicit: Verilog will
  // not widen a naked 32-bit operand without a lint complaint).
  wire signed [63:0] w_energy_ext = {{32{w_energy[31]}}, w_energy};

  // Blocking temps for the closing slot (slot 4): the nonblocking
  // accumulators above hold slots 0..3, so the 5th slot folds in here.
  reg signed [63:0] tot_e;
  reg signed [63:0] tot_p;
  reg        [13:0] tot_m;
  reg         [7:0] tot_f;
  reg               tot_neg;
  reg         [7:0] tot_ov;
  integer k;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (k = 0; k < 5; k = k + 1) zc_mem[k] <= 12'd0;
      count <= 3'd0;
      e_sum <= 64'sd0;
      p_sum <= 64'sd0;
      m_total <= 14'd0;
      flags <= 8'd0;
      rec_energy <= 64'd0;
      rec_p_sum <= 64'sd0;
      rec_m_total <= 14'd0;
      rec_zc <= 60'd0;
      rec_flags <= 8'd0;
      rec_overrun_cnt <= 8'd0;
      rec_done <= 1'b0;
    end else begin
      rec_done <= 1'b0;
      if (w_done) begin
        zc_mem[count] <= w_ok ? w_m : 12'd0;
        if (count == (N_SLOTS - 1)) begin
          // Close the record: fold slot 4 in combinationally (blocking),
          // clamp once, publish, clear for the next record.
          tot_e = $signed(energy_prev_in) + e_sum + (w_ok ? w_energy_ext : 64'sd0);
          tot_p = p_sum + (w_ok ? w_p_sum : 64'sd0);
          tot_m = m_total + (w_ok ? {2'd0, w_m} : 14'd0);
          tot_f = flags | (w_ok ? (8'd1 << count) : 8'd0);
          tot_neg = (tot_e < 64'sd0);
          // Overrun: the 5th slot's running snapshot is the record total
          // (monotonic within a record: cleared only at record close).
          tot_ov = (w_overrun_cnt > 16'd255) ? 8'd255 : w_overrun_cnt[7:0];
          rec_energy <= tot_neg ? 64'd0 : tot_e[63:0];
          rec_p_sum <= tot_p;
          rec_m_total <= tot_m;
          rec_zc <= {(w_ok ? w_m : 12'd0),
                     zc_mem[3], zc_mem[2], zc_mem[1], zc_mem[0]};
          rec_flags <= tot_f | (tot_neg ? (8'd1 << NEG_ENERGY_BIT) : 8'd0) |
                       ((tot_ov != 8'd0) ? (8'd1 << OVERRUN_BIT) : 8'd0);
          rec_overrun_cnt <= tot_ov;
          rec_done <= 1'b1;
          count <= 3'd0;
          e_sum <= 64'sd0;
          p_sum <= 64'sd0;
          m_total <= 14'd0;
          flags <= 8'd0;
        end else begin
          if (w_ok) begin
            e_sum <= e_sum + w_energy_ext;
            p_sum <= p_sum + w_p_sum;
            m_total <= m_total + {2'd0, w_m};
            flags <= flags | (8'd1 << count);
          end
          count <= count + 3'd1;
        end
      end
    end
  end

endmodule
