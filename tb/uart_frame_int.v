// uart_frame_int.v — TEST-ONLY integration top (Week 5a2, NOT part of rtl/).
//
// Wires uart_rx -> frame_rx exactly as top.v will in a later Week 5 step,
// so the framing_error hole behavior can be proven end to end at the byte
// level: a uart_rx framing_error deletes exactly one byte from the stream;
// frame_rx sees the holed stream, rejects the straddling candidate via CRC
// and resyncs on the next SOF. Never synthesized, never shipped.
`timescale 1ns / 1ps

module uart_frame_int (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        rx,
    input  wire        cnt_clear,
    output wire [31:0] frame_counter,
    output wire [15:0] frame_v,
    output wire [15:0] frame_i,
    output wire        frame_valid,
    output wire        crc_err,
    output wire [15:0] crc_err_cnt,
    output wire [15:0] resync_cnt,
    output wire        cnt_saturated,
    output wire        framing_error,
    output wire [15:0] framing_err_cnt
);

  wire [7:0] ubyte;
  wire uvalid;

  uart_rx #(
      .CLK_PER_BIT(16)
  ) u_rx (
      .clk(clk),
      .rst_n(rst_n),
      .rx(rx),
      .cnt_clear(cnt_clear),
      .data(ubyte),
      .data_valid(uvalid),
      .framing_error(framing_error),
      .framing_err_cnt(framing_err_cnt)
  );

  frame_rx u_fr (
      .clk(clk),
      .rst_n(rst_n),
      .data_in(ubyte),
      .data_valid(uvalid),
      .cnt_clear(cnt_clear),
      .frame_counter(frame_counter),
      .frame_v(frame_v),
      .frame_i(frame_i),
      .frame_valid(frame_valid),
      .crc_err(crc_err),
      .crc_err_cnt(crc_err_cnt),
      .resync_cnt(resync_cnt),
      .cnt_saturated(cnt_saturated)
  );

endmodule
