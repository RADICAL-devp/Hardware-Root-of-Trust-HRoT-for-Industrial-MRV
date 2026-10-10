// top.v — integrated attestation core (Week 5d2).
//
// Datapath: uart_rx -> frame_rx -> zc_detect -> power_calc -> record_agg
// (existing units, untouched) with a sample tap at the frame_rx output:
// P_inst = V x I (16x16->32 exact combinational) feeds V||I||P (8 B
// atomic units, little-endian) through a 16-deep skid FIFO into
// chain_ctrl, which streams prev -> samples live. At record close the
// descriptor latches (counter, window_start, zc, flags, overrun,
// device, p_avg via div_fsm, energy), chain finishes -> window_hash,
// then hmac_core runs descriptor||hash through the SAME sha256_wrap
// (time-multiplexed mux, select only at job boundaries) -> record out.
// The secure element signs off-chip (ledger/writer.py, 5d3); keys come
// ONLY from sim/provision.py (B2: write-once port below).
//
// Production parameters (B1/B3): CLK_PER_BIT=8 (12 MHz / 1.5 Mbaud),
// LUT_MEAN_ENABLE=0 (exact-only), DEVICE_ID (default 1), SKID_DEPTH
// (default 16; TB overflow test overrides via parameters=). No test-only
// counter hooks exist here (no FERR/OVERRUN/CNT_INIT — repo-checked).
// Clear domain is PER RECORD (amendment 7): sidecar latches at close,
// then ONE shared cnt_clear strobe fans to uart/frame/power
// (increment-wins same-cycle in each unit); saturated reads as LOWER
// BOUND; sidecar entries carry the record counter.
// Skid policy (amendment 5): 17th in-window byte sets STICKY overflow,
// is never hashed, and forces window_flags bit 7 HASH_OVERFLOW with the
// record INVALIDATED (valid bits cleared, p_avg 0, energy excluded).
// Counted in the sidecar too. Unreachable at wire rate (margin-proven);
// adversarial bursts only.
// Counter halt: win_counter halts at 2^32-1; top SUPPRESSES further
// record_valid (fail-closed against nonce reuse).
// Mux rule (amendment 6): select changes only at job boundaries;
// each master sees digest_valid/ready/overflow gated by selection.
// Boundary ownership (amendment 4): the record-closing E sample is
// owned by the NEXT record ([S,E) rule) — boundary_hold keeps E bytes
// in skid for the next chain job; the reference L0 tap for verifiers
// is the CHAIN INPUT (post-skid), so skid drops never exist downstream
// and bit 7 is their only (attested) evidence.
// No latches, Verilog-2005, rst_n async-assert / sync-release.
`timescale 1ns / 1ps

module top #(
    parameter DEVICE_ID = 32'd1, // whitelisted (TB device tests; Yosys default)
    parameter SKID_DEPTH = 16 // whitelisted (TB overflow test; Yosys default; unsized: -G safe)
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         rx, // UART serial line (1.5 Mbaud at 12 MHz)
    input  wire [255:0] hmac_key_data_in, // [7:0] = key byte 0 (provisioning)
    input  wire         hmac_key_valid_in, // latch pulse (write-once, see below)
    // Attested record outputs (valid with record_valid_out).
    output reg          record_valid_out, // 1-clk pulse, clean record
    output reg  [31:0]  record_counter, // == win_counter at close (pre-incr)
    output reg  [31:0]  record_window_start, // L0 counter of first sample
    output reg  [59:0]  record_zc, // five u12 M values (arrival order)
    output reg  [7:0]   record_flags, // bits k valid; 5 NEG; 6 OVERRUN; 7 HASH_OVF
    output reg  [7:0]   record_overrun_cnt, // u8 drop count (bit 6 iff > 0)
    output reg  signed [31:0] record_p_avg, // i32 Q30 exact mean (0 if m == 0)
    output reg  [63:0]  record_energy, // u64 cumulative measured uWh
    output reg  [255:0] record_hash, // window_hash (chain digest)
    output reg  [255:0] record_hmac, // HMAC-SHA256(descriptor || hash)
    output wire         halted_out, // counter halted at MAX (records suppressed)
    // Status.
    output reg          skid_overflow_out, // STICKY skid overflow (consumed at assembly)
    output reg          error_out, // STICKY: unprovisioned close / sub-error
    output wire         hmac_provisioned_out, // key latched (protocol precondition)
    output wire         busy_out,
    // Unsigned telemetry sidecar (NOT attested — writer labels as such).
    output reg  [15:0]  sc_crc_err_cnt,
    output reg  [15:0]  sc_resync_cnt,
    output reg  [15:0]  sc_ferr_cnt,
    output reg  [15:0]  sc_skid_ovf_cnt, // saturating skid-overflow event count
    output reg          sc_saturated, // any health counter saturated: lower bound
    output reg  [31:0]  sc_counter, // record counter this sidecar belongs to
    // Computed-exact window outputs (NOT separately attested — the
    // descriptor carries only the record mean/energy; these feed
    // MPC/baseline downstream per the Week 4a3 PF contract).
    output reg          window_out_valid, // 1-cyc per closed window (incl tombstones)
    output reg  [15:0]  window_vrms, // u16 Q15 (0 on invalid/tombstone)
    output reg  [15:0]  window_irms, // u16 Q15 (0 on invalid/tombstone)
    output reg  signed [15:0] window_pf, // i16 Q15 exact (0 on invalid/tombstone)
    output reg  [11:0]  window_m, // M (0 on invalid/tombstone)
    output reg          window_ok, // valid_out at close (0 on invalid/tombstone)
    // Debug taps (plain wires; synthesis keeps them, infers nothing).
    output wire [7:0]   tap_sample_byte, // skid-bound tap byte (P_inst test)
    output wire         tap_sample_valid, // 1-cyc per pushed tap byte
    output wire         tap_rec_done, // record_agg close pulse
    output wire         tap_win_end, // zc window-close strobe
    output wire [4:0]   tap_skid_occ, // skid occupancy 0..16 (margin telemetry)
    output wire [4:0]   tap_skid_peak, // STICKY max occupancy (reset clears)
    output wire [31:0]  tap_stamp_winend5, // cyc of 5th win_end (span derivation)
    output wire [31:0]  tap_stamp_recdone, // cyc of rec_done (span derivation)
    output wire [31:0]  tap_stamp_valid, // cyc of record_valid (span derivation)
    output wire [3:0]   tap_phase // top sequencer phase
);

  // Top sequencer phases.
  localparam T_IDLE = 4'd0; // no accumulation yet / post-error
  localparam T_ACCUM = 4'd1; // windows accumulating (chain SAMP open)
  localparam T_DIVWAIT = 4'd2; // rec closed: sidecar latched, p_avg dividing
  localparam T_ASSEMBLE = 4'd3; // descriptor formed
  localparam T_CHAINWAIT = 4'd4; // chain hashing tail (desc fed)
  localparam T_HMACWAIT = 4'd5; // chain done sampled; HMAC starts
  localparam T_HMAC = 4'd6; // HMAC over descriptor || hash
  localparam T_OUT = 4'd7; // publish record

  localparam CHAIN_SEL = 1'b0;
  localparam HMAC_SEL = 1'b1;

  // Chain phase code for SAMP (boundary lone-last gate below).
  localparam CHAIN_SAMP = 4'd3;

  reg [3:0] phase;
  assign tap_phase = phase;

  // ---- front-end units (defaults except whitelisted production values) ----
  wire [7:0]  uart_byte;
  wire        uart_dv;
  wire        uart_ferr;
  wire [15:0] ferr_cnt;
  reg         cnt_clear_q; // shared consume-then-clear strobe (per record)

  uart_rx #(
      .CLK_PER_BIT(8) // production: 12 MHz / 1.5 Mbaud (B1)
  ) u_uart (
      .clk(clk),
      .rst_n(rst_n),
      .rx(rx),
      .cnt_clear(cnt_clear_q),
      .data(uart_byte),
      .data_valid(uart_dv),
      .framing_error(uart_ferr),
      .framing_err_cnt(ferr_cnt)
  );

  wire [31:0] frame_counter;
  wire [15:0] frame_v;
  wire [15:0] frame_i;
  wire        frame_valid;
  wire        crc_err;
  wire [15:0] crc_err_cnt;
  wire [15:0] resync_cnt;
  wire        frame_sat;

  frame_rx u_frame (
      .clk(clk),
      .rst_n(rst_n),
      .data_in(uart_byte),
      .data_valid(uart_dv),
      .cnt_clear(cnt_clear_q),
      .frame_counter(frame_counter),
      .frame_v(frame_v),
      .frame_i(frame_i),
      .frame_valid(frame_valid),
      .crc_err(crc_err),
      .crc_err_cnt(crc_err_cnt),
      .resync_cnt(resync_cnt),
      .cnt_saturated(frame_sat)
  );

  // Sample fanout (bit patterns reinterpreted signed; counter verbatim).
  wire               sample_valid = frame_valid;
  wire signed [15:0] sample_v = $signed(frame_v);
  wire signed [15:0] sample_i = $signed(frame_i);
  wire [31:0]        sample_ctr = frame_counter;

  wire        win_start_pulse;
  wire [11:0] zc_M;
  wire        win_valid;
  wire        win_end_strobe;

  zc_detect u_zc (
      .clk(clk),
      .rst_n(rst_n),
      .sample_valid(sample_valid),
      .v_q15(sample_v),
      .i_q15(sample_i),
      .counter_in(sample_ctr),
      .cross_strobe(),
      .win_start_pulse(win_start_pulse),
      .M(zc_M),
      .win_valid(win_valid),
      .win_end_strobe(win_end_strobe)
  );
  assign tap_win_end = win_end_strobe;

  wire signed [31:0] pc_p_avg_exact;
  wire [15:0]        pc_vrms;
  wire [15:0]        pc_irms;
  wire signed [15:0] pc_pf;
  wire signed [31:0] pc_energy;
  wire [11:0]        pc_m;
  wire               pc_valid;
  wire signed [63:0] pc_p_sum;
  wire               pc_out_valid;
  wire               pc_overrun;
  wire [15:0]        pc_overrun_cnt;
  wire               pc_dropped;

  power_calc #(
      .LUT_MEAN_ENABLE(0) // exact-only build (whitelisted)
  ) u_power (
      .clk(clk),
      .rst_n(rst_n),
      .sample_valid(sample_valid),
      .v_q15(sample_v),
      .i_q15(sample_i),
      .win_start_pulse(win_start_pulse),
      .win_end_strobe(win_end_strobe),
      .M(zc_M),
      .win_valid_in(win_valid),
      .cnt_clear(cnt_clear_q),
      .p_avg_lut(),
      .p_avg_exact(pc_p_avg_exact),
      .vrms_q15(pc_vrms),
      .irms_q15(pc_irms),
      .pf_q15(pc_pf),
      .energy_uwh(pc_energy),
      .m_out(pc_m),
      .valid_out(pc_valid),
      .p_sum_q30(pc_p_sum),
      .out_valid(pc_out_valid),
      .finalize_overrun(pc_overrun),
      .finalize_overrun_cnt(pc_overrun_cnt),
      .w_dropped(pc_dropped)
  );

  // Record aggregation (energy feedback held stable across the record).
  reg [63:0] energy_q; // previous record energy (0 at reset)
  wire [63:0] rec_energy_w;
  wire signed [63:0] rec_p_sum_w;
  wire [13:0] rec_m_total_w;
  wire [59:0] rec_zc_w;
  wire [7:0]  rec_flags_w;
  wire [7:0]  rec_overrun_cnt_w;
  wire        rec_done_w;
  assign tap_rec_done = rec_done_w;

  record_agg u_rec (
      .clk(clk),
      .rst_n(rst_n),
      .w_done(pc_out_valid),
      .w_energy(pc_energy),
      .w_p_sum(pc_p_sum),
      .w_m(pc_m),
      .w_ok(pc_valid),
      .w_dropped(pc_dropped),
      .energy_prev_in(energy_q),
      .rec_energy(rec_energy_w),
      .rec_p_sum(rec_p_sum_w),
      .rec_m_total(rec_m_total_w),
      .rec_zc(rec_zc_w),
      .rec_flags(rec_flags_w),
      .rec_overrun_cnt(rec_overrun_cnt_w),
      .rec_done(rec_done_w)
  );

  // Record P_AVG division (exact half-away; 0 when m == 0).
  reg        div_start_q;
  wire       div_done_w;
  wire [63:0] div_quot_w;
  reg signed [63:0] div_p_sum_q;
  reg [13:0] div_m_q;

  div_fsm u_divpavg (
      .clk(clk),
      .rst_n(rst_n),
      .start(div_start_q),
      .neg(div_p_sum_q[63]),
      .mag(div_p_sum_q[63] ? -div_p_sum_q : div_p_sum_q),
      .den({50'd0, div_m_q}),
      .done(div_done_w),
      .quot(div_quot_w)
  );

  // Monotonic counter (defaults: starts 0, halts at MAX — B3).
  wire [31:0] cnt_out_w;
  wire        cnt_halted_w;
  reg         cnt_incr_q;
  assign halted_out = cnt_halted_w;

  win_counter u_cnt (
      .clk(clk),
      .rst_n(rst_n),
      .incr_in(cnt_incr_q),
      .close_valid_in(1'b1), // D-02: closed records advance, even invalid-flagged
      .count_out(cnt_out_w),
      .halted_out(cnt_halted_w)
  );

  // HMAC key register (write-once provisioning, B2).
  reg [255:0] key_q;
  reg         provisioned_q;
  assign hmac_provisioned_out = provisioned_q;

  // Sample tap: P_inst exact combinational multiplier + 8-byte serializer.
  wire signed [31:0] p_inst_w = sample_v * sample_i; // exact 16x16->32
  reg [15:0] ser_v;
  reg [15:0] ser_i;
  reg [31:0] ser_p;
  reg [3:0]  ser_idx; // next tap byte 0..7; 8 == idle
  reg        ser_busy;
  reg [7:0]  ser_byte;
  reg        ser_push; // 1-cyc skid push strobe
  reg [7:0]  tap_byte_q; // registered tap byte: consistent with ser_push
  // post-edge (a combinational byte would show the NEXT index after the
  // push edge advances ser_idx — the week5b same-edge-stale class).
  reg [7:0]  push_data_q; // registered skid write data (SAME coherence
  // rule as the tap: skid samples wr_data an edge after the push edge,
  // so combinational ser_byte would deliver byte[idx+1] (systematic +1
  // skew) plus a stale duplicate at frame end. Found by e2e_small.)
  wire       tap_overrun_w = frame_valid && ser_busy; // wire-rate impossible

  always @* begin
    case (ser_idx[2:0])
      3'd0: ser_byte = ser_v[7:0];
      3'd1: ser_byte = ser_v[15:8];
      3'd2: ser_byte = ser_i[7:0];
      3'd3: ser_byte = ser_i[15:8];
      3'd4: ser_byte = ser_p[7:0];
      3'd5: ser_byte = ser_p[15:8];
      3'd6: ser_byte = ser_p[23:16];
      default: ser_byte = ser_p[31:24];
    endcase
  end
  assign tap_sample_byte = tap_byte_q;
  assign tap_sample_valid = ser_push;

  // Skid FIFO (production, defaults: depth 16).
  wire [7:0] skid_rd_data;
  wire       skid_rd_valid;
  reg        skid_rd_ready;
  wire       skid_wr_ready;
  wire       skid_ovf_w;
  wire [4:0] skid_occ_w;
  wire [15:0] skid_drop_cnt_w; // unit-side saturating dropped-byte count
  wire       skid_consume_w; // 1-edge: descriptor assembly consumed the flag
  assign tap_skid_occ = skid_occ_w;

  skid_buf #(
      .DEPTH(SKID_DEPTH) // production 16 (whitelisted test hook)
  ) u_skid (
      .clk(clk),
      .rst_n(rst_n),
      .wr_data(push_data_q),
      .wr_valid(ser_push),
      .wr_ready(skid_wr_ready),
      .rd_data(skid_rd_data),
      .rd_valid(skid_rd_valid),
      .rd_ready(skid_rd_ready),
      .overflow_out(skid_ovf_w),
      .drop_cnt_out(skid_drop_cnt_w),
      .flag_consume(skid_consume_w),
      .cnt_clear(cnt_clear_q),
      .occ_out(skid_occ_w)
  );

  // Boundary hold: E bytes wait in skid for the NEXT chain job (amend 4).
  reg boundary_hold_q;

  // Shared wrap wires (declared before the masters that use them —
  // Icarus rejects use-before-declare; job-boundary mux per amend 6).
  reg wrap_sel_q; // 0 chain, 1 HMAC
  wire wrap_ready_w;
  wire [255:0] wrap_digest_w;
  wire wrap_dv_w;
  wire wrap_ovf_w;

  // Chain sequencer (wrap master A).
  reg        chain_start_q;
  reg        chain_genesis_q;
  reg        chain_samp_last_q; // lone-last pulse (record-boundary SAMP_END)
  reg [287:0] chain_desc_q;
  reg        chain_desc_valid_q;
  wire [255:0] chain_hash_w;
  wire [255:0] chain_prev_w;
  wire        chain_done_w;
  wire        chain_busy_w;
  wire        chain_err_w;
  wire [3:0]  chain_phase_w;
  wire        chain_samp_ready_w;
  wire        chain_w_init;
  wire [7:0]  chain_w_data;
  wire        chain_w_valid;
  wire        chain_w_last;
  wire        chain_w_ready_g;
  wire        chain_w_dv_g;
  wire        chain_w_ovf_g;

  chain_ctrl u_chain (
      .clk(clk),
      .rst_n(rst_n),
      .start_in(chain_start_q),
      .prev_sel_genesis(chain_genesis_q),
      .sample_data_in(skid_rd_data),
      .sample_valid_in(skid_rd_valid && !boundary_hold_q),
      .sample_last_in(chain_samp_last_q),
      .sample_ready_out(chain_samp_ready_w),
      .desc_data_in(chain_desc_q),
      .desc_valid_in(chain_desc_valid_q),
      .window_hash_out(chain_hash_w),
      .prev_hash_out(chain_prev_w),
      .done_out(chain_done_w),
      .busy_out(chain_busy_w),
      .error_out(chain_err_w),
      .phase_out(chain_phase_w),
      .w_init(chain_w_init),
      .w_data(chain_w_data),
      .w_valid(chain_w_valid),
      .w_last(chain_w_last),
      .w_ready(chain_w_ready_g),
      .w_digest(wrap_digest_w),
      .w_digest_valid(chain_w_dv_g),
      .w_overflow(chain_w_ovf_g)
  );
  assign skid_rd_ready = chain_samp_ready_w && !boundary_hold_q;

  // HMAC sequencer (wrap master B): 32 B provisioned key, 68 B message.
  reg        hmac_start_q;
  reg [7:0]  hmac_msg_data_q;
  reg        hmac_msg_valid_q;
  reg        hmac_msg_last_q;
  wire       hmac_msg_ready_w;
  wire [255:0] hmac_tag_w;
  wire        hmac_done_w;
  wire        hmac_busy_w;
  wire        hmac_err_w;
  wire        hmac_w_init;
  wire [7:0]  hmac_w_data;
  wire        hmac_w_valid;
  wire        hmac_w_last;
  wire        hmac_w_ready_g;
  wire        hmac_w_dv_g;
  wire        hmac_w_ovf_g;

  hmac_core u_hmac (
      .clk(clk),
      .rst_n(rst_n),
      .key_data_in({1024'd0, key_q}), // 32 B key, zero-extended to KEY_MAX port
      .key_len_in(8'd32), // provisioned keys are 32 B (B2)
      .msg_data_in(hmac_msg_data_q),
      .msg_valid_in(hmac_msg_valid_q),
      .msg_last_in(hmac_msg_last_q),
      .msg_ready_out(hmac_msg_ready_w),
      .start_in(hmac_start_q),
      .hmac_out(hmac_tag_w),
      .done_out(hmac_done_w),
      .busy_out(hmac_busy_w),
      .error_out(hmac_err_w),
      .phase_out(),
      .w_init(hmac_w_init),
      .w_data(hmac_w_data),
      .w_valid(hmac_w_valid),
      .w_last(hmac_w_last),
      .w_ready(hmac_w_ready_g),
      .w_digest(wrap_digest_w),
      .w_digest_valid(hmac_w_dv_g),
      .w_overflow(hmac_w_ovf_g)
  );

  // Shared wrap instance (wires declared above, beside chain_ctrl).
  sha256_wrap u_wrap (
      .clk(clk),
      .rst_n(rst_n),
      .data_in(wrap_sel_q ? hmac_w_data : chain_w_data),
      .valid_in(wrap_sel_q ? hmac_w_valid : chain_w_valid),
      .init_in(wrap_sel_q ? hmac_w_init : chain_w_init),
      .last_in(wrap_sel_q ? hmac_w_last : chain_w_last),
      .ready_out(wrap_ready_w),
      .digest_out(wrap_digest_w),
      .digest_valid_out(wrap_dv_w),
      .busy_out(),
      .overflow_out(wrap_ovf_w),
      .occupancy_out(),
      .core_init(),
      .core_next(),
      .core_digest_valid()
  );

  // Per-master gated views: each master sees ONLY its own digest_valid
  // (else chain could latch HMAC's digest — the M-H6 phantom class).
  assign chain_w_ready_g = (wrap_sel_q == CHAIN_SEL) ? wrap_ready_w : 1'b0;
  assign chain_w_dv_g = (wrap_sel_q == CHAIN_SEL) ? wrap_dv_w : 1'b0;
  assign chain_w_ovf_g = (wrap_sel_q == CHAIN_SEL) ? wrap_ovf_w : 1'b0;
  assign hmac_w_ready_g = (wrap_sel_q == HMAC_SEL) ? wrap_ready_w : 1'b0;
  assign hmac_w_dv_g = (wrap_sel_q == HMAC_SEL) ? wrap_dv_w : 1'b0;
  assign hmac_w_ovf_g = (wrap_sel_q == HMAC_SEL) ? wrap_ovf_w : 1'b0;

  // Record bookkeeping.
  reg [2:0]  win_end_cnt; // window closes this record (boundary at 4)
  reg        first_start_done; // record-0 window_start captured
  // Next chain job wanted. Reset value 1: the genesis job autostarts at
  // reset release so sample 0 streams live (a pre-first-start preamble
  // bigger than 2 samples would otherwise overflow the 16-deep skid and
  // force the first record invalid — found by the e2e chunking probe).
  // Later jobs arm at their record boundary E (trailing win_start).
  reg        need_chain_start;
  reg        first_record_q; // genesis one-shot (no digest yet)
  reg        hash_valid_q; // a digest exists (prev available for sel=0)
  reg [31:0] window_start_q; // L0 counter of the record's first sample
  // Staged copy for outputs: window_start_q is reloaded by the NEXT
  // chain job start (~E+1100, post-HMAC), which can precede this
  // record's T_OUT — outputs must use the close-time value (same
  // staging discipline as flags/p_avg/energy/counter).
  reg [31:0] desc_wstart_q;
  reg [31:0] window_start_next_q; // boundary-captured S of the next record
  reg [7:0]  desc_flags_q; // assembled flags (rec | bit7, possibly cleared)
  reg [31:0] desc_p_avg_q; // assembled P_AVG (0 when forced-invalid/m==0)
  reg [63:0] desc_energy_q; // assembled energy (prev when forced-invalid)
  reg [7:0]  desc_overrun_q; // assembled overrun count
  reg [7:0]  desc_tmp_flags; // blocking temp: flag assembly (no multi-assign)
  reg        div_started_q; // div in flight (single declaration)
  reg [6:0]  hmac_idx_q; // HMAC message byte 0..67 (7 bits: 67 > 63)
  reg        hmac_feeding_q; // message streaming active
  reg [7:0]  hash_b [0:31]; // latched window_hash bytes, message order
  reg [7:0]  desc_b [0:35]; // latched descriptor bytes for HMAC feed
  reg [255:0] hash_q; // latched window_hash for output
  reg        cnt_clear_armed; // consume-then-clear second phase
  // Measurement telemetry: free-running cycle counter + sticky event
  // stamps + skid peak. TB polls these (levels, miss-proof) instead of
  // sampling every edge — an 8.8M-clock record costs ~1M Python↔sim
  // interactions per edge monitored; polled levels cost dozens.
  reg [31:0] cyc_q;
  reg [4:0]  skid_peak_q;
  reg [31:0] stamp_winend5_q;
  reg [31:0] stamp_recdone_q;
  reg [31:0] stamp_valid_q;
  assign tap_skid_peak = skid_peak_q;
  assign tap_stamp_winend5 = stamp_winend5_q;
  assign tap_stamp_recdone = stamp_recdone_q;
  assign tap_stamp_valid = stamp_valid_q;
  integer j;

  // Descriptor bytes (36 B, Week-3 layout) assembled at close.
  reg [7:0] desc_mem [0:35];

  assign busy_out = (phase != T_IDLE) || chain_busy_w || hmac_busy_w || div_started_q;

  // Consume handshake: exactly the assembly edge (phase leaves DIVWAIT
  // the same edge, so this is a 1-edge pulse by construction).
  assign skid_consume_w = (phase == T_DIVWAIT) && (!div_started_q || div_done_w)
       && skid_overflow_out;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      phase <= T_IDLE;
      cnt_clear_q <= 1'b0;
      cnt_clear_armed <= 1'b0;
      energy_q <= 64'd0;
      win_end_cnt <= 3'd0;
      first_start_done <= 1'b0;
      need_chain_start <= 1'b1; // autostart genesis job (see decl note)
      first_record_q <= 1'b1;
      hash_valid_q <= 1'b0;
      window_start_q <= 32'd0;
      window_start_next_q <= 32'd0;
      boundary_hold_q <= 1'b0;
      provisioned_q <= 1'b0;
      key_q <= 256'd0;
      chain_start_q <= 1'b0;
      chain_genesis_q <= 1'b0;
      chain_samp_last_q <= 1'b0;
      chain_desc_valid_q <= 1'b0;
      chain_desc_q <= 288'd0;
      hmac_start_q <= 1'b0;
      hmac_msg_data_q <= 8'd0;
      hmac_msg_valid_q <= 1'b0;
      hmac_msg_last_q <= 1'b0;
              hmac_idx_q <= 7'd0;
      hmac_feeding_q <= 1'b0;
      hash_q <= 256'd0;
      for (j = 0; j < 32; j = j + 1) hash_b[j] <= 8'd0;
      for (j = 0; j < 36; j = j + 1) desc_b[j] <= 8'd0;
      wrap_sel_q <= CHAIN_SEL;
      div_start_q <= 1'b0;
      div_started_q <= 1'b0;
      div_p_sum_q <= 64'sd0;
      div_m_q <= 14'd0;
      desc_flags_q <= 8'd0;
      desc_wstart_q <= 32'd0;
      desc_p_avg_q <= 32'd0;
      desc_energy_q <= 64'd0;
      desc_overrun_q <= 8'd0;
      desc_tmp_flags <= 8'd0;
      for (j = 0; j < 36; j = j + 1) desc_mem[j] <= 8'd0;
      ser_v <= 16'd0;
      ser_i <= 16'd0;
      ser_p <= 32'd0;
      ser_idx <= 4'd8;
      ser_busy <= 1'b0;
      ser_push <= 1'b0;
      tap_byte_q <= 8'd0;
      push_data_q <= 8'd0;
      skid_overflow_out <= 1'b0;
      cnt_incr_q <= 1'b0;
      record_valid_out <= 1'b0;
      record_counter <= 32'd0;
      record_window_start <= 32'd0;
      record_zc <= 60'd0;
      record_flags <= 8'd0;
      record_overrun_cnt <= 8'd0;
      record_p_avg <= 32'sd0;
      record_energy <= 64'd0;
      record_hash <= 256'd0;
      record_hmac <= 256'd0;
      error_out <= 1'b0;
      window_out_valid <= 1'b0;
      window_vrms <= 16'd0;
      window_irms <= 16'd0;
      window_pf <= 16'sd0;
      window_m <= 12'd0;
      window_ok <= 1'b0;
      sc_crc_err_cnt <= 16'd0;
      sc_resync_cnt <= 16'd0;
      sc_ferr_cnt <= 16'd0;
      sc_skid_ovf_cnt <= 16'd0;
      sc_saturated <= 1'b0;
      sc_counter <= 32'd0;
      cyc_q <= 32'd0;
      skid_peak_q <= 5'd0;
      stamp_winend5_q <= 32'd0;
      stamp_recdone_q <= 32'd0;
      stamp_valid_q <= 32'd0;
    end else begin
      // Single-cycle pulses default low (incl. cnt_clear: never held).
      record_valid_out <= 1'b0;
      cnt_clear_q <= 1'b0;
      cnt_incr_q <= 1'b0;
      window_out_valid <= 1'b0;
      chain_start_q <= 1'b0;
      chain_samp_last_q <= 1'b0;
      chain_desc_valid_q <= 1'b0;
      hmac_start_q <= 1'b0;
      div_start_q <= 1'b0;
      ser_push <= 1'b0;
      cyc_q <= cyc_q + 32'd1;
      if (skid_occ_w > skid_peak_q) skid_peak_q <= skid_occ_w;

      // Window streaming outputs (sampled with power's close pulse,
      // same edge record_agg uses — no atomicity hazard by construction).
      if (pc_out_valid) begin
        window_out_valid <= 1'b1;
        window_vrms <= pc_vrms;
        window_irms <= pc_irms;
        window_pf <= pc_pf;
        window_m <= pc_m;
        window_ok <= pc_valid;
      end

      // Write-once key provisioning (B2): the FIRST pulse wins whenever
      // it arrives (even mid-record — chain autostarts at reset, so an
      // idle gate would deadlock provisioning against the running chain;
      // found by the e2e_small bringup). Later pulses are ignored, so a
      // provisioned job can never mix keys; TB provisions before traffic
      // (protocol precondition asserted in test).
      if (hmac_key_valid_in && !provisioned_q) begin
        key_q <= hmac_key_data_in;
        provisioned_q <= 1'b1;
        error_out <= 1'b0; // operator intervention clears a prior error
      end

      // Tap serializer: latch frame fields, shift 8 bytes while skid ready.
      if (tap_overrun_w) begin
        // Wire-rate impossible (880 >> 8); fail loud via the skid flag
        // (no count: the unit counts skid drops; this site is unreachable).
        skid_overflow_out <= 1'b1;
      end else if (frame_valid && !ser_busy) begin
        ser_v <= frame_v;
        ser_i <= frame_i;
        ser_p <= $signed(frame_v) * $signed(frame_i);
        ser_idx <= 4'd0;
        ser_busy <= 1'b1;
      end else if (ser_busy) begin
        if (skid_wr_ready) begin
          ser_push <= 1'b1;
          push_data_q <= ser_byte;
          tap_byte_q <= ser_byte;
          if (ser_idx == 4'd7) begin
            ser_idx <= 4'd8;
            ser_busy <= 1'b0;
          end else ser_idx <= ser_idx + 4'd1;
        end
      end

      // Skid overflow flag (sticky to descriptor assembly; the unit
      // clears on consume, so no latch-up: every record re-triggers on
      // new drops only. Unit-side drop count latched to the sidecar.)
      if (skid_ovf_w) begin
        skid_overflow_out <= 1'b1;
      end

      // Record-boundary E split (amendment 4): 5th win_end holds E bytes
      // in skid for the NEXT chain job; trailing S captured for window_start.
      if (win_end_strobe && win_end_cnt == 3'd4 && phase == T_ACCUM) begin
        boundary_hold_q <= 1'b1;
        window_start_next_q <= sample_ctr;
        need_chain_start <= 1'b1;
        stamp_winend5_q <= cyc_q;
      end

      // First window start post-reset: latch window_start, open
      // accumulation. (No first_record_q gate: it clears at the reset-
      // release autostart, long before the first win_start — gating on
      // it deadlocked the whole post-close pipeline in IDLE, found by
      // the chunked-flow probe. first_start_done spans all records
      // (boundary capture serves N+1); recovery from error is by reset.)
      if (win_start_pulse && !first_start_done) begin
        first_start_done <= 1'b1;
        window_start_q <= sample_ctr;
        if (phase == T_IDLE) phase <= T_ACCUM;
      end

      // Window closes counted in accumulation AND idle-with-traffic
      // (records are contiguous: window 6 closes while T_IDLE).
      if (win_end_strobe && (phase == T_ACCUM || phase == T_IDLE)) begin
        if (win_end_cnt != 3'd4) win_end_cnt <= win_end_cnt + 3'd1;
      end

      // Chain job start (needs prev + free wrap; boundary hold clears:
      // PREV never consumes, so E bytes stay queued for SAMP). PHASE-GATED
      // to T_ACCUM/T_IDLE (amendment-6 hardening): without it, a chain
      // start can fire the SAME edge as an HMAC start (both sample stale
      // pre-edge idle/CHAIN), landing the chain's 2-cycle INIT inside the
      // HMAC window where the mux drops it — the chain then wedges in
      // PREV forever (no re-INIT exists) while the HMAC completes
      // normally. Found by the events timeline (job-2 start == HMAC-1
      // start edge). Post-T_OUT starts keep the E_4+~1100 shape (need +
      // prev + wrap all ready by then); liveness holds (phase always
      // returns to ACCUM/IDLE, error recovery is by reset).
      if (need_chain_start && !chain_busy_w && !hmac_busy_w && wrap_sel_q == CHAIN_SEL
          && (first_record_q || hash_valid_q)
          && (phase == T_ACCUM || phase == T_IDLE)) begin
        chain_start_q <= 1'b1;
        chain_genesis_q <= first_record_q;
        need_chain_start <= 1'b0;
        boundary_hold_q <= 1'b0;
        if (first_record_q) first_record_q <= 1'b0;
        else window_start_q <= window_start_next_q;
      end

      // Sub-error propagation (unexpected in top; fail loud, abort record).
      if ((chain_err_w || hmac_err_w) && phase != T_IDLE) begin
        error_out <= 1'b1;
        boundary_hold_q <= 1'b0;
        need_chain_start <= 1'b0;
        phase <= T_IDLE;
      end

      case (phase)
        T_IDLE: begin
          // Windows flowing with no open record only happen pre-first-start
          // (covered above) — accumulation opens on the first win_start.
        end
        T_ACCUM: begin
          // Lone sample_last ends SAMP at the record boundary (E held).
          if (boundary_hold_q && chain_phase_w == CHAIN_SAMP)
            chain_samp_last_q <= 1'b1; // lone: no byte, no seal (SAMP_END)
          if (rec_done_w) begin
            stamp_recdone_q <= cyc_q;
            // Latch sidecar at close (per-record snapshot, amendment 7).
            sc_crc_err_cnt <= crc_err_cnt;
            sc_resync_cnt <= resync_cnt;
            sc_ferr_cnt <= ferr_cnt;
            sc_skid_ovf_cnt <= skid_drop_cnt_w;
            sc_saturated <= frame_sat || (ferr_cnt == 16'hFFFF)
              || (crc_err_cnt == 16'hFFFF) || (resync_cnt == 16'hFFFF);
            sc_counter <= cnt_out_w;
            cnt_clear_armed <= 1'b1; // clear one edge after the latch
            // P_AVG division (skipped when m == 0).
            div_p_sum_q <= rec_p_sum_w;
            div_m_q <= rec_m_total_w;
            if (rec_m_total_w != 14'd0) begin
              div_start_q <= 1'b1;
              div_started_q <= 1'b1;
            end
            phase <= T_DIVWAIT;
          end
        end
        T_DIVWAIT: begin
          if (cnt_clear_armed) begin
            cnt_clear_q <= 1'b1;
            cnt_clear_armed <= 1'b0;
          end
          if (!div_started_q || div_done_w) begin
            div_started_q <= 1'b0;
            desc_wstart_q <= window_start_q;
            desc_overrun_q <= rec_overrun_cnt_w;
            desc_p_avg_q <= (rec_m_total_w == 14'd0) ? 32'd0 : div_quot_w[31:0];
            desc_energy_q <= rec_energy_w;
            desc_tmp_flags = rec_flags_w;
            // Skid-overflow policy (amendment 5): bit 7 + INVALIDATE
            // (valid bits cleared, p_avg 0, energy excluded), consumed.
            if (skid_overflow_out) begin
              desc_tmp_flags = (rec_flags_w & 8'h60) | 8'h80;
              desc_p_avg_q <= 32'd0;
              desc_energy_q <= energy_q;
              skid_overflow_out <= 1'b0;
            end
            desc_flags_q <= desc_tmp_flags;
            phase <= T_ASSEMBLE;
          end
        end
        T_ASSEMBLE: begin
          // Pack descriptor bytes (counter pre-incr; device const).
          desc_mem[0] <= cnt_out_w[7:0];
          desc_mem[1] <= cnt_out_w[15:8];
          desc_mem[2] <= cnt_out_w[23:16];
          desc_mem[3] <= cnt_out_w[31:24];
          desc_mem[4] <= desc_wstart_q[7:0];
          desc_mem[5] <= desc_wstart_q[15:8];
          desc_mem[6] <= desc_wstart_q[23:16];
          desc_mem[7] <= desc_wstart_q[31:24];          desc_mem[8] <= rec_zc_w[7:0];
          desc_mem[9] <= {4'd0, rec_zc_w[11:8]};
          desc_mem[10] <= rec_zc_w[19:12];
          desc_mem[11] <= {4'd0, rec_zc_w[23:20]};
          desc_mem[12] <= rec_zc_w[31:24];
          desc_mem[13] <= {4'd0, rec_zc_w[35:32]};
          desc_mem[14] <= rec_zc_w[43:36];
          desc_mem[15] <= {4'd0, rec_zc_w[47:44]};
          desc_mem[16] <= rec_zc_w[55:48];
          desc_mem[17] <= {4'd0, rec_zc_w[59:56]};
          desc_mem[18] <= desc_flags_q;
          desc_mem[19] <= desc_overrun_q;
          desc_mem[20] <= DEVICE_ID[7:0];
          desc_mem[21] <= DEVICE_ID[15:8];
          desc_mem[22] <= DEVICE_ID[23:16];
          desc_mem[23] <= DEVICE_ID[31:24];
          desc_mem[24] <= desc_p_avg_q[7:0];
          desc_mem[25] <= desc_p_avg_q[15:8];
          desc_mem[26] <= desc_p_avg_q[23:16];
          desc_mem[27] <= desc_p_avg_q[31:24];
          desc_mem[28] <= desc_energy_q[7:0];
          desc_mem[29] <= desc_energy_q[15:8];
          desc_mem[30] <= desc_energy_q[23:16];
          desc_mem[31] <= desc_energy_q[31:24];
          desc_mem[32] <= desc_energy_q[39:32];
          desc_mem[33] <= desc_energy_q[47:40];
          desc_mem[34] <= desc_energy_q[55:48];
          desc_mem[35] <= desc_energy_q[63:56];
          phase <= T_CHAINWAIT;
        end
        T_CHAINWAIT: begin
          // Feed latched descriptor bytes to the chain (it waits in DESC_WAIT).
          chain_desc_q <= {desc_mem[35], desc_mem[34], desc_mem[33], desc_mem[32],
                           desc_mem[31], desc_mem[30], desc_mem[29], desc_mem[28],
                           desc_mem[27], desc_mem[26], desc_mem[25], desc_mem[24],
                           desc_mem[23], desc_mem[22], desc_mem[21], desc_mem[20],
                           desc_mem[19], desc_mem[18], desc_mem[17], desc_mem[16],
                           desc_mem[15], desc_mem[14], desc_mem[13], desc_mem[12],
                           desc_mem[11], desc_mem[10], desc_mem[9], desc_mem[8],
                           desc_mem[7], desc_mem[6], desc_mem[5], desc_mem[4],
                           desc_mem[3], desc_mem[2], desc_mem[1], desc_mem[0]};
          chain_desc_valid_q <= 1'b1;
          phase <= T_HMACWAIT;
        end
        T_HMACWAIT: begin
          if (chain_done_w) begin
            hash_q <= chain_hash_w;
            hash_valid_q <= 1'b1;
            first_record_q <= 1'b0;
            if (!provisioned_q) begin
              // Fail-loud interlock (B2): no zero-key tags, ever.
              error_out <= 1'b1;
              phase <= T_IDLE;
            end else begin
              wrap_sel_q <= HMAC_SEL; // boundary: chain done, HMAC starts
              hmac_start_q <= 1'b1;
      hmac_idx_q <= 7'd0;
              hmac_feeding_q <= 1'b1;
              for (j = 0; j < 32; j = j + 1) hash_b[j] <= chain_hash_w[255 - 8 * j-:8];
              for (j = 0; j < 36; j = j + 1) desc_b[j] <= chain_desc_q[8 * j+:8];
              phase <= T_HMAC;
            end
          end
        end
        T_HMAC: begin
          // Stream descriptor || hash (68 B) honoring msg_ready.
          if (hmac_feeding_q && hmac_msg_ready_w) begin
            hmac_msg_valid_q <= 1'b1;
            if (hmac_idx_q < 7'd36) hmac_msg_data_q <= desc_b[hmac_idx_q[5:0]];
            // Hash byte (idx-36): idx[4:0]+28 mod 32 == idx-36 on [36,67]
            // (avoids a 7-bit array index; exact, no borrow).
            else hmac_msg_data_q <= hash_b[hmac_idx_q[4:0] + 5'd28];
            hmac_msg_last_q <= (hmac_idx_q == 7'd67);
            if (hmac_idx_q == 7'd67) hmac_feeding_q <= 1'b0;
            else hmac_idx_q <= hmac_idx_q + 7'd1;
          end else begin
            hmac_msg_valid_q <= 1'b0;
            hmac_msg_last_q <= 1'b0;
          end
          if (hmac_done_w) begin
            wrap_sel_q <= CHAIN_SEL; // boundary: HMAC done, chain owns wrap
            hmac_feeding_q <= 1'b0;
            hmac_msg_valid_q <= 1'b0;
            hmac_msg_last_q <= 1'b0;
            phase <= T_OUT;
          end
        end
        T_OUT: begin
          // Publish (fail-closed on counter halt: no nonce reuse, ever).
          if (!cnt_halted_w) begin
            record_valid_out <= 1'b1;
            stamp_valid_q <= cyc_q;
            record_counter <= cnt_out_w;
            record_window_start <= desc_wstart_q;
            record_zc <= rec_zc_w;
            record_flags <= desc_flags_q;
            record_overrun_cnt <= desc_overrun_q;
            record_p_avg <= $signed(desc_p_avg_q);
            record_energy <= desc_energy_q;
            record_hash <= hash_q;
            record_hmac <= hmac_tag_w;
            energy_q <= desc_energy_q;
            cnt_incr_q <= 1'b1;
          end
          win_end_cnt <= 3'd0;
          phase <= T_ACCUM; // accumulation is contiguous across records
        end
        default: phase <= T_IDLE;
      endcase
    end
  end

endmodule
