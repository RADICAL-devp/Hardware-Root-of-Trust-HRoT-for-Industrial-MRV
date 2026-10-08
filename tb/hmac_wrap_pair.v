// hmac_wrap_pair.v — TEST-ONLY pairing of hmac_core + sha256_wrap (Week 5c).
//
// Wires the HMAC sequencer to its engine (the one-core sharing that
// top.v will own in 5d) with a TB inject mux on the wrap input and
// measurement taps re-exported at top level (no hierarchical probes):
//   inject_en = 0: wrap input comes from hmac_core (HMAC phases).
//   inject_en = 1: wrap input comes from TB (chain streams for the
//     overlap test; held-high valid for the overflow-abort test).
// Never modify for production: the mux dissolves into top.v wiring.
`timescale 1ns / 1ps

module hmac_wrap_pair #(
    parameter KEY_MAX = 160 // must match hmac_core.KEY_MAX
) (
    input  wire                   clk,
    input  wire                   rst_n,
    // HMAC job inputs (stable before start_in, held to done).
    input  wire [KEY_MAX*8-1:0]   key_data_in, // key_data_in[7:0] = key byte 0
    input  wire [7:0]             key_len_in, // 0..KEY_MAX bytes
    input  wire [7:0]             msg_data_in, // streamed message byte
    input  wire                   msg_valid_in,
    input  wire                   msg_last_in, // with final byte (or lone)
    output wire                   msg_ready_out,
    input  wire                   start_in, // pulse in IDLE (ignored when busy)
    output wire [255:0]           hmac_out, // held to next start
    output wire                   done_out, // 1-cycle pulse, clean completion
    output wire                   busy_out,
    output wire                   error_out, // sticky to start/rst
    output wire [3:0]             phase_out,
    // TB inject mux (test-only).
    input  wire                   inject_en,
    input  wire [7:0]             inject_data,
    input  wire                   inject_valid,
    input  wire                   inject_last,
    input  wire                   inject_init,
    // Measurement taps (wrap re-exports).
    output wire                   tap_accept, // core_init || core_next
    output wire                   tap_winit, // hmac w_init (pass boundaries)
    output wire                   tap_dv, // core digest flag
    output wire                   tap_ready, // wrap ready_out
    output wire                   tap_ovf, // wrap overflow_out (sticky)
    output wire [8:0]             tap_occ, // wrap occupancy_out
    output wire                   tap_wdv, // wrap digest_valid_out
    output wire [255:0]           tap_digest // wrap digest_out
);

  wire        h_init;
  wire [7:0]  h_data;
  wire        h_valid;
  wire        h_last;
  wire        w_ready;
  wire [255:0] w_digest;
  wire        w_digest_valid;
  wire        w_overflow;

  hmac_core #(
      .KEY_MAX(KEY_MAX)
  ) hmac (
      .clk(clk),
      .rst_n(rst_n),
      .key_data_in(key_data_in),
      .key_len_in(key_len_in),
      .msg_data_in(msg_data_in),
      .msg_valid_in(msg_valid_in),
      .msg_last_in(msg_last_in),
      .msg_ready_out(msg_ready_out),
      .start_in(start_in),
      .hmac_out(hmac_out),
      .done_out(done_out),
      .busy_out(busy_out),
      .error_out(error_out),
      .phase_out(phase_out),
      .w_init(h_init),
      .w_data(h_data),
      .w_valid(h_valid),
      .w_last(h_last),
      .w_ready(w_ready),
      .w_digest(w_digest),
      .w_digest_valid(w_digest_valid),
      .w_overflow(w_overflow)
  );

  wire wrap_init_in = inject_en ? inject_init : h_init;
  wire [7:0] wrap_data_in = inject_en ? inject_data : h_data;
  wire wrap_valid_in = inject_en ? inject_valid : h_valid;
  wire wrap_last_in = inject_en ? inject_last : h_last;

  wire wrap_core_init;
  wire wrap_core_next;
  wire wrap_core_dv;

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
  assign tap_winit = h_init;
  assign tap_dv = wrap_core_dv;
  assign tap_ready = w_ready;
  assign tap_ovf = w_overflow;
  assign tap_wdv = w_digest_valid;
  assign tap_digest = w_digest;

endmodule
