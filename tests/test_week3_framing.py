"""Week 3: L0 frame layout, CRC variant, Q15 codec and byte-stream resync.

Frozen per DECISIONS.md D-01: little-endian, CRC16-CCITT-FALSE, 11-byte
`[SOF 0xA5][counter u32][V i16][I i16][CRC16 u16]`, CRC over
`counter || V || I`.
"""

import struct

from edge.framing import (
    FRAME_LEN,
    SOF,
    Frame,
    FrameDecoder,
    crc16_ccitt_false,
    decode_frame,
    encode_frame,
    q15_decode,
    q15_encode,
)


def test_crc16_known_vector():
    # CRC16-CCITT-FALSE check value for ASCII "123456789" is 0x29B1.
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    assert crc16_ccitt_false(b"") == 0xFFFF


def test_frame_layout_is_11_bytes():
    raw = encode_frame(0x01020304, 1000, -2000)
    assert len(raw) == FRAME_LEN == 11
    assert raw[0] == SOF == 0xA5
    assert raw[1:5] == struct.pack("<I", 0x01020304)  # little-endian counter
    assert raw[5:7] == struct.pack("<h", 1000)
    assert raw[7:9] == struct.pack("<h", -2000)
    body = raw[1:9]
    assert raw[9:11] == struct.pack("<H", crc16_ccitt_false(body))
    frame = decode_frame(raw, 0)
    assert frame == Frame(counter=0x01020304, v_q15=1000, i_q15=-2000)


def test_q15_roundtrip_within_half_lsb():
    for x, fs in ((230.0, 500.0), (-31.0, 100.0), (0.0, 500.0)):
        code = q15_encode(x, fs)
        assert -32768 <= code <= 32767
        assert abs(q15_decode(code, fs) - x) <= fs / 32768.0 / 2.0 + 1e-12
    # Saturation, no wrap-around.
    assert q15_encode(10_000.0, 500.0) == 32767
    assert q15_encode(-10_000.0, 500.0) == -32768


def test_resync_after_garbage_and_lost_byte():
    frames = [encode_frame(k, 100 * k, -50 * k) for k in range(10)]
    stream = bytearray(b"\x00\xa5\xff\xa5\xa5\xa5garbage!")
    for k, f in enumerate(frames):
        stream += f
    # Lose one payload byte inside frame 4: it must die, the rest survive.
    lost_at = len(b"\x00\xa5\xff\xa5\xa5\xa5garbage!") + 4 * FRAME_LEN + 3
    del stream[lost_at]
    dec = FrameDecoder()
    got = []
    for off in range(0, len(stream), 7):  # odd chunk sizes stress buffering
        got += dec.feed(bytes(stream[off : off + 7]))
    assert [f.counter for f in got] == [0, 1, 2, 3, 5, 6, 7, 8, 9]


def test_sof_byte_inside_payload_does_not_confuse_decoder():
    # 0xA5 appears in V/I codes; only true SOF positions decode.
    frames = [encode_frame(k, 0x25A5 - 2 * k, -0x5B60 + k) for k in range(5)]
    assert any(b"\xa5" in f[1:9] for f in frames)  # precondition: SOF in payload
    stream = b"".join(frames)
    dec = FrameDecoder()
    got = dec.feed(stream[:13])
    got += dec.feed(stream[13:])  # split mid-frame
    assert [f.counter for f in got] == [0, 1, 2, 3, 4]
    assert got[0].v_q15 == 0x25A5


def test_garbage_prefix_and_truncated_tail_resynchronise():
    frames = [encode_frame(k, k, -k) for k in range(3)]
    tail = encode_frame(99, 7, -7)
    stream = b"\xa5\xa5\xa5\x00\xff" + b"".join(frames) + tail[:5]
    dec = FrameDecoder()
    got = dec.feed(stream)
    assert [f.counter for f in got] == [0, 1, 2]  # 5-byte tail buffered, not emitted
    got += dec.feed(tail[5:])  # completing the tail emits frame 99
    assert [f.counter for f in got] == [0, 1, 2, 99]
