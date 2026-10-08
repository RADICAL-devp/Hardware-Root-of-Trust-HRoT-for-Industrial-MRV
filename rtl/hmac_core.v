// hmac_core.v — HMAC-SHA256 sequencer + key schedule (Week 5c).
//
// This module holds NO hash engine: it drives ONE external sha256_wrap
// (one-core sharing; tb/hmac_wrap_pair.v wires them for unit tests,
// top.v will own the mux in 5d). Per job:
//
//   start (IDLE) latches the key (keys > 64 B take a pre-hash pass
//   first, FIPS 198); then INNER = SHA((key^ipad) || msg) with the
//   message STREAMED in (msg_* handshake, any length); then OUTER =
//   SHA((key^opad) || inner_digest); done pulses with the latched tag.
//
// Wrap discipline: w_init held 2 cycles per pass (wrap protocol);
// w_valid is asserted ONLY with w_ready (never self-inflicted
// overflow); w_last rides the final byte of each pass (P0: final key
// byte; INNER: forwarded msg_last; OUTER: 32nd inner byte).
// msg_ready_out = (INNER msg stage) && w_ready: backpressure flows
// top -> hmac -> wrap with no drop possible (valid-hold handshake).
//
// Errors (sticky to start/rst; abort with NO done, busy drops):
//   * key_len_in > KEY_MAX at start (job rejected, stays IDLE).
//   * w_overflow outside P_INIT (P_INIT is masked: wrap init clears a
//     stale flag, so the first two cycles cannot judge).
// start_in while busy is IGNORED (top-level protocol guarantees
// IDLE-start; the TB asserts it). No latches, Verilog-2005,
// rst_n async-assert / sync-release (house style).
`timescale 1ns / 1ps

module hmac_core #(
    parameter KEY_MAX = 160, // max key bytes (covers RFC 4231: max 131)
    parameter KEY_BITW = KEY_MAX * 8
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire [KEY_BITW-1:0]  key_data_in, // [7:0] = key byte 0 (LSB-first)
    input  wire [7:0]           key_len_in, // 0..KEY_MAX bytes
    input  wire [7:0]           msg_data_in, // streamed message byte
    input  wire                 msg_valid_in,
    input  wire                 msg_last_in, // with final byte (or lone)
    output wire                 msg_ready_out,
    input  wire                 start_in, // pulse in IDLE (ignored when busy)
    output reg  [255:0]         hmac_out, // held to next start
    output reg                  done_out, // 1-cycle pulse, clean completion
    output wire                 busy_out,
    output reg                  error_out, // sticky to start/rst
    output wire [3:0]           phase_out, // visibility tap
    // sha256_wrap master (external instance).
    output reg                  w_init,
    output reg  [7:0]           w_data,
    output reg                  w_valid,
    output reg                  w_last,
    input  wire                 w_ready,
    input  wire [255:0]         w_digest,
    input  wire                 w_digest_valid, // wrap's 1-cycle pulse
    input  wire                 w_overflow
);

  localparam PH_IDLE = 4'd0;
  localparam PH_INIT = 4'd1; // hold w_init 2 cycles, then dispatch by pass
  localparam PH_P0_FEED = 4'd2;
  localparam PH_P0_DV = 4'd3;
  localparam PH_IN_PAD = 4'd4; // 64 x (k_eff ^ ipad)
  localparam PH_IN_MSG = 4'd5; // forwarded message stream
  localparam PH_IN_END = 4'd6; // lone-last seal (empty tail): w_last alone
  localparam PH_IN_DV = 4'd7;
  localparam PH_OUT_PAD = 4'd8; // 64 x (k_eff ^ opad)
  localparam PH_OUT_MSG = 4'd9; // 32 inner-digest bytes
  localparam PH_OUT_DV = 4'd10;
  localparam PH_FINISH = 4'd11; // done pulse, then IDLE

  localparam PASS_P0 = 2'd0;
  localparam PASS_IN = 2'd1;
  localparam PASS_OUT = 2'd2;

  reg [3:0] phase;
  reg [1:0] pass;
  reg [1:0] init_cnt;
  reg [7:0] feed_idx;
  reg [7:0] feed_total; // fixed-part byte counts (msg streams open-ended)
  reg [7:0] key_len;
  reg       use_p0; // key_len > 64 at start

  reg [7:0] key_mem [0:159]; // latched key, zero-padded past len
  reg [7:0] k32 [0:31]; // pre-hashed long key (BE digest bytes)
  reg [7:0] inner_b [0:31]; // inner digest (BE bytes, OUTER message)
  reg [255:0] inner_d;

  integer j;

  // Effective key byte: pre-hashed digest (zero past 32) for long keys,
  // latched key (zero-padded at start) for short keys. Array indices are
  // sliced to need-width (Verilator WIDTHTRUNC-clean).
  wire [7:0] keff_byte = use_p0 ? ((feed_idx < 8'd32) ? k32[feed_idx[4:0]] : 8'd0)
                                : key_mem[feed_idx];

  assign busy_out = (phase != PH_IDLE);
  assign phase_out = phase;
  assign msg_ready_out = (phase == PH_IN_MSG) && w_ready;

  always @* begin
    w_init = 1'b0;
    w_data = 8'd0;
    w_valid = 1'b0;
    w_last = 1'b0;
    if (phase == PH_INIT) w_init = 1'b1;
    else if (phase == PH_P0_FEED && w_ready) begin
      w_valid = 1'b1;
      w_data = key_mem[feed_idx];
      w_last = (feed_idx == key_len - 8'd1);
    end else if (phase == PH_IN_PAD && w_ready) begin
      w_valid = 1'b1;
      w_data = keff_byte ^ 8'h36; // ipad (M-H7 swaps these: RFC catches it)
      w_last = 1'b0;
    end else if (phase == PH_IN_MSG && msg_valid_in && w_ready) begin
      w_valid = 1'b1;
      w_data = msg_data_in;
      w_last = msg_last_in;
    end else if (phase == PH_IN_END) begin
      w_valid = 1'b0;
      w_last = 1'b1; // lone seal: no byte, wrap seals on last alone
    end else if (phase == PH_OUT_PAD && w_ready) begin
      w_valid = 1'b1;
      w_data = keff_byte ^ 8'h5c; // opad
      w_last = 1'b0;
    end else if (phase == PH_OUT_MSG && w_ready) begin
      w_valid = 1'b1;
      w_data = inner_b[feed_idx[4:0]];
      w_last = (feed_idx == 8'd31);
    end
  end

  wire w_consume = w_valid && w_ready;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (j = 0; j < 160; j = j + 1) key_mem[j] <= 8'd0;
      for (j = 0; j < 32; j = j + 1) k32[j] <= 8'd0;
      for (j = 0; j < 32; j = j + 1) inner_b[j] <= 8'd0;
      phase <= PH_IDLE;
      pass <= PASS_IN;
      init_cnt <= 2'd0;
      feed_idx <= 8'd0;
      feed_total <= 8'd0;
      key_len <= 8'd0;
      use_p0 <= 1'b0;
      inner_d <= 256'd0;
      hmac_out <= 256'd0;
      done_out <= 1'b0;
      error_out <= 1'b0;
    end else begin
      done_out <= 1'b0;
      if (phase != PH_IDLE && phase != PH_INIT && w_overflow) begin
        // Abort: wrap overflowed mid-job (only reachable via a rogue
        // producer on the shared wrap — the TB inject mux). No done.
        error_out <= 1'b1;
        phase <= PH_IDLE;
      end else if (phase == PH_IDLE && start_in) begin
        if (key_len_in > KEY_MAX) begin
          error_out <= 1'b1; // job rejected, stays IDLE
        end else begin
          for (j = 0; j < 160; j = j + 1)
            key_mem[j] <= (j < key_len_in) ? key_data_in[8 * j+:8] : 8'd0;
          key_len <= key_len_in;
          use_p0 <= (key_len_in > 8'd64);
          pass <= (key_len_in > 8'd64) ? PASS_P0 : PASS_IN;
          error_out <= 1'b0; // new job, new verdict
          init_cnt <= 2'd2;
          feed_idx <= 8'd0;
          phase <= PH_INIT;
        end
      end else if (phase == PH_INIT) begin
        if (init_cnt == 2'd1) begin
          init_cnt <= 2'd0;
          feed_idx <= 8'd0;
          if (pass == PASS_P0) begin
            feed_total <= key_len;
            phase <= PH_P0_FEED;
          end else if (pass == PASS_IN) begin
            feed_total <= 8'd64;
            phase <= PH_IN_PAD;
          end else begin
            feed_total <= 8'd64;
            phase <= PH_OUT_PAD;
          end
        end else init_cnt <= init_cnt - 2'd1;
      end else if (w_consume) begin
        // Fixed-part advance (IN_MSG ends on forwarded msg_last instead,
        // and never advances the pad index: the message is open-ended).
        if (phase == PH_IN_MSG) begin
          if (msg_last_in) phase <= PH_IN_DV;
        end else begin
          if (phase == PH_OUT_PAD && feed_idx == feed_total - 8'd1) begin
            feed_total <= 8'd32; // OUT_MSG: exactly the 32 inner bytes
            feed_idx <= 8'd0;
            phase <= PH_OUT_MSG;
          end else begin
            feed_idx <= feed_idx + 8'd1;
            if (feed_idx == feed_total - 8'd1) begin
              if (phase == PH_P0_FEED) phase <= PH_P0_DV;
              else if (phase == PH_IN_PAD) phase <= PH_IN_MSG;
              else if (phase == PH_OUT_MSG) phase <= PH_OUT_DV;
            end
          end
        end
      end else if (phase == PH_IN_MSG && msg_last_in && !msg_valid_in) begin
        phase <= PH_IN_END; // lone last: seal the (possibly empty) tail
      end else if (phase == PH_IN_END) begin
        phase <= PH_IN_DV;
      end else if (phase == PH_P0_DV && w_digest_valid) begin
        for (j = 0; j < 32; j = j + 1) k32[j] <= w_digest[255 - 8 * j-:8];
        pass <= PASS_IN;
        init_cnt <= 2'd2;
        phase <= PH_INIT;
      end else if (phase == PH_IN_DV && w_digest_valid) begin
        inner_d <= w_digest;
        for (j = 0; j < 32; j = j + 1) inner_b[j] <= w_digest[255 - 8 * j-:8];
        pass <= PASS_OUT;
        init_cnt <= 2'd2;
        phase <= PH_INIT;
      end else if (phase == PH_OUT_DV && w_digest_valid) begin
        hmac_out <= w_digest;
        phase <= PH_FINISH;
      end else if (phase == PH_FINISH) begin
        done_out <= 1'b1;
        phase <= PH_IDLE;
      end
    end
  end

endmodule
