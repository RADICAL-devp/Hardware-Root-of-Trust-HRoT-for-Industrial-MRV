// sqrt_fsm.v — iterative restoring square root with half-up rounding (Week 5b).
//
// Bit-equals sensors.windows.isqrt_round_half_up (hence tb/golden.py):
// yields (root, remainder), rounds up iff rem > root.
//
// Interface: start sampled at edge E latches x[63:0]; done rises at edge
// E+33 (34 inclusive cycles: 1 load + 32 iterate + 1 finalize) with the
// rounded root held on root[32:0].
//
// The port is 33 bits wide because isqrt(2^64-1) rounds to 2^32, which a
// 32-bit port would wrap to 0. Call-site inputs (half-away means of
// squares) never exceed 2^30, so the controller saturates to u16 as
// before; the unit itself is exact over the full 64-bit input range.
//
// No latches, no delays, Verilog-2005. Explicit shifts/compares only.
`timescale 1ns / 1ps

module sqrt_fsm (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start, // 1-cycle: latch x, begin
    input  wire [63:0] x, // radicand, full 64-bit range
    output reg         done, // 1-cycle pulse with the result
    output reg  [32:0] root // rounded root, held (33 bits: max 2^32)
);

  localparam [1:0] ST_IDLE = 2'd0;
  localparam [1:0] ST_ITER = 2'd1;
  localparam [1:0] ST_FIN = 2'd2;

  reg  [1:0] state;
  reg [63:0] x_sh; // remaining radicand bits, consumed top-down
  reg [31:0] root_r; // floor root under construction
  reg [63:0] rem;
  reg  [5:0] cnt; // iterations remaining (32..0)

  // Restoring step: bring down the top 2 unconsumed bits, then compare
  // against trial = (root<<2)|1. All widths constant (no part-selects).
  wire [63:0] rem2 = {rem[61:0], x_sh[63:62]};
  wire [63:0] trial = {30'd0, root_r, 2'b01};
  wire take = (rem2 >= trial);
  wire [63:0] next_rem = take ? rem2 - trial : rem2;
  wire [31:0] next_root = {root_r[30:0], take};

  // Half-up: rem > root -> +1 (33-bit sum, never wraps: root <= 2^32-1).
  wire [32:0] r_rounded = (rem > {32'd0, root_r}) ? ({1'b0, root_r} + 33'd1)
                                                         : {1'b0, root_r};

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE;
      x_sh <= 64'd0;
      root_r <= 32'd0;
      rem <= 64'd0;
      cnt <= 6'd0;
      done <= 1'b0;
      root <= 33'd0;
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (start) begin
            x_sh <= x;
            root_r <= 32'd0;
            rem <= 64'd0;
            cnt <= 6'd32;
            state <= ST_ITER;
          end
        end
        ST_ITER: begin
          rem <= next_rem;
          root_r <= next_root;
          x_sh <= x_sh << 2;
          if (cnt == 6'd1) begin
            state <= ST_FIN;
          end
          cnt <= cnt - 6'd1;
        end
        ST_FIN: begin
          root <= r_rounded;
          done <= 1'b1;
          state <= ST_IDLE;
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
