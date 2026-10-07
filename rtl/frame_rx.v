// frame_rx.v — byte-stream L0 framer (Week 5a).
//
// Hardware half of the byte->frame path: SOF hunt (0xA5), 11-byte assembly,
// CRC16-CCITT-FALSE gate over counter || V || I, resync. Fed by uart_rx
// bytes (data_in + data_valid 1-clk pulse per byte); the cocotb tests prove
// it against tb/golden.py parse_l0_stream.
//
// Behavior (bit-exact vs the golden):
//   - Garbage bytes before SOF are ignored (stay hunting).
//   - A candidate needs 11 bytes [SOF][ctr u32 LE][V i16 LE][I i16 LE][CRC u16 LE].
//   - Good CRC -> latch (counter, v, i), pulse frame_valid for 1 clk.
//   - Bad CRC -> pulse crc_err for 1 clk, frame dropped, then rescan: the
//     failed candidate's bytes 1..10 are searched for the first SOF and the
//     tail from there becomes the next candidate prefix (equivalent to the
//     golden's +1 advance past false syncs, compressed: skipped bytes are
//     provably non-SOF). No SOF inside -> back to hunting.
//   - Trailing partial frames stay buffered (no output until completed).
//   - Health telemetry: crc_err_cnt++ on every CRC rejection, resync_cnt++
//     when the rejection reuses an inner-SOF prefix (back-to-hunt counts
//     only in crc_err_cnt). Both 16-bit saturating (sticky at 0xFFFF),
//     cleared on reset. These are telemetry, not policy.
//   - Continuity (replay/reorder) is NOT checked here — that is the
//     receiver/verifier job in Python (edge/receiver.py, ledger/verifier.py).
//     L0 counter monotonicity is enforced ONLY in the Python receiver;
//     this module outputs the counter field verbatim (DECISIONS.md Week 5a).
//
// Rescan timing: rescan_pos is combinational and the prefix shift completes
// in the SAME edge as crc_err, so the worst-case replay stall is 0 cycles;
// back-to-back bad-CRC frames cost 1 cycle each. The design accepts
// 1 byte/cycle, far above the line rate (160 clocks/byte at CLK_PER_BIT 16,
// 80 at the proposed 8), so overrun is structurally impossible — there is
// no multi-cycle rescan state for a byte to arrive during.
//
// No latches, no delays, Verilog-2005. All assignments are explicit
// (no variable-index memory writes) so Yosys infers plain registers.
`timescale 1ns / 1ps

module frame_rx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [7:0]  data_in, // byte from uart_rx (valid with data_valid)
    input  wire        data_valid, // 1-clk pulse per byte
    output reg  [31:0] frame_counter, // latched u32 L0 counter (holds)
    output reg  [15:0] frame_v, // latched i16 V code bit pattern (holds)
    output reg  [15:0] frame_i, // latched i16 I code bit pattern (holds)
    output reg         frame_valid, // 1-clk pulse on good CRC
    output reg         crc_err, // 1-clk pulse on bad CRC (frame dropped)
    output reg  [15:0] crc_err_cnt, // saturating count of CRC rejections
    output reg  [15:0] resync_cnt // saturating count of inner-SOF prefix reuses
);

  localparam [7:0] SOF = 8'hA5;

  // Candidate buffer: buf[0] is the SOF, buf[1..8] the CRC body,
  // buf[9..10] the received CRC (LE). n = bytes held (0 = hunting).
  reg [7:0] b0, b1, b2, b3, b4, b5, b6, b7, b8, b9;
  reg [3:0] n;

  // CRC16-CCITT-FALSE over 8 body bytes, MSB-first per byte
  // (== edge/framing.crc16_ccitt_false; check b"123456789" -> 0x29B1).
  function [15:0] crc16_body;
    input [7:0] c0, c1, c2, c3, c4, c5, c6, c7;
    reg [15:0] crc;
    reg [7:0] by;
    integer i, k;
    begin
      crc = 16'hFFFF;
      for (i = 0; i < 8; i = i + 1) begin
        case (i)
          0: by = c0;
          1: by = c1;
          2: by = c2;
          3: by = c3;
          4: by = c4;
          5: by = c5;
          6: by = c6;
          default: by = c7;
        endcase
        crc = crc ^ ({by, 8'd0});
        for (k = 0; k < 8; k = k + 1) begin
          if (crc[15]) crc = (crc << 1) ^ 16'h1021;
          else crc = crc << 1;
        end
      end
      crc16_body = crc;
    end
  endfunction

  // Completion-cycle combinational checks (candidate closes this cycle:
  // body bytes b1..b8 are stable, CRC low byte is b9, high byte arrives now).
  wire [15:0] crc_calc = crc16_body(b1, b2, b3, b4, b5, b6, b7, b8);
  wire [15:0] crc_rx = {data_in, b9};
  wire crc_ok = (crc_calc == crc_rx);

  // Rescan: first SOF position inside the failed candidate's bytes 1..10
  // (byte 10 is data_in, arriving this cycle). 0 = none.
  reg [3:0] rescan_pos;
  always @* begin
    if (b1 == SOF) rescan_pos = 4'd1;
    else if (b2 == SOF) rescan_pos = 4'd2;
    else if (b3 == SOF) rescan_pos = 4'd3;
    else if (b4 == SOF) rescan_pos = 4'd4;
    else if (b5 == SOF) rescan_pos = 4'd5;
    else if (b6 == SOF) rescan_pos = 4'd6;
    else if (b7 == SOF) rescan_pos = 4'd7;
    else if (b8 == SOF) rescan_pos = 4'd8;
    else if (b9 == SOF) rescan_pos = 4'd9;
    else if (data_in == SOF) rescan_pos = 4'd10;
    else rescan_pos = 4'd0;
  end

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      b0 <= 8'd0; b1 <= 8'd0; b2 <= 8'd0; b3 <= 8'd0; b4 <= 8'd0;
      b5 <= 8'd0; b6 <= 8'd0; b7 <= 8'd0; b8 <= 8'd0; b9 <= 8'd0;
      n <= 4'd0;
      frame_counter <= 32'd0;
      frame_v <= 16'd0;
      frame_i <= 16'd0;
      frame_valid <= 1'b0;
      crc_err <= 1'b0;
      crc_err_cnt <= 16'd0;
      resync_cnt <= 16'd0;
    end else begin
      frame_valid <= 1'b0;
      crc_err <= 1'b0;
      if (data_valid) begin
        if (n == 4'd0) begin
          // Hunting: only SOF opens a candidate; garbage is ignored.
          if (data_in == SOF) begin
            b0 <= data_in;
            n <= 4'd1;
          end
        end else if (n == 4'd10) begin
          // Candidate closes this cycle (this byte is buf[10]).
          if (crc_ok) begin
            frame_counter <= {b4, b3, b2, b1};
            frame_v <= {b6, b5};
            frame_i <= {b8, b7};
            frame_valid <= 1'b1;
            n <= 4'd0;
          end else begin
            crc_err <= 1'b1;
            if (crc_err_cnt != 16'hFFFF) crc_err_cnt <= crc_err_cnt + 16'd1;
            if ((rescan_pos != 4'd0) && (resync_cnt != 16'hFFFF))
              resync_cnt <= resync_cnt + 16'd1;
            // Rescan: tail from the first inner SOF becomes the next
            // candidate prefix (bytes known stable: b1..b9 + data_in).
            case (rescan_pos)
              4'd1: begin
                b0 <= b1; b1 <= b2; b2 <= b3; b3 <= b4; b4 <= b5;
                b5 <= b6; b6 <= b7; b7 <= b8; b8 <= b9; b9 <= data_in;
                n <= 4'd10;
              end
              4'd2: begin
                b0 <= b2; b1 <= b3; b2 <= b4; b3 <= b5; b4 <= b6;
                b5 <= b7; b6 <= b8; b7 <= b9; b8 <= data_in;
                n <= 4'd9;
              end
              4'd3: begin
                b0 <= b3; b1 <= b4; b2 <= b5; b3 <= b6; b4 <= b7;
                b5 <= b8; b6 <= b9; b7 <= data_in;
                n <= 4'd8;
              end
              4'd4: begin
                b0 <= b4; b1 <= b5; b2 <= b6; b3 <= b7; b4 <= b8;
                b5 <= b9; b6 <= data_in;
                n <= 4'd7;
              end
              4'd5: begin
                b0 <= b5; b1 <= b6; b2 <= b7; b3 <= b8; b4 <= b9;
                b5 <= data_in;
                n <= 4'd6;
              end
              4'd6: begin
                b0 <= b6; b1 <= b7; b2 <= b8; b3 <= b9; b4 <= data_in;
                n <= 4'd5;
              end
              4'd7: begin
                b0 <= b7; b1 <= b8; b2 <= b9; b3 <= data_in;
                n <= 4'd4;
              end
              4'd8: begin
                b0 <= b8; b1 <= b9; b2 <= data_in;
                n <= 4'd3;
              end
              4'd9: begin
                b0 <= b9; b1 <= data_in;
                n <= 4'd2;
              end
              4'd10: begin
                b0 <= data_in;
                n <= 4'd1;
              end
              default: n <= 4'd0; // no inner SOF: back to hunting
            endcase
          end
        end else begin
          // Collecting bytes 1..9 into the open candidate.
          case (n)
            4'd1: b1 <= data_in;
            4'd2: b2 <= data_in;
            4'd3: b3 <= data_in;
            4'd4: b4 <= data_in;
            4'd5: b5 <= data_in;
            4'd6: b6 <= data_in;
            4'd7: b7 <= data_in;
            4'd8: b8 <= data_in;
            4'd9: b9 <= data_in;
            default: begin end
          endcase
          n <= n + 4'd1;
        end
      end
    end
  end

endmodule
