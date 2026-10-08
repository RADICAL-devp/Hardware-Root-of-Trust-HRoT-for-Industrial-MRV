// sha256_wrap.v — streaming SHA-256 engine around one secworks sha256_core (Week 5c).
//
// The core hashes 512-bit blocks (66 cycles, no padding, init = first
// block with IV, next = subsequent). This wrapper adds the streaming
// shell the chain + HMAC path needs:
//
//   * 256-byte input FIFO with REAL backpressure: ready_out = (FILL &&
//     FIFO not full). A producer that honors ready never overflows; a
//     producer that ignores it sets the STICKY overflow flag (the byte
//     is dropped AND flagged — never silent). Cleared by init/rst.
//   * SHA-256 padding owned here (the core does none): after the seal,
//     0x80, zeros to length = 56 (mod 64), then the 64-bit big-endian
//     BIT length (byte count x 8; messages < 2^61 bytes by construction).
//   * init_in (hold >= 2 cycles) ABORTS anything — FIFO flushed, counters
//     zeroed, core held in reset while HIGH — and opens a new message.
//   * last_in WITH the final byte (or a lone last_in for the empty
//     message) seals; after the seal ready reads LOW and further valid
//     bytes set overflow while the in-flight digest stays exact.
//   * digest latch (held to next init) + 1-cycle digest_valid_out pulse.
//   * occupancy_out (FIFO bytes held): status/debug tap for peak reports.
//
// Byte order: message order in (first byte -> block[511:504]), digest
// bytes big-endian out (digest[255:248] first) — standard SHA-256.
//
// Single driver required on the input stream. No latches, no delays,
// Verilog-2005. Reset: rst_n async-assert / sync-release (house style);
// the core's reset_n is shared (rst_n & ~init_in) so init aborts it.
`timescale 1ns / 1ps

module sha256_wrap #(
    parameter FIFO_DEPTH = 256, // bytes; >= 128 required (pad flush + bursts)
    parameter FIFO_ADDRW = 8 // $clog2(FIFO_DEPTH)
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire [7:0]   data_in, // message byte, message order
    input  wire         valid_in, // byte presented this cycle
    input  wire         init_in, // hold >= 2 cycles: abort + open message
    input  wire         last_in, // with final byte (or lone): seal the message
    output wire         ready_out, // FILL && FIFO not full (real backpressure)
    output wire [255:0] digest_out, // latched at completion, held to next init
    output wire         digest_valid_out, // 1-cycle pulse at completion
    output wire         busy_out, // message open (FILL / POST)
    output wire         overflow_out, // STICKY: valid && !ready (init/rst clears)
    output wire [8:0]   occupancy_out, // FIFO bytes held (status/debug)
    // Core taps (measurement bulkhead: 1-cycle accept pulses + digest flag).
    output reg          core_init, // pulse: first block to the core
    output reg          core_next, // pulse: subsequent block to the core
    output wire         core_digest_valid // core digest flag (combinational)
);

  localparam ST_IDLE = 2'd0;
  localparam ST_FILL = 2'd1;
  localparam ST_POST = 2'd2; // sealed: drain FIFO, pad, hash, finish

  reg [1:0] state;

  // Input FIFO (head = write, tail = read, count 0..FIFO_DEPTH).
  reg [7:0] fifo_mem [0:255];
  reg [7:0] fifo_head;
  reg [7:0] fifo_tail;
  reg [8:0] fifo_cnt;

  // Message accounting (bytes).
  reg [63:0] accepted; // message bytes written to the FIFO
  reg [63:0] assembled_msg; // message bytes moved FIFO -> assembler
  reg [63:0] len_bits; // latched at seal: accepted x 8 (BE length field)

  // Block assembler (MSB-first shift: first byte -> sr[511:504]).
  reg [511:0] sr;
  reg [5:0]  fill; // bytes in sr (0..63; 64 means block_avail)
  reg        block_avail;

  // Pad generator (POST, once msg_remaining == 0).
  reg [1:0] pad_st; // 0 = P80, 1 = PZERO, 2 = PLEN, 3 = PDONE
  reg [5:0] zeros_left;
  reg [3:0] len_left; // length bytes remaining (8..1)
  reg [2:0] len_idx; // next length byte index (0 = MSB)

  // Core handoff + completion.
  reg        first; // next handoff uses init (first block)
  reg [31:0] accepts; // blocks handed to the core
  reg [31:0] dones; // core digest RISES seen (dv is sticky-high to next
  reg        core_dv_q; // accept, so edges — not levels — are counted)
  reg [255:0] digest_latch;
  reg        digest_valid_q;
  reg        overflow_q;

  integer k;

  wire in_fill = (state == ST_FILL);
  wire fifo_full = (fifo_cnt >= FIFO_DEPTH);
  // NOTE: FIFO_DEPTH = 256 needs the 9-bit compare above (8-bit count
  // would alias full/empty); occupancy_out carries the same 9-bit count.
  wire fifo_empty = (fifo_cnt == 9'd0);
  wire [63:0] msg_remaining = accepted - assembled_msg;

  wire take_fifo = in_fill || (state == ST_POST && msg_remaining != 64'd0);
  wire take_pad = (state == ST_POST) && (msg_remaining == 64'd0) && (pad_st != 2'd3);
  // A byte is taken exactly when one is available (FIFO msg byte or pad
  // byte); fill counts TAKEN bytes only (never idle cycles).
  wire take_byte = !block_avail && ((take_fifo && !fifo_empty) || take_pad);

  // Single-assign FIFO count: write and pop may fire the SAME cycle
  // (sustained 1 B/cycle streaming) and must net to zero, not last-win.
  wire do_write = valid_in && ready_out;
  wire do_pop = !block_avail && take_fifo && !fifo_empty;

  assign ready_out = in_fill && !fifo_full;
  assign busy_out = (state != ST_IDLE);
  assign digest_out = digest_latch;
  assign digest_valid_out = digest_valid_q;
  assign overflow_out = overflow_q;
  assign occupancy_out = fifo_cnt;

  // Pad byte mux (combinational; consumed 1/cycle into the assembler).
  reg [7:0] pad_byte;
  always @* begin
    case (pad_st)
      2'd0: pad_byte = 8'h80;
      2'd1: pad_byte = 8'h00;
      2'd2: pad_byte = len_bits[63 - {len_idx, 3'd0} -: 8];
      default: pad_byte = 8'h00;
    endcase
  end

  // Seal total includes a final byte riding WITH last_in (same cycle):
  // the FIFO write and the seal below both fire, so len/zeros must count it.
  wire [63:0] seal_total = accepted + ((valid_in && ready_out) ? 64'd1 : 64'd0);
  wire [6:0] seal_r = {1'b0, seal_total[5:0]} + 7'd1; // (len+1) mod 64, 1..64
  // Zeros to length = 56 (mod 64): 7-bit exact, no truncation reliance.
  wire [6:0] seal_z1 = 7'd56 - seal_r; // r <= 56: 0..55
  wire [6:0] seal_z2 = 7'd120 - seal_r; // r in 57..64: 63..56
  wire [5:0] seal_zeros = (seal_r <= 7'd56) ? seal_z1[5:0] : seal_z2[5:0];

  wire core_ready;
  wire [255:0] core_digest;
  wire core_dv;
  wire dv_edge = core_dv && !core_dv_q;
  assign core_digest_valid = core_dv;

  sha256_core core (
      .clk(clk),
      .reset_n(rst_n & ~init_in),
      .init(core_init),
      .next(core_next),
      .mode(1'b1), // SHA-256 (M-H5 flips this: digests go SHA-224)
      .block(sr),
      .ready(core_ready),
      .digest(core_digest),
      .digest_valid(core_dv)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (k = 0; k < 256; k = k + 1) fifo_mem[k] <= 8'd0;
      state <= ST_IDLE;
      fifo_head <= 8'd0;
      fifo_tail <= 8'd0;
      fifo_cnt <= 9'd0;
      accepted <= 64'd0;
      assembled_msg <= 64'd0;
      len_bits <= 64'd0;
      sr <= 512'd0;
      fill <= 6'd0;
      block_avail <= 1'b0;
      pad_st <= 2'd0;
      zeros_left <= 6'd0;
      len_left <= 4'd0;
      len_idx <= 3'd0;
      first <= 1'b1;
      accepts <= 32'd0;
      dones <= 32'd0;
      core_dv_q <= 1'b0;
      digest_latch <= 256'd0;
      digest_valid_q <= 1'b0;
      overflow_q <= 1'b0;
      core_init <= 1'b0;
      core_next <= 1'b0;
    end else begin
      core_init <= 1'b0;
      core_next <= 1'b0;
      digest_valid_q <= 1'b0;
      if (init_in) begin
        // Abort + open: flush FIFO, zero accounting, clear flag. The
        // core is held in reset while init_in is HIGH (port wiring).
        state <= ST_FILL;
        fifo_head <= 8'd0;
        fifo_tail <= 8'd0;
        fifo_cnt <= 9'd0;
        accepted <= 64'd0;
        assembled_msg <= 64'd0;
        len_bits <= 64'd0;
        sr <= 512'd0;
        fill <= 6'd0;
        block_avail <= 1'b0;
        pad_st <= 2'd0;
        zeros_left <= 6'd0;
        len_left <= 4'd0;
        len_idx <= 3'd0;
        first <= 1'b1;
        accepts <= 32'd0;
        dones <= 32'd0;
        core_dv_q <= 1'b0;
        overflow_q <= 1'b0;
      end else begin
        // Overflow: valid while not ready (any state; the byte is NOT
        // consumed). Sticky to init/rst — never silent.
        if (valid_in && !ready_out) overflow_q <= 1'b1;
        // FIFO write (honored bytes only; count updated once below).
        if (do_write) begin
          fifo_mem[fifo_head] <= data_in;
          fifo_head <= fifo_head + 8'd1;
          accepted <= accepted + 64'd1;
        end
        // Seal (FILL only; init wins ties by the branch above).
        if (in_fill && last_in) begin
          state <= ST_POST;
          len_bits <= seal_total << 3; // byte count x 8 (messages < 2^61 B)
          zeros_left <= seal_zeros;
          pad_st <= 2'd0;
          len_left <= 4'd8;
          len_idx <= 3'd0;
        end
        // Assembler: one byte per cycle from FIFO (msg) or padgen (pad);
        // fill advances ONLY on a taken byte (see take_byte).
        if (take_byte) begin
          if (take_fifo && !fifo_empty) begin
            sr <= {sr[503:0], fifo_mem[fifo_tail]};
            fifo_tail <= fifo_tail + 8'd1;
            assembled_msg <= assembled_msg + 64'd1;
          end else begin
            sr <= {sr[503:0], pad_byte};
            if (pad_st == 2'd0) pad_st <= (zeros_left == 6'd0) ? 2'd2 : 2'd1;
            else if (pad_st == 2'd1) begin
              if (zeros_left == 6'd1) pad_st <= 2'd2;
              else zeros_left <= zeros_left - 6'd1;
            end else if (pad_st == 2'd2) begin
              if (len_left == 4'd1) pad_st <= 2'd3;
              len_left <= len_left - 4'd1;
              len_idx <= len_idx + 3'd1;
            end
          end
          if (fill == 6'd63) block_avail <= 1'b1;
          fill <= fill + 6'd1;
        end
        // Unified FIFO count (write + pop same cycle nets to zero).
        fifo_cnt <= fifo_cnt + (do_write ? 9'd1 : 9'd0) - (do_pop ? 9'd1 : 9'd0);
        // Handoff: full block + core ready -> init (first) / next.
        if (block_avail && core_ready) begin
          if (first) begin
            core_init <= 1'b1;
            first <= 1'b0;
          end else core_next <= 1'b1;
          block_avail <= 1'b0;
          fill <= 6'd0;
          accepts <= accepts + 32'd1;
        end
        // Completion: sealed, padding fully assembled, no block waiting,
        // every accepted block digested -> latch + pulse + IDLE.
        core_dv_q <= core_dv;
        if (dv_edge) dones <= dones + 32'd1;
        if (state == ST_POST && pad_st == 2'd3 && !block_avail &&
            accepts != 32'd0 && dones + (dv_edge ? 32'd1 : 32'd0) == accepts) begin
          digest_latch <= core_digest;
          digest_valid_q <= 1'b1;
          state <= ST_IDLE;
        end
      end
    end
  end

endmodule
