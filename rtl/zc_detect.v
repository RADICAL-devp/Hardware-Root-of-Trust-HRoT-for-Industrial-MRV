// zc_detect.v — rising-zero-crossing detector + window validator (Week 4b).
//
// Maps bit-for-bit to sensors/windows.py find_rising_crossings_q15 +
// build_windows (DECISIONS.md D-05, Week 4a2): arm when v_q15 < ARM_Q15
// (-328 codes = -5.0049 V), trigger on the first v_q15 >= 0 while armed
// AND the previous sample was < 0. The detection sample's counter_in value
// IS the window boundary (no division in the detection path).
//
// Window convention: [S, E) over N_CYCLES = 10 consecutive detections — S
// included, E excluded and owned by the next window; M = E - S by counter
// subtraction. Validity = every inter-crossing gap in [GAP_MIN, GAP_MAX]
// AND M in [M_MIN, M_MAX]; violation -> win_valid = 0 (flagged, never
// healed). The raw M is still reported on M[11:0] during win_end_strobe;
// forcing recorded-M/sums to 0 on invalid windows is power_calc's job.
//
// Timing: all outputs are COMBINATIONAL on (sample_valid, v_q15,
// counter_in) plus registered detector state, valid during the detection
// sample's cycle. power_calc registers them, so a direct zc->power wiring
// (Week 5 top.v) is synchronous with zero ambiguity. Strobes are 1-cycle
// iff the testbench holds sample_valid for 1 cycle per sample.
//
// No latches (outputs are pure assigns), no delays, Verilog-2005.
`timescale 1ns / 1ps

module zc_detect #(
    parameter N_CYCLES = 10,
    parameter GAP_MIN  = 150,
    parameter GAP_MAX  = 260,
    parameter M_MIN    = 1810,
    parameter M_MAX    = 2230
) (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               sample_valid,
    input  wire signed [15:0] v_q15,
    input  wire signed [15:0] i_q15, // carried for single-bus symmetry; unused here
    input  wire        [31:0] counter_in,
    output wire               cross_strobe, // 1 per detection
    output wire               win_start_pulse, // S of a window detected
    output wire        [11:0] M, // E - S, live during win_end_strobe
    output wire               win_valid, // gap + range checks passed
    output wire               win_end_strobe // E detected: window closes (E excluded)
);

  // Integer thresholds: ARM = round(-5/500*32768) = -328, TRIG = 0.
  localparam signed [15:0] ARM_Q15 = -16'sd328;
  localparam signed [15:0] TRIG_Q15 = 16'sd0;

  reg        armed; // set by a sample < ARM, cleared on detection
  reg        prev_neg; // previous sample_valid sample was < 0
  reg        have_prev; // at least one detection seen (gap defined)
  reg        gap_bad; // a latched out-of-range gap inside this window
  reg  [3:0] cross_cnt; // detections in this window so far (0..N_CYCLES)
  reg [31:0] start_cnt; // counter of S
  reg [31:0] prev_cross_cnt; // counter of previous detection (gap base)

  wire detect = sample_valid & armed & prev_neg & (v_q15 >= TRIG_Q15);
  wire [31:0] gap_now = counter_in - prev_cross_cnt;
  wire gap_now_ok = ~have_prev | ((gap_now >= GAP_MIN) & (gap_now <= GAP_MAX));
  wire [31:0] m_full = counter_in - start_cnt;
  wire m_ok = ((m_full >= M_MIN) & (m_full <= M_MAX));
  wire window_end = detect & (cross_cnt == N_CYCLES);
  wire window_start = detect & ((cross_cnt == 4'd0) | (cross_cnt == N_CYCLES));
  // Current gap belongs to the closing window: fold it in combinationally.
  wire gap_bad_done = ~(gap_bad | ~gap_now_ok);

  assign cross_strobe = detect;
  assign win_start_pulse = window_start;
  assign win_end_strobe = window_end;
  assign M = m_full[11:0]; // exact: valid windows satisfy M < 4096
  assign win_valid = window_end & gap_bad_done & m_ok;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      armed <= 1'b0;
      prev_neg <= 1'b0;
      have_prev <= 1'b0;
      gap_bad <= 1'b0;
      cross_cnt <= 4'd0;
      start_cnt <= 32'd0;
      prev_cross_cnt <= 32'd0;
    end else if (sample_valid) begin
      prev_neg <= (v_q15 < TRIG_Q15);
      if (detect) begin
        armed <= 1'b0;
        prev_cross_cnt <= counter_in;
        have_prev <= 1'b1;
        if (cross_cnt == N_CYCLES) begin
          // E closes this window and opens the next (counted as its first).
          start_cnt <= counter_in;
          cross_cnt <= 4'd1;
          gap_bad <= 1'b0;
        end else begin
          if (cross_cnt == 4'd0) start_cnt <= counter_in;
          cross_cnt <= cross_cnt + 4'd1;
          if (have_prev & ~gap_now_ok) gap_bad <= 1'b1;
        end
      end else if (v_q15 < ARM_Q15) begin
        armed <= 1'b1;
      end
    end
  end

endmodule
