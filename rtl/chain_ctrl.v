// chain_ctrl.v — hash-chain record sequencer (Week 5d1).
//
// Holds NO hash engine: drives ONE external sha256_wrap (the same core
// top.v shares with HMAC in 5d2; tb/chain_wrap_pair.v wires them for
// unit tests). Per record: latch prev (genesis zeros iff
// prev_sel_genesis, else the held digest) -> stream 32 prev bytes ->
// stream sample bytes (8 B atomic units, ready-gated) -> latch the
// 36 B descriptor -> stream it -> wait the wrap digest -> latch
// window_hash (which becomes the next record's prev) -> done pulse.
//
// The chain message is ONE wrap message: prev || samples || descriptor
// (the Week 5c streaming reorder). Hashed bytes are exactly the
// in-window frame_rx-ACCEPTED bytes: CRC-rejected L0 frames never enter
// (the producer withholds them), out-of-window samples hash in their
// own windows. An empty sample tail seals NOTHING (SAMP_END moves on
// without w_last); only the 36th descriptor byte carries w_last.
//
// Phases (frozen contract, see tb/w5d_chain_cocotb.py): 0 IDLE, 1 INIT,
// 2 PREV, 3 SAMP, 4 SAMP_END, 5 DESC_WAIT, 6 DESC_FEED, 7 DV, 8 FINISH.
// start_in while busy is IGNORED (same protocol as hmac_core).
// Errors (sticky to start/rst; abort with NO done, busy drops):
//   * sample_valid_in while !sample_ready_out (stream violation);
//   * w_overflow outside INIT (INIT is masked: wrap init clears a stale
//     flag, so the first two cycles cannot judge — same rationale as
//     hmac_core P_INIT).
// No latches, Verilog-2005, rst_n async-assert / sync-release.
`timescale 1ns / 1ps

module chain_ctrl (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         start_in, // pulse in IDLE (ignored when busy)
    input  wire         prev_sel_genesis, // 1: prev = zeros; 0: held digest
    input  wire [7:0]   sample_data_in, // streamed sample byte
    input  wire         sample_valid_in,
    input  wire         sample_last_in, // with final byte (or lone: empty tail)
    output wire         sample_ready_out, // SAMP && wrap ready (backpressure)
    input  wire [287:0] desc_data_in, // [7:0] = descriptor byte 0 (LSB-first)
    input  wire         desc_valid_in, // pulse in DESC_WAIT: latch + stream
    output wire [255:0] window_hash_out, // held to next start
    output wire [255:0] prev_hash_out, // chaining value for the next record
    output reg          done_out, // 1-cycle pulse, clean completion
    output wire         busy_out,
    output reg          error_out, // sticky to start/rst
    output wire [3:0]   phase_out, // visibility tap
    // sha256_wrap master (external instance).
    output reg          w_init,
    output reg  [7:0]   w_data,
    output reg          w_valid,
    output reg          w_last,
    input  wire         w_ready,
    input  wire [255:0] w_digest,
    input  wire         w_digest_valid, // wrap's 1-cycle pulse
    input  wire         w_overflow
);

  localparam PH_IDLE = 4'd0;
  localparam PH_INIT = 4'd1; // hold w_init 2 cycles
  localparam PH_PREV = 4'd2; // 32 prev bytes
  localparam PH_SAMP = 4'd3; // open-ended sample stream
  localparam PH_SAMP_END = 4'd4; // lone-last seal of an empty tail (no byte)
  localparam PH_DESC_WAIT = 4'd5; // wait desc_valid (descriptor known at close)
  localparam PH_DESC_FEED = 4'd6; // 36 descriptor bytes, w_last on the 36th
  localparam PH_DV = 4'd7; // wait wrap digest
  localparam PH_FINISH = 4'd8; // done pulse, then IDLE

  localparam PREV_LEN = 6'd32;
  localparam DESC_LEN = 6'd36;

  reg [3:0] phase;
  reg [1:0] init_cnt;
  reg [5:0] feed_idx;
  reg [5:0] feed_total;

  reg [7:0] prev_b [0:31]; // prev bytes, message order (zeros iff genesis)
  reg [7:0] desc_b [0:35]; // latched descriptor, message order
  reg [255:0] prev_hash_q; // chaining value (zeros until the first done)
  reg [255:0] window_hash_q;

  integer j;

  // Digest bytes are big-endian out (digest[255:248] first) per the wrap
  // contract; prev byte i rides the stream in message order.
  wire [7:0] prev_feed_byte = prev_b[feed_idx[4:0]];

  assign busy_out = (phase != PH_IDLE);
  assign phase_out = phase;
  assign sample_ready_out = (phase == PH_SAMP) && w_ready;
  assign prev_hash_out = prev_hash_q;
  assign window_hash_out = window_hash_q;

  always @* begin
    w_init = 1'b0;
    w_data = 8'd0;
    w_valid = 1'b0;
    w_last = 1'b0;
    if (phase == PH_INIT) w_init = 1'b1;
    else if (phase == PH_PREV && w_ready) begin
      w_valid = 1'b1;
      w_data = prev_feed_byte;
      w_last = 1'b0; // message continues (samples follow)
    end else if (phase == PH_SAMP && sample_valid_in && w_ready) begin
      w_valid = 1'b1;
      w_data = sample_data_in;
      w_last = 1'b0; // never seals: the descriptor always follows
    end else if (phase == PH_DESC_FEED && w_ready) begin
      w_valid = 1'b1;
      w_data = desc_b[feed_idx[5:0]];
      w_last = (feed_idx == DESC_LEN - 6'd1);
    end
  end

  wire w_consume = w_valid && w_ready;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (j = 0; j < 32; j = j + 1) prev_b[j] <= 8'd0;
      for (j = 0; j < 36; j = j + 1) desc_b[j] <= 8'd0;
      phase <= PH_IDLE;
      init_cnt <= 2'd0;
      feed_idx <= 6'd0;
      feed_total <= 6'd0;
      prev_hash_q <= 256'd0;
      window_hash_q <= 256'd0;
      done_out <= 1'b0;
      error_out <= 1'b0;
    end else begin
      done_out <= 1'b0;
      if (phase != PH_IDLE && phase != PH_INIT && w_overflow) begin
        // Abort: wrap overflowed mid-record (only reachable via a rogue
        // producer on the shared wrap). No done. (M-H* class: the abort
        // path is pinned by test_chain_stream_violation on the TB side;
        // wrap-side overflow pins live in the week5c suites.)
        error_out <= 1'b1;
        phase <= PH_IDLE;
      end else if (phase != PH_IDLE && phase != PH_SAMP && sample_valid_in) begin
        // Stream violation: sample bytes are only legal in SAMP while
        // ready (SAMP_END/DESC phases take no sample bytes). Fail loud.
        error_out <= 1'b1;
        phase <= PH_IDLE;
      end else if (phase == PH_IDLE && start_in) begin
        for (j = 0; j < 32; j = j + 1)
          prev_b[j] <= prev_sel_genesis ? 8'd0 : prev_hash_q[255 - 8 * j-:8];
        error_out <= 1'b0; // new record, new verdict
        init_cnt <= 2'd2;
        feed_idx <= 6'd0;
        phase <= PH_INIT;
      end else if (phase == PH_INIT) begin
        if (init_cnt == 2'd1) begin
          init_cnt <= 2'd0;
          feed_idx <= 6'd0;
          feed_total <= PREV_LEN;
          phase <= PH_PREV;
        end else init_cnt <= init_cnt - 2'd1;
      end else if (w_consume) begin
        if (phase == PH_SAMP) begin
          if (sample_last_in) phase <= PH_DESC_WAIT;
        end else begin
          feed_idx <= feed_idx + 6'd1;
          if (feed_idx == feed_total - 6'd1) begin
            if (phase == PH_PREV) phase <= PH_SAMP;
            else if (phase == PH_DESC_FEED) phase <= PH_DV;
          end
        end
      end else if (phase == PH_SAMP && sample_last_in && !sample_valid_in) begin
        phase <= PH_SAMP_END; // lone last: empty tail, no byte, no seal
      end else if (phase == PH_SAMP_END) begin
        phase <= PH_DESC_WAIT;
      end else if (phase == PH_DESC_WAIT && desc_valid_in) begin
        for (j = 0; j < 36; j = j + 1) desc_b[j] <= desc_data_in[8 * j+:8];
        feed_idx <= 6'd0;
        feed_total <= DESC_LEN;
        phase <= PH_DESC_FEED;
      end else if (phase == PH_DV && w_digest_valid) begin
        window_hash_q <= w_digest;
        prev_hash_q <= w_digest; // linkage: next record's prev (M-C5)
        phase <= PH_FINISH;
      end else if (phase == PH_FINISH) begin
        done_out <= 1'b1;
        phase <= PH_IDLE;
      end
    end
  end

endmodule
