// chain_wrap_pair.v — TEST-ONLY pairing of chain_ctrl + sha256_wrap (Week 5d1).
//
// Mirrors tb/hmac_wrap_pair.v: the chain sequencer drives ONE external
// wrap (the same core top.v will share with HMAC in 5d2) with a TB
// inject mux on the wrap input (rogue-producer overflow-abort coverage)
// and measurement taps re-exported at top level (no hierarchical
// probes):
//   inject_en = 0: wrap input comes from chain_ctrl (record phases).
//   inject_en = 1: wrap input comes from TB (held-high valid for the
//     overflow-abort test).
// Never modify for production: the mux dissolves into top.v wiring.
// NOTE: unlike hmac_wrap_pair there is no P_INIT masking subtlety to
// mirror — chain has a single INIT per record (same 2-cycle masking
// rationale documented in chain_ctrl.v).
`timescale 1ns / 1ps

module chain_wrap_pair (
    input  wire         clk,
    input  wire         rst_n,
    // Chain job inputs.
    input  wire         start_in, // pulse in IDLE (ignored when busy)
    input  wire         prev_sel_genesis, // 1: prev = zeros; 0: held digest
    input  wire [7:0]   sample_data_in,
    input  wire         sample_valid_in,
    input  wire         sample_last_in, // with final sample byte (or lone)
    output wire         sample_ready_out,
    input  wire [287:0] desc_data_in, // [7:0] = descriptor byte 0
    input  wire         desc_valid_in, // pulse in DESC-wait: latch + stream
    output wire [255:0] window_hash_out, // held to next start
    output wire [255:0] prev_hash_out, // chaining value for the next record
    output wire         done_out, // 1-cycle pulse, clean completion
    output wire         busy_out,
    output wire         error_out, // sticky to start/rst
    output wire [3:0]   phase_out,
    // TB inject mux (test-only: rogue-producer overflow coverage).
    input  wire         inject_en,
    input  wire [7:0]   inject_data,
    input  wire         inject_valid,
    input  wire         inject_last,
    input  wire         inject_init,
    // Measurement taps (wrap re-exports).
    output wire         tap_accept, // core_init || core_next
    output wire         tap_winit, // chain w_init (pass boundary)
    output wire         tap_dv, // core digest flag
    output wire         tap_ready, // wrap ready_out
    output wire         tap_ovf, // wrap overflow_out (sticky)
    output wire [8:0]   tap_occ, // wrap occupancy_out
    output wire         tap_wdv, // wrap digest_valid_out
    output wire [255:0] tap_digest // wrap digest_out
);

  wire        c_init;
  wire [7:0]  c_data;
  wire        c_valid;
  wire        c_last;
  wire        w_ready;
  wire [255:0] w_digest;
  wire        w_digest_valid;
  wire        w_overflow;

  chain_ctrl chain (
      .clk(clk),
      .rst_n(rst_n),
      .start_in(start_in),
      .prev_sel_genesis(prev_sel_genesis),
      .sample_data_in(sample_data_in),
      .sample_valid_in(sample_valid_in),
      .sample_last_in(sample_last_in),
      .sample_ready_out(sample_ready_out),
      .desc_data_in(desc_data_in),
      .desc_valid_in(desc_valid_in),
      .window_hash_out(window_hash_out),
      .prev_hash_out(prev_hash_out),
      .done_out(done_out),
      .busy_out(busy_out),
      .error_out(error_out),
      .phase_out(phase_out),
      .w_init(c_init),
      .w_data(c_data),
      .w_valid(c_valid),
      .w_last(c_last),
      .w_ready(w_ready),
      .w_digest(w_digest),
      .w_digest_valid(w_digest_valid),
      .w_overflow(w_overflow)
  );

  wire wrap_core_init;
  wire wrap_core_next;
  wire wrap_core_dv;

  wire        wrap_init_in = inject_en ? inject_init : c_init;
  wire [7:0]  wrap_data_in = inject_en ? inject_data : c_data;
  wire        wrap_valid_in = inject_en ? inject_valid : c_valid;
  wire        wrap_last_in = inject_en ? inject_last : c_last;

  sha256_wrap wrap (
      .clk(clk),
      .rst_n(rst_n),
      .data_in(wrap_data_in),
      .valid_in(wrap_valid_in),
      .init_in(wrap_init_in),
      .last_in(wrap_last_in),
      .ready_out(w_ready),
      .digest_out(w_digest),
      .digest_valid_out(w_digest_valid),
      .busy_out(),
      .overflow_out(w_overflow),
      .occupancy_out(tap_occ),
      .core_init(wrap_core_init),
      .core_next(wrap_core_next),
      .core_digest_valid(wrap_core_dv)
  );

  assign tap_accept = wrap_core_init || wrap_core_next;
  assign tap_winit = c_init;
  assign tap_dv = wrap_core_dv;
  assign tap_ready = w_ready;
  assign tap_ovf = w_overflow;
  assign tap_wdv = w_digest_valid;
  assign tap_digest = w_digest;

endmodule
