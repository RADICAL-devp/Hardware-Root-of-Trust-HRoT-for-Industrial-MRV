// power_calc.v — per-window fixed-point power measurement (Week 4b).
//
// Bit-exact vs tb/golden.py window_power (all fields) with the variable
// count M supplied on the M[11:0] port (from zc_detect, or the testbench
// in unit tests). Contract: power_calc TRUSTS M (range validation is
// zc_detect's job); valid windows satisfy M in [1810, 2230].
//
// DESCRIPTOR SOURCE (Week 4b2 decision, DECISIONS.md): p_avg_exact is the
// ONLY mean that feeds the attestation descriptor (== mean_q30_half_away,
// exactly what edge/attestation.py and tb/golden.py record_fields use, so
// the Week 5 verifier re-derives it without LUT knowledge). p_avg_lut is
// characterization only, behind parameter LUT_MEAN_ENABLE (0 ties it to 0
// so synthesis trims the ROM/multiplier in exact-only builds).
//
// Outputs, latched by a 3-deep behavioral pipeline (see LATENCY NOTE below):
//   p_avg_exact : i32 Q30 = round-half-away(sum(v*i) / M) — the byte-exact
//                 descriptor P_AVG target (== mean_q30_half_away).
//   p_avg_lut   : i32 Q30 = (sum * LUT[M]) >> 24, LUT = 512x24-bit ROM
//                 (rtl/recip_lut.vh) — the divider-replacement path
//                 (== lut_window_mean). Characterization only.
//   vrms/irms   : u16 Q15, isqrt_round_half_up over the half-away mean of
//                 squares; root 32768 saturates to 32767 (single corner).
//   pf_q15      : i16 Q15 = round-half-away(P_AVG*2^15 / (Vrms*Irms)),
//                 saturated, 0 on zero denominator — EXACT, no LUT.
//   energy_uwh  : i32 raw signed increment == energy_uwh_increment
//                 (round-half-away(p_sum*12500 / (9*2^30))); negative on
//                 negative-P windows (record-level NEG_ENERGY clamp lives
//                 in record_fields/build_record, Week 4a2 policy).
//   m_out       : M, or 0 when invalid. p_sum_q30: exact 64-bit sum for
//                 record-level P_AVG composition (45-bit minimum, Week 4a3).
// Invalid window (win_valid_in = 0, or M = 0): ALL data outputs forced 0
// (never a plausible number), valid_out = 0, out_valid still pulses.
//
// LATENCY NOTE (Week 4b2): the divider (udiv64/div_half_away) and square
// root (isqrt_ru) above are COMBINATIONAL loops evaluated inside one clock
// cycle — correct in simulation (Verilator/Icarus) but NOT a hardware
// timing claim. The reported "3 cycles" (last-sample -> out_valid) is the
// BEHAVIORAL-simulation pipeline depth:
//   t+1: shadow latch (sums/M/valid)          [accumulate]
//   t+2: means + energy + rms magnitudes      [LUT multiply-shift etc.]
//   t+3: PF + all output regs + out_valid     [register]
// Week 5 converts the divider/sqrt to iterative FSMs (restoring
// divide ~64 cycles, restoring sqrt 32 cycles) and re-states the true
// cycle count; until then no hardware latency is claimed.
// Consecutive win_end pulses are >= 10 cycles apart by construction (one
// detection per cycle max, 10 detections per window), so the 3-deep pipe
// can never overlap. Accumulators are 64-bit (43-bit minimum per window).
//
// Arithmetic: restoring-unsigned divider (udiv64) + half-away wrapper
// (== div_round_half_away), binary restoring square root (==
// isqrt_round_half_up, rem > root rounds up). Denominators are provably
// nonzero on the guarded paths (M, Vrms*Irms, 9*2^30); M = 0 forces the
// invalid path without dividing.
//
// No latches, no delays, Verilog-2005.
`timescale 1ns / 1ps

module power_calc #(
    parameter LUT_MEAN_ENABLE = 1 // 0: p_avg_lut ties 0 (exact-only build)
) (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               sample_valid,
    input  wire signed [15:0] v_q15,
    input  wire signed [15:0] i_q15,
    input  wire               win_start_pulse, // S presented: open window WITH sample
    input  wire               win_end_strobe, // E presented: close (E excluded)
    input  wire        [11:0] M, // E - S, live during win_end_strobe
    input  wire               win_valid_in,
    output reg  signed [31:0] p_avg_lut,
    output reg  signed [31:0] p_avg_exact,
    output reg         [15:0] vrms_q15,
    output reg         [15:0] irms_q15,
    output reg  signed [15:0] pf_q15,
    output reg  signed [31:0] energy_uwh,
    output reg         [11:0] m_out,
    output reg                valid_out,
    output reg  signed [63:0] p_sum_q30,
    output reg                out_valid // 1-clk pulse, 3 clocks after win_end
);

  // ENERGY denominator: 9 * 2^30 (fs = 10 kHz baked in, asserted in TB).
  localparam [33:0] E_DEN = 34'd9663676416;
  localparam [11:0] LUT_BASE = 12'd1810;

  // Window accumulators (64-bit; 43-bit signed minimum per sub-window).
  reg signed [63:0] sum_p, sum_v2, sum_i2;
  // Shadow latch: closed-window sums + count + validity.
  reg signed [63:0] sh_p, sh_v2, sh_i2;
  reg        [11:0] sh_M;
  reg               sh_valid;
  // Pipeline staging.
  reg        s1_v; // shadow ready -> compute mid regs next edge
  reg        s2_v; // mid regs ready -> finalize outputs next edge
  // Mid (stage-2) registers.
  reg signed [31:0] r_pavg_lut, r_pavg_exact;
  reg        [15:0] r_vrms, r_irms;
  reg signed [31:0] r_energy;

  // Unsigned restoring divider, 64/64 -> 64. den != 0 required (guarded).
  function [63:0] udiv64;
    input [63:0] n;
    input [63:0] d;
    reg [63:0] q;
    reg [64:0] r;
    integer k;
    begin
      q = 64'd0;
      r = 65'd0;
      for (k = 63; k >= 0; k = k - 1) begin
        r = (r << 1) | {64'd0, n[k]};
        if (r >= {1'b0, d}) begin
          r = r - {1'b0, d};
          q[k] = 1'b1;
        end
      end
      udiv64 = q;
    end
  endfunction

  // Round-half-away signed division (== div_round_half_away). den > 0.
  // |num| < 2^63 required (true: |products| < 2^56 on every call path).
  function signed [63:0] div_half_away;
    input signed [63:0] num;
    input [63:0] den;
    reg neg;
    reg [63:0] mag, q, r;
    begin
      neg = (num < 0);
      mag = neg ? (~num + 64'd1) : num;
      q = udiv64(mag, den);
      r = mag - q * den;
      if ((r << 1) >= den) q = q + 64'd1;
      div_half_away = neg ? (64'd0 - q) : q;
    end
  endfunction

  // Binary restoring square root with half-up rounding (== isqrt_round_half_up:
  // rem > root -> +1). Returns floor root + rounding; caller saturates.
  function [31:0] isqrt_ru;
    input [63:0] x;
    reg [31:0] root;
    reg [63:0] rem, trial;
    integer k;
    begin
      root = 32'd0;
      rem = 64'd0;
      for (k = 31; k >= 0; k = k - 1) begin
        rem = (rem << 2) | ((x >> (2 * k)) & 64'd3);
        trial = {30'd0, root, 2'b00} + 64'd1;
        if (rem >= trial) begin
          rem = rem - trial;
          root = (root << 1) | 32'd1;
        end else begin
          root = root << 1;
        end
      end
      if (rem > {32'd0, root}) root = root + 32'd1;
      isqrt_ru = root;
    end
  endfunction

  // Reciprocal LUT: 24-bit round(2^24 / M), index M - 1810.
  // rtl/recip_lut.vh IS the case statement (generated, checked in).
  function [23:0] recip_lut;
    input [8:0] lut_idx;
    reg [23:0] lut_val;
    begin
`include "recip_lut.vh"
      recip_lut = lut_val;
    end
  endfunction

  wire we = win_end_strobe & sample_valid;
  wire ws = win_start_pulse & sample_valid;
  wire s1_ok = sh_valid & (sh_M != 12'd0);

  // Stage-2 combinational results (functions of the shadow latch).
  wire signed [63:0] c_pavg_exact_64 = div_half_away(sh_p, {52'd0, sh_M});
  wire signed [63:0] c_energy_64 = div_half_away(sh_p * 64'sd12500, {30'd0, E_DEN});
  wire signed [63:0] c_mean_v2_64 = div_half_away(sh_v2, {52'd0, sh_M});
  wire signed [63:0] c_mean_i2_64 = div_half_away(sh_i2, {52'd0, sh_M});
  wire [31:0] c_root_v = isqrt_ru(c_mean_v2_64[63:0]);
  wire [31:0] c_root_i = isqrt_ru(c_mean_i2_64[63:0]);

  // Valid M in [1810, 2230] keeps the difference under 512, so [8:0] is
  // exact; out-of-range M hits the ROM default and forces zero outputs.
  wire [11:0] c_lut_sub = sh_M - LUT_BASE;
  wire [8:0] c_lut_idx = c_lut_sub[8:0];
  // NB: operands widened to 88 bits first — a bare 64x25 multiply would
  // truncate under Verilog width rules. Defense in depth (Week 4b2
  // mutation M6): the in-contract max product is 2230·32768·32767·9269 =
  // 2.22e16 < 2^54, so no in-contract test can observe a truncation and
  // invalid-window garbage never surfaces (outputs forced 0) — the 88-bit
  // form stands so contract violations cannot silently wrap.
  wire signed [87:0] c_lut_prod =
    $signed({{24{sh_p[63]}}, sh_p}) * $signed({63'd0, recip_lut(c_lut_idx)});

  // Stage-3 combinational PF (function of the mid registers).
  wire signed [63:0] c_pf_num = r_pavg_exact * 64'sd32768;
  wire [31:0] c_pf_den = r_vrms * r_irms;
  wire signed [63:0] c_pf_64 = div_half_away(c_pf_num, {32'd0, c_pf_den});
  wire signed [15:0] c_pf_sat = (c_pf_den == 32'd0) ? 16'sd0 :
                                (c_pf_64 > 64'sd32767) ? 16'sd32767 :
                                (c_pf_64 < -64'sd32768) ? -16'sd32768 : c_pf_64[15:0];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sum_p <= 64'sd0;
      sum_v2 <= 64'sd0;
      sum_i2 <= 64'sd0;
      sh_p <= 64'sd0;
      sh_v2 <= 64'sd0;
      sh_i2 <= 64'sd0;
      sh_M <= 12'd0;
      sh_valid <= 1'b0;
      s1_v <= 1'b0;
      s2_v <= 1'b0;
      r_pavg_lut <= 32'sd0;
      r_pavg_exact <= 32'sd0;
      r_vrms <= 16'd0;
      r_irms <= 16'd0;
      r_energy <= 32'sd0;
      p_avg_lut <= 32'sd0;
      p_avg_exact <= 32'sd0;
      vrms_q15 <= 16'd0;
      irms_q15 <= 16'd0;
      pf_q15 <= 16'sd0;
      energy_uwh <= 32'sd0;
      m_out <= 12'd0;
      valid_out <= 1'b0;
      p_sum_q30 <= 64'sd0;
      out_valid <= 1'b0;
    end else begin
      // s0: accumulate. win_start opens the new window WITH the sample;
      // win_end latches the old sums WITHOUT it (nonblocking reads pre-sample).
      if (ws) begin
        sum_p <= v_q15 * i_q15;
        sum_v2 <= v_q15 * v_q15;
        sum_i2 <= i_q15 * i_q15;
      end else if (sample_valid) begin
        sum_p <= sum_p + v_q15 * i_q15;
        sum_v2 <= sum_v2 + v_q15 * v_q15;
        sum_i2 <= sum_i2 + i_q15 * i_q15;
      end
      if (we) begin
        sh_p <= sum_p;
        sh_v2 <= sum_v2;
        sh_i2 <= sum_i2;
        sh_M <= M;
        sh_valid <= win_valid_in;
      end
      s1_v <= we;
      // s2: means + energy + rms magnitudes into mid regs (guarded: no
      // divide by zero; invalid windows leave stale mids, outputs forced 0).
      s2_v <= s1_v;
      if (s1_v & s1_ok) begin
        r_pavg_exact <= c_pavg_exact_64[31:0];
        r_pavg_lut <= (LUT_MEAN_ENABLE != 0) ? c_lut_prod[55:24] : 32'sd0;
        r_energy <= c_energy_64[31:0];
        r_vrms <= (c_root_v > 32'd32767) ? 16'd32767 : c_root_v[15:0];
        r_irms <= (c_root_i > 32'd32767) ? 16'd32767 : c_root_i[15:0];
      end
      // s3: PF + outputs. Invalid -> all data forced 0, out_valid pulses.
      out_valid <= s2_v;
      if (s2_v) begin
        if (sh_valid & (sh_M != 12'd0)) begin
          p_avg_lut <= r_pavg_lut;
          p_avg_exact <= r_pavg_exact;
          vrms_q15 <= r_vrms;
          irms_q15 <= r_irms;
          pf_q15 <= c_pf_sat;
          energy_uwh <= r_energy;
          m_out <= sh_M;
          p_sum_q30 <= sh_p;
          valid_out <= 1'b1;
        end else begin
          p_avg_lut <= 32'sd0;
          p_avg_exact <= 32'sd0;
          vrms_q15 <= 16'd0;
          irms_q15 <= 16'd0;
          pf_q15 <= 16'sd0;
          energy_uwh <= 32'sd0;
          m_out <= 12'd0;
          p_sum_q30 <= 64'sd0;
          valid_out <= 1'b0;
        end
      end
    end
  end

endmodule
