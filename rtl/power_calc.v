// power_calc.v — per-window fixed-point power measurement (Week 5b).
//
// Bit-exact vs tb/golden.py window_power (all fields) with the variable
// count M supplied on the M[11:0] port. Contract: power_calc TRUSTS M
// (range validation is zc_detect's job); valid windows satisfy M in
// [1810, 2230].
//
// DESCRIPTOR SOURCE (Week 4b2 decision, DECISIONS.md): p_avg_exact is the
// ONLY mean that feeds the attestation descriptor (== mean_q30_half_away,
// exactly what edge/attestation.py and tb/golden.py record_fields use, so
// the Week 5 verifier re-derives it without LUT knowledge). p_avg_lut is
// characterization only, behind parameter LUT_MEAN_ENABLE (0 ties it to 0
// so synthesis trims the ROM/multiplier in exact-only builds).
//
// Outputs (see LATENCY below): p_avg_exact/p_avg_lut (i32 Q30), vrms/irms
// (u16 Q15, root 32768 saturates to 32767), pf_q15 (i16 Q15, EXACT, 0 on
// zero denominator), energy_uwh (i32 raw signed increment; the
// record-level NEG_ENERGY clamp lives in record_agg), m_out (M, or 0 when
// invalid), p_sum_q30 (exact 64-bit record-composition sum), valid_out,
// out_valid (1-clk pulse). Outputs are meaningful ONLY with out_valid
// (registers update at their own schedule edges mid-finalization).
// Invalid window (win_valid_in = 0, or M = 0): forced through the FULL
// schedule with safe operands (magnitudes 0, denominators 1), ALL data
// outputs forced 0, valid_out = 0, out_valid still pulses — constant
// latency, no divide by zero anywhere.
//
// LATENCY (Week 5b, replaces the Week 4b2 behavioral 3-cycle note):
// iterative FSMs — div_fsm (done rises 65 edges after its start edge) and
// sqrt_fsm (done rises 33 after start) — sequenced by a linear schedule
// counter t (edges since the win_end edge E0). Exact edge map, measured
// by TB on both sims (any deviation is a bug):
//   E0    win_end sampled: shadow latch (sums/M/valid), busy<=1
//   E1    DIV_PAVG start sampled (operands: shadow)
//   E2    emult/p_avg_lut multiply regs latched (parallel, timing hygiene)
//   E66   DIV_PAVG done
//   E67   latch p_avg_exact; DIV_ENERGY start sampled (operands: emult)
//   E132  DIV_ENERGY done
//   E133  latch energy; DIV_V2 start sampled
//   E198  done; latch mean_v2
//   E199  DIV_I2 start sampled
//   E264  done; latch mean_i2
//   E265  SQRT_V start sampled (x: mean_v2 holding)
//   E298  done; latch vrms (saturate)
//   E299+1=300  SQRT_I start sampled (x: mean_i2 holding; +35 spacing:
//         the holding-latch edge, absorbed)
//   E332  done; latch irms
//   E333+1=334  DIV_PF start sampled (num: pavg<<15 wire, den: vrms*irms
//         holding, forced 1 when 0; +34 spacing)
//   E398+1=399  DIV_PF done
//   E399+1=400  latch pf/m_out/valid_out/p_sum_q30; out_valid rises;
//         busy<=0  =>  401 cycles inclusive (E0..E400), every window.
// Sqrt ops are deliberately sequenced (not overlapped with divs) for a
// trivially verifiable linear schedule; utilization stays under 3% of the
// tightest real-rate finalization budget (13,200 clocks), so overlapping
// would buy nothing (DECISIONS.md Week 5b).
// Rule: every result register latches at least one edge AFTER its
// producer's done edge (same-edge reads see pre-edge stale values — this
// exact bug class was caught during bringup on the PF path and is pinned
// by the directed suite).
//
// SHADOW + E OWNERSHIP: the shadow latch reads pre-sample accumulator
// values at the win_end edge, so window covers [S, E) — S included, E
// excluded and owned by the next window (sums reset WITH the E sample
// via win_start_pulse, exactly as Week 4b). The next window accumulates
// into sum_* concurrently with finalization (separate regs).
//
// BUSY / OVERRUN / TOMBSTONE (DECISIONS.md Week 5b): busy runs from the
// shadow latch to the out_valid edge. A win_end coincident with the
// out_valid edge latches new (idle-tie rule). A win_end while busy is a
// DROP: no shadow update (the ws/sample_valid path still attributes the E
// sample to the next window, so nothing contaminates anything); instead a
// tombstone out_valid fires 1 cycle later with all-zero data and
// valid_out = 0, and finalize_overrun[_cnt] increments per drop. The
// zeroing is a combinational mux on the output ports during the tombstone
// pulse (in-flight registers untouched, so the completing window's own
// out-latch still restores everything). Tombstones never collide with
// data completions (deferred past the completion edge; drops space >= 11
// cycles apart, deferral <= 1, so no merge is possible).
// finalize_overrun_cnt saturates (sticky 0xFFFF), clears on cnt_clear (S3
// scheme: increment wins on same-cycle clear); the sticky
// finalize_overrun flag clears on reset only. w_overrun for record_agg is
// finalize_overrun_cnt sampled at out_valid (data or tombstone alike).
//
// Arithmetic bounds (DECISIONS.md table): |sums| < 2^42, energy product
// < 2^56, PF numerator < 2^47, sqrt input <= 2^30; controller operands
// stay < 2^56, inside the div unit's |N| < 2^63 contract.
//
// No latches, no delays, Verilog-2005.
`timescale 1ns / 1ps

module power_calc #(
    parameter LUT_MEAN_ENABLE = 1, // 0: p_avg_lut ties 0 (exact-only build)
    // Overrun counter reset value, test-only override (untyped so -G widths
    // never truncate; sliced explicitly at use). Production and Yosys use
    // the default (natural saturation needs 65k drops in one record).
    parameter OVERRUN_CNT_INIT = 16'd0
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
    input  wire               cnt_clear, // sync strobe: zero overrun_cnt (flag stays)
    output wire  signed [31:0] p_avg_lut, // tomb-muxed below; valid with out_valid
    output wire  signed [31:0] p_avg_exact,
    output wire         [15:0] vrms_q15,
    output wire         [15:0] irms_q15,
    output wire  signed [15:0] pf_q15,
    output wire  signed [31:0] energy_uwh,
    output wire         [11:0] m_out,
    output wire                valid_out,
    output wire  signed [63:0] p_sum_q30,
    output reg                out_valid, // 1-clk pulse, 401 cycles after win_end
    output reg                finalize_overrun, // sticky: a window was dropped
    output reg         [15:0] finalize_overrun_cnt, // saturating drop count (telemetry)
    output wire               w_dropped // 1 on tombstone slots (record evidence)
);

  // ENERGY denominator: 9 * 2^30 (fs = 10 kHz baked in, asserted in TB).
  localparam [33:0] E_DEN = 34'd9663676416;
  localparam [11:0] LUT_BASE = 12'd1810;
  // Schedule: t = edges since the win_end edge; busy gates everything.
  // Out latch + out_valid fire at t==399 (edge E400): 401 inclusive.
  localparam [8:0] T_OUT = 9'd399;

  // Window accumulators (64-bit; 43-bit signed minimum per sub-window).
  reg signed [63:0] sum_p, sum_v2, sum_i2;
  // Shadow latch: closed-window sums + count + validity.
  reg signed [63:0] sh_p, sh_v2, sh_i2;
  reg        [11:0] sh_M;
  reg               sh_valid;
  // Schedule state.
  reg               busy;
  reg         [8:0] t;
  // Result registers (each latched >= 1 edge after its producer's done
  // edge; tomb-muxed onto the ports during tombstone pulses only).
  reg signed [31:0] r_pavg, r_energy;
  reg signed [63:0] r_mv2, r_mi2;
  reg        [15:0] r_vrms, r_irms;
  reg signed [63:0] r_pf;
  reg signed [31:0] r_lut;
  reg signed [63:0] r_emult;
  reg        [11:0] r_m;
  reg               r_valid;
  reg signed [63:0] r_psum;
  // Tombstone state (drop reporting independent of the sequencer).
  reg               tomb_pending;
  reg               tomb_firing;

  // PF numerator: r_pavg<<15 (free shift, |.| <= 2^46) and denominator.
  // Sourced from the vrms/irms/pavg holdings (all stable well before the
  // PF start edge). Declared before the port assigns (Icarus needs
  // declaration before use).
  wire signed [63:0] pf_num = $signed({{32{r_pavg[31]}}, r_pavg}) <<< 15;
  wire [31:0] pf_den = r_vrms * r_irms;
  wire pf_den_zero = (pf_den == 32'd0);

  // PF output saturation (combinational from the pf holding reg).
  wire signed [15:0] r_pf_sat = (r_pf > 64'sd32767) ? 16'sd32767 :
                                (r_pf < -64'sd32768) ? -16'sd32768 : r_pf[15:0];

  // Tombstone marker: 1 exactly on tombstone out_valid pulses (each drop
  // yields exactly one tombstone; drops space >= 11 cycles apart while
  // deferral is <= 1, so no merge is possible). record_agg counts these
  // per record — the count evidence rides atomic with its slot, unlike a
  // running snapshot, which no clear timing can keep exact under the +1
  // handoff delay (proven by the atomic-clear test attempt in bringup).
  assign w_dropped = tomb_firing;

  // Tombstone zero-mux: during the 1-cycle tombstone pulse the ports read
  // zero while every in-flight register is untouched.
  assign p_avg_lut = tomb_firing ? 32'sd0 : r_lut;
  assign p_avg_exact = tomb_firing ? 32'sd0 : r_pavg;
  assign vrms_q15 = tomb_firing ? 16'd0 : r_vrms;
  assign irms_q15 = tomb_firing ? 16'd0 : r_irms;
  assign pf_q15 = tomb_firing ? 16'sd0 :
                  (pf_den_zero ? 16'sd0 : r_pf_sat);
  assign energy_uwh = tomb_firing ? 32'sd0 : r_energy;
  assign m_out = tomb_firing ? 12'd0 : r_m;
  assign valid_out = tomb_firing ? 1'b0 : r_valid;
  assign p_sum_q30 = tomb_firing ? 64'sd0 : r_psum;

  wire we = win_end_strobe & sample_valid;
  wire ws = win_start_pulse & sample_valid;
  wire starting = we & (~busy | (busy & (t == T_OUT)));
  wire dropping = we & busy & (t != T_OUT);
  wire tomb_fire = tomb_pending & ~(busy & (t == T_OUT));
  wire ok = sh_valid & (sh_M != 12'd0);

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

  // LUT product, combinational from shadow (latched at t==1 for timing).
  // Operands widened to 88 bits first (Week 4b2 M6: defense in depth;
  // in-contract max product 2.22e16 < 2^54, invalid garbage never
  // surfaces). Out-of-range M hits the ROM default and forces zero below.
  wire [11:0] lut_sub = sh_M - LUT_BASE;
  wire [8:0] lut_idx = lut_sub[8:0];
  wire signed [87:0] lut_prod =
    $signed({{24{sh_p[63]}}, sh_p}) * $signed({63'd0, recip_lut(lut_idx)});

  // Divider operands, combinational on t (stable a full cycle before the
  // sampling edge; invalid paths force magnitude 0 / denominator 1).
  reg        div_neg;
  reg [63:0] div_mag;
  reg [63:0] div_den;
  always @* begin
    div_neg = 1'b0;
    div_mag = 64'd0;
    div_den = 64'd1;
    if (busy) begin
      case (t)
        9'd0: begin // P_AVG = round-half-away(sh_p / M)
          div_neg = ok & sh_p[63];
          div_mag = ok ? (sh_p[63] ? (~sh_p + 64'd1) : sh_p) : 64'd0;
          div_den = ok ? {52'd0, sh_M} : 64'd1;
        end
        9'd66: begin // ENERGY = round-half-away(emult / (9*2^30))
          div_neg = ok & r_emult[63];
          div_mag = ok ? (r_emult[63] ? (~r_emult + 64'd1) : r_emult) : 64'd0;
          div_den = {30'd0, E_DEN};
        end
        9'd132: begin // mean(v^2)
          div_mag = ok ? sh_v2 : 64'd0;
          div_den = ok ? {52'd0, sh_M} : 64'd1;
        end
        9'd198: begin // mean(i^2)
          div_mag = ok ? sh_i2 : 64'd0;
          div_den = ok ? {52'd0, sh_M} : 64'd1;
        end
        9'd333: begin // PF = round-half-away(pavg*2^15 / (vrms*irms))
          div_neg = ok & pf_num[63];
          div_mag = ok ? (pf_num[63] ? (~pf_num + 64'd1) : pf_num) : 64'd0;
          div_den = (ok & !pf_den_zero) ? {32'd0, pf_den} : 64'd1;
        end
        default: begin
          div_neg = 1'b0;
          div_mag = 64'd0;
          div_den = 64'd1;
        end
      endcase
    end
  end

  // Unit starts: combinational from (busy, t); sampled the next edge.
  // Start edges E1,E67,E133,E199 (div, 66 spacing), E265 (sqrt, +66),
  // E300 (sqrt, +35: holding-latch edge), E334 (div PF, +34).
  wire div_start = busy & ((t == 9'd0) | (t == 9'd66) | (t == 9'd132) |
                           (t == 9'd198) | (t == 9'd333));
  wire [63:0] sqrt_x = (t == 9'd264) ? r_mv2[63:0] :
                        (t == 9'd298) ? r_mi2[63:0] : 64'd0;
  wire sqrt_start = busy & ((t == 9'd264) | (t == 9'd298));

  wire div_done;
  wire [63:0] div_quot;
  wire sqrt_done;
  wire [32:0] sqrt_root;

  div_fsm u_div (
      .clk(clk),
      .rst_n(rst_n),
      .start(div_start),
      .neg(div_neg),
      .mag(div_mag),
      .den(div_den),
      .done(div_done), // sequenced by t; observed by TB only
      .quot(div_quot)
  );

  sqrt_fsm u_sqrt (
      .clk(clk),
      .rst_n(rst_n),
      .start(sqrt_start),
      .x(sqrt_x),
      .done(sqrt_done), // sequenced by t; observed by TB only
      .root(sqrt_root)
  );

  // Overrun counter next value: clear first, then count (increment wins
  // on a same-cycle clear+drop, uniform with the frame/uart counters).
  reg [15:0] ov_next;
  always @* begin
    ov_next = finalize_overrun_cnt;
    if (cnt_clear) ov_next = 16'd0;
    if (dropping) ov_next = (ov_next == 16'hFFFF) ? 16'hFFFF : ov_next + 16'd1;
  end

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
      busy <= 1'b0;
      t <= 9'd0;
      r_pavg <= 32'sd0;
      r_energy <= 32'sd0;
      r_mv2 <= 64'sd0;
      r_mi2 <= 64'sd0;
      r_vrms <= 16'd0;
      r_irms <= 16'd0;
      r_pf <= 64'sd0;
      r_lut <= 32'sd0;
      r_emult <= 64'sd0;
      r_m <= 12'd0;
      r_valid <= 1'b0;
      r_psum <= 64'sd0;
      tomb_pending <= 1'b0;
      tomb_firing <= 1'b0;
      out_valid <= 1'b0;
      finalize_overrun <= 1'b0;
      finalize_overrun_cnt <= OVERRUN_CNT_INIT[15:0];
    end else begin
      // Accumulate. win_start opens the new window WITH the sample
      // (this also runs on drop edges, so E is owned by the next window
      // exactly as in normal closes); win_end latches the old sums
      // WITHOUT it (nonblocking reads pre-sample). A drop skips ONLY the
      // shadow latch below, never this path.
      if (ws) begin
        sum_p <= v_q15 * i_q15;
        sum_v2 <= v_q15 * v_q15;
        sum_i2 <= i_q15 * i_q15;
      end else if (sample_valid) begin
        sum_p <= sum_p + v_q15 * i_q15;
        sum_v2 <= sum_v2 + v_q15 * v_q15;
        sum_i2 <= sum_i2 + i_q15 * i_q15;
      end
      // Window close: normal start, or overrun drop (tombstone later).
      // Priority: a win_end coincident with the completion edge restarts
      // the sequencer (latch-new wins) while the out-latch below still
      // fires for the completing window — neither is lost.
      if (starting) begin
        sh_p <= sum_p;
        sh_v2 <= sum_v2;
        sh_i2 <= sum_i2;
        sh_M <= M;
        sh_valid <= win_valid_in;
        busy <= 1'b1;
        t <= 9'd0;
      end else if (busy) begin
        t <= t + 9'd1;
        if (t == T_OUT) busy <= 1'b0;
      end
      if (dropping) begin
        tomb_pending <= 1'b1;
        finalize_overrun <= 1'b1;
      end
      finalize_overrun_cnt <= ov_next;
      // Registered multiplies (timing hygiene, zero schedule cost).
      if (busy & (t == 9'd1)) begin
        r_emult <= sh_p * 32'sd12500;
        r_lut <= (ok && (LUT_MEAN_ENABLE != 0)) ? lut_prod[55:24] : 32'sd0;
      end
      // Result registers: each latches >= 1 edge after its producer's
      // done edge (same-edge reads would see pre-edge stale values).
      // Invalid paths already forced safe operands, so latch zeros.
      if (busy & (t == 9'd66)) r_pavg <= ok ? div_quot[31:0] : 32'sd0;
      if (busy & (t == 9'd132)) r_energy <= ok ? div_quot[31:0] : 32'sd0;
      if (busy & (t == 9'd198)) r_mv2 <= ok ? $signed(div_quot) : 64'sd0;
      if (busy & (t == 9'd264)) r_mi2 <= ok ? $signed(div_quot) : 64'sd0;
      if (busy & (t == 9'd298))
        r_vrms <= !ok ? 16'd0 :
                  (sqrt_root > 33'd32767) ? 16'd32767 : sqrt_root[15:0];
      if (busy & (t == 9'd332))
        r_irms <= !ok ? 16'd0 :
                  (sqrt_root > 33'd32767) ? 16'd32767 : sqrt_root[15:0];
      // PF result: DIV_PF done rises at E399, so this latches at E400
      // (t==399) alongside m/valid/psum and out_valid below.
      if (busy & (t == 9'd399)) r_pf <= ok ? $signed(div_quot) : 64'sd0;
      // Out latch + out_valid (t==399 executes at E400) + busy already
      // cleared above; tombstone fire aligned to the same pulse shape.
      tomb_firing <= tomb_fire;
      if (tomb_fire) begin
        tomb_pending <= 1'b0;
      end
      out_valid <= (busy & (t == T_OUT)) | tomb_fire;
      if (busy & (t == T_OUT)) begin
        if (ok) begin
          // Data regs already hold exact values (latched at done+1
          // edges); refresh the record-composition set + PF saturation.
          r_m <= sh_M;
          r_valid <= 1'b1;
          r_psum <= sh_p;
        end else begin
          r_m <= 12'd0;
          r_valid <= 1'b0;
          r_psum <= 64'sd0;
        end
      end
    end
  end

endmodule
