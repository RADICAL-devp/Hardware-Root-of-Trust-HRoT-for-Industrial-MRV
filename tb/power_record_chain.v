// power_record_chain.v — TEST-ONLY integration top (Week 5b, NOT part of rtl/).
//
// Wires power_calc -> record_agg as top.v will in a later Week 5 step
// (w_done = out_valid, per-window results straight through, running
// overrun snapshot alongside), so record-level overrun carriage (invalid
// tombstone slots, rec_overrun_cnt, flags bit 6) is proven end to end
// against record_fields/build_record. energy_prev_in is a top input
// (driven per record by the TB; top.v will chain rec_energy back).
// cnt_clear fans to power_calc (record_agg needs none: it takes the 5th
// slot's snapshot). Never synthesized, never shipped.
`timescale 1ns / 1ps

module power_record_chain (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               sample_valid,
    input  wire signed [15:0] v_q15,
    input  wire signed [15:0] i_q15,
    input  wire               win_start_pulse,
    input  wire               win_end_strobe,
    input  wire        [11:0] M,
    input  wire               win_valid_in,
    input  wire               cnt_clear,
    input  wire        [63:0] energy_prev_in,
    output wire               out_valid,
    output wire               valid_out,
    output wire        [11:0] m_out,
    output wire signed [31:0] p_avg_exact,
    output wire signed [31:0] p_avg_lut,
    output wire        [15:0] vrms_q15,
    output wire        [15:0] irms_q15,
    output wire signed [15:0] pf_q15,
    output wire signed [31:0] energy_uwh,
    output wire signed [63:0] p_sum_q30,
    output wire               finalize_overrun,
    output wire        [15:0] finalize_overrun_cnt,
    output wire        [63:0] rec_energy,
    output wire signed [63:0] rec_p_sum,
    output wire        [13:0] rec_m_total,
    output wire        [59:0] rec_zc,
    output wire         [7:0] rec_flags,
    output wire         [7:0] rec_overrun_cnt,
    output wire               rec_done
);

  wire signed [31:0] pc_energy;
  wire signed [63:0] pc_psum;

  power_calc u_pc (
      .clk(clk),
      .rst_n(rst_n),
      .sample_valid(sample_valid),
      .v_q15(v_q15),
      .i_q15(i_q15),
      .win_start_pulse(win_start_pulse),
      .win_end_strobe(win_end_strobe),
      .M(M),
      .win_valid_in(win_valid_in),
      .cnt_clear(cnt_clear),
      .p_avg_lut(p_avg_lut),
      .p_avg_exact(p_avg_exact),
      .vrms_q15(vrms_q15),
      .irms_q15(irms_q15),
      .pf_q15(pf_q15),
      .energy_uwh(pc_energy),
      .m_out(m_out),
      .valid_out(valid_out),
      .p_sum_q30(pc_psum),
      .out_valid(out_valid),
      .finalize_overrun(finalize_overrun),
      .finalize_overrun_cnt(finalize_overrun_cnt)
  );

  assign energy_uwh = pc_energy;
  assign p_sum_q30 = pc_psum;

  record_agg u_ra (
      .clk(clk),
      .rst_n(rst_n),
      .w_done(out_valid),
      .w_energy(pc_energy),
      .w_p_sum(pc_psum),
      .w_m(m_out),
      .w_ok(valid_out),
      .w_overrun_cnt(finalize_overrun_cnt),
      .energy_prev_in(energy_prev_in),
      .rec_energy(rec_energy),
      .rec_p_sum(rec_p_sum),
      .rec_m_total(rec_m_total),
      .rec_zc(rec_zc),
      .rec_flags(rec_flags),
      .rec_overrun_cnt(rec_overrun_cnt),
      .rec_done(rec_done)
  );

endmodule
