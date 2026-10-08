// zc_power_chain.v — TEST-ONLY integration top (Week 5b, NOT part of rtl/).
//
// Wires zc_detect -> power_calc as top.v will in a later Week 5 step, so
// detector-driven overrun (spurious crossings closing windows faster than
// the 401-cycle finalization) and recovery can be proven end to end:
// rapid spurious win_ends -> tombstones + sticky flag, then clean sine
// windows verify exactly again. Never synthesized, never shipped.
`timescale 1ns / 1ps

module zc_power_chain (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               sample_valid,
    input  wire signed [15:0] v_q15,
    input  wire signed [15:0] i_q15,
    input  wire        [31:0] counter_in,
    input  wire               cnt_clear,
    output wire               win_end_strobe,
    output wire        [11:0] win_M,
    output wire               win_valid,
    output wire signed [31:0] p_avg_exact,
    output wire        [15:0] vrms_q15,
    output wire        [15:0] irms_q15,
    output wire signed [15:0] pf_q15,
    output wire signed [31:0] energy_uwh,
    output wire        [11:0] m_out,
    output wire               valid_out,
    output wire signed [63:0] p_sum_q30,
    output wire               out_valid,
    output wire               finalize_overrun,
    output wire        [15:0] finalize_overrun_cnt
);

  wire cross_strobe_unused;
  wire win_start_pulse;

  zc_detect u_zc (
      .clk(clk),
      .rst_n(rst_n),
      .sample_valid(sample_valid),
      .v_q15(v_q15),
      .i_q15(i_q15),
      .counter_in(counter_in),
      .cross_strobe(cross_strobe_unused),
      .win_start_pulse(win_start_pulse),
      .M(win_M),
      .win_valid(win_valid),
      .win_end_strobe(win_end_strobe)
  );

  // Window strobes wire straight through, exactly as top.v will: S opens
  // the new window WITH its sample, E closes the old one WITHOUT it.

  power_calc u_pc (
      .clk(clk),
      .rst_n(rst_n),
      .sample_valid(sample_valid),
      .v_q15(v_q15),
      .i_q15(i_q15),
      .win_start_pulse(win_start_pulse),
      .win_end_strobe(win_end_strobe),
      .M(win_M),
      .win_valid_in(win_valid),
      .cnt_clear(cnt_clear),
      .p_avg_lut(),
      .p_avg_exact(p_avg_exact),
      .vrms_q15(vrms_q15),
      .irms_q15(irms_q15),
      .pf_q15(pf_q15),
      .energy_uwh(energy_uwh),
      .m_out(m_out),
      .valid_out(valid_out),
      .p_sum_q30(p_sum_q30),
      .out_valid(out_valid),
      .finalize_overrun(finalize_overrun),
      .finalize_overrun_cnt(finalize_overrun_cnt)
  );

endmodule
