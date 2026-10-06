// uart_rx.v — 8N1 UART byte receiver (Week 4b).
//
// Line signaling only: start-bit edge detect, 16x mid-bit sampling, LSB
// first, 1 stop bit. Emits (data[7:0], data_valid 1-clk pulse) per good
// byte; a bad stop bit raises framing_error (1-clk pulse) and drops the
// byte. L0 framing (SOF hunt 0xA5 + CRC16 gate + resync) lives in the
// cocotb testbench against tb/golden.py parse_l0_stream — this module is
// the bit-level half that the golden explicitly does NOT model.
//
// Properties pinned by cocotb: back-to-back frames need no idle gap; a
// frame cut mid-byte (line idles high) completes a garbage byte at worst
// and re-syncs on the next falling edge — the CRC gate rejects the
// garbage; ±2% baud error still lands mid-bit samples inside the cell;
// reset mid-frame returns to IDLE with no spurious data_valid.
//
// No latches, no delays, Verilog-2005.
`timescale 1ns / 1ps

module uart_rx #(
    parameter CLK_PER_BIT = 16 // oversample clocks per UART bit
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rx,
    output reg  [7:0] data, // last good byte (holds)
    output reg        data_valid, // 1-clk pulse on good stop bit
    output reg        framing_error // 1-clk pulse on bad stop bit (byte dropped)
);

  localparam [1:0] ST_IDLE = 2'd0;
  localparam [1:0] ST_START = 2'd1;
  localparam [1:0] ST_DATA = 2'd2;
  localparam [1:0] ST_STOP = 2'd3;

  localparam [11:0] HALF_BIT = CLK_PER_BIT >> 1;
  localparam [11:0] FULL_BIT = CLK_PER_BIT - 1;

  reg  [1:0] state;
  reg [11:0] cnt; // ticks since state entry / last sample
  reg  [3:0] bit_idx; // next data bit to sample (0..7)
  reg  [7:0] shift; // assembling LSB-first

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE;
      cnt <= 12'd0;
      bit_idx <= 4'd0;
      shift <= 8'd0;
      data <= 8'd0;
      data_valid <= 1'b0;
      framing_error <= 1'b0;
    end else begin
      data_valid <= 1'b0;
      framing_error <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (!rx) begin
            state <= ST_START;
            cnt <= 12'd0;
          end
        end
        ST_START: begin
          // Mid-start-bit check: still low -> real start, else glitch.
          if (cnt == HALF_BIT) begin
            if (!rx) begin
              state <= ST_DATA;
              bit_idx <= 4'd0;
              cnt <= 12'd0;
            end else begin
              state <= ST_IDLE;
              cnt <= 12'd0;
            end
          end else begin
            cnt <= cnt + 12'd1;
          end
        end
        ST_DATA: begin
          if (cnt == FULL_BIT) begin
            shift <= {rx, shift[7:1]};
            cnt <= 12'd0;
            if (bit_idx == 4'd7) begin
              state <= ST_STOP;
            end else begin
              bit_idx <= bit_idx + 4'd1;
            end
          end else begin
            cnt <= cnt + 12'd1;
          end
        end
        ST_STOP: begin
          if (cnt == FULL_BIT) begin
            if (rx) begin
              data <= shift;
              data_valid <= 1'b1;
            end else begin
              framing_error <= 1'b1;
            end
            state <= ST_IDLE;
            cnt <= 12'd0;
          end else begin
            cnt <= cnt + 12'd1;
          end
        end
        default: begin
          state <= ST_IDLE;
          cnt <= 12'd0;
        end
      endcase
    end
  end

endmodule
