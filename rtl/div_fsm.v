// div_fsm.v — iterative restoring divider with half-away rounding (Week 5b).
//
// Bit-equals sensors.windows.div_round_half_away (hence tb/golden.py):
// sign-magnitude core, exact halves round away from zero.
//
// Interface: start sampled at edge E latches (neg, mag, den); done rises
// at edge E+65 (66 inclusive cycles: 1 load + 64 iterate + 1 finalize) with
// the signed quotient held on quot[63:0].
//
// Contract: den > 0 required (the power_calc controller guarantees it:
// invalid windows force den 1 and zero the outputs); |N| < 2^63 required
// (controller call sites stay < 2^56 — see DECISIONS.md bounds table).
// Under the contract the rounding increment cannot overflow: rem > 0 at
// increment time implies quot_mag <= 2^64-2, and the signed result fits
// because |N| <= 2^63-1 with den >= 1.
//
// Half-away without a wide multiply: 2*rem >= den  <=>  rem >= ceil(den/2)
// with ceil(den/2) = (den>>1) + den[0] (no overflow at any width).
//
// No latches, no delays, Verilog-2005. Explicit shifts/compares only.
`timescale 1ns / 1ps

module div_fsm (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start, // 1-cycle: latch operands, begin
    input  wire        neg, // result negative (sign-magnitude in)
    input  wire [63:0] mag, // |numerator|, full 64-bit magnitude path
    input  wire [63:0] den, // denominator, must be > 0
    output reg         done, // 1-cycle pulse with the result
    output reg  [63:0] quot // signed quotient, held
);

  localparam [1:0] ST_IDLE = 2'd0;
  localparam [1:0] ST_ITER = 2'd1;
  localparam [1:0] ST_FIN = 2'd2;

  reg  [1:0] state;
  reg        neg_r;
  reg [63:0] den_r;
  reg [63:0] rem;
  reg [63:0] quo;
  reg  [6:0] cnt; // iterations remaining (64..0)

  // Restoring shift: {rem, quo} << 1 needs 129 bits so rem's top bit
  // survives (rem < den <= 2^64-1, so rem<<1 can need 65 bits).
  wire [128:0] shifted = {1'b0, rem, quo} << 1;
  wire [64:0] sh_rem = shifted[128:64]; // 65-bit shifted remainder
  wire [63:0] sh_quo = shifted[63:0]; // quo<<1, LSB filled below
  wire [64:0] den65 = {1'b0, den_r};
  wire take = (sh_rem >= den65);
  wire [63:0] next_rem = take ? sh_rem[63:0] - den_r : sh_rem[63:0];
  // NOTE: sh_rem < 2*den when take is false... when take is true,
  // sh_rem - den < den <= 2^64-1, so [63:0] never truncates: take=true
  // implies sh_rem < 2*den (shifted rem < 2*den always? rem_old < den
  // invariant: sh_rem = 2*rem_old + bit <= 2*(den-1)+1 = 2*den-1, so
  // sh_rem - den <= den-1. take=false gives sh_rem < den.) Invariant
  // rem < den holds by induction (base rem=0).
  wire [63:0] next_quo = {sh_quo[63:1], take};

  // Half-away threshold: ceil(den/2), exact at all widths.
  wire [63:0] half_ceil = (den_r >> 1) + {63'd0, den_r[0]};
  wire round_up = (rem >= half_ceil);
  wire [63:0] q_rounded = round_up ? quo + 64'd1 : quo;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE;
      neg_r <= 1'b0;
      den_r <= 64'd0;
      rem <= 64'd0;
      quo <= 64'd0;
      cnt <= 7'd0;
      done <= 1'b0;
      quot <= 64'd0;
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (start) begin
            neg_r <= neg;
            den_r <= den;
            rem <= 64'd0;
            quo <= mag;
            cnt <= 7'd64;
            state <= ST_ITER;
          end
        end
        ST_ITER: begin
          rem <= next_rem;
          quo <= next_quo;
          if (cnt == 7'd1) begin
            state <= ST_FIN;
          end
          cnt <= cnt - 7'd1;
        end
        ST_FIN: begin
          quot <= neg_r ? (~q_rounded + 64'd1) : q_rounded;
          done <= 1'b1;
          state <= ST_IDLE;
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
