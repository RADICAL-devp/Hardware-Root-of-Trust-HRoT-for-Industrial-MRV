"""L0 sample-frame codec: SOF + counter + V + I + CRC16 (DECISIONS.md D-01).

Wire format, little-endian, fixed 11 bytes:
    `[SOF 0xA5 u8][counter u32][V i16][I i16][CRC16 u16]`,
CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF) computed over
`counter || V || I` (bytes 1–8).

V/I codes are Q15 normalized to full-scale (`V_FS_PK_V` / `I_FS_PK_A`,
pinned exact per the Week 3 byte-layout note; identical to the Week 2
ADC full-scales). `FrameDecoder` is a stateful byte-stream
resynchroniser: it hunts for SOF, gates candidates on CRC, advances one
byte past false syncs (SOF bytes legitimately occur inside payloads),
and buffers trailing partial frames across `feed` calls.
"""

import struct
from dataclasses import dataclass, field

from sensors.adc import ADCParams

SOF = 0xA5  # [byte] start-of-frame marker
FRAME_LEN = 11  # [bytes] fixed L0 frame length

_ADC_DEFAULTS = ADCParams()
V_FS_PK_V = _ADC_DEFAULTS.v_fullscale_pk_V  # [V] Q15 voltage full-scale (exact)
I_FS_PK_A = _ADC_DEFAULTS.i_fullscale_pk_A  # [A] Q15 current full-scale (exact)


def _crc_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16_ccitt_false(data: bytes) -> int:
    """CRC16-CCITT-FALSE [u16] over `data` (check: b"123456789" → 0x29B1)."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) ^ _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]) & 0xFFFF
    return crc


def q15_encode(x: float, fs_pk: float) -> int:
    """Quantize physical value [V]/[A] to Q15 [counts]; saturates, never wraps."""
    return max(-32768, min(32767, int(round(x / fs_pk * 32768.0))))


def q15_decode(code: int, fs_pk: float) -> float:
    """Convert Q15 [counts] back to physical units [V]/[A]."""
    return (int(code) / 32768.0) * fs_pk


@dataclass(frozen=True)
class Frame:
    """One decoded L0 sample frame (host-side units documented per field)."""

    counter: int  # [counts] monotonic u32 sample counter
    v_q15: int  # [counts] Q15 voltage code
    i_q15: int  # [counts] Q15 current code


def encode_frame(counter: int, v_q15: int, i_q15: int) -> bytes:
    """Encode one 11-byte L0 frame; out-of-range fields raise (strict, fail loud)."""
    if not 0 <= int(counter) <= 0xFFFFFFFF:
        raise ValueError(f"counter out of u32 range: {counter}")
    for name, code in (("V", v_q15), ("I", i_q15)):
        if not -32768 <= int(code) <= 32767:
            raise ValueError(f"{name} out of i16 range: {code}")
    body = struct.pack("<Ihh", int(counter), int(v_q15), int(i_q15))
    return struct.pack("<B", SOF) + body + struct.pack("<H", crc16_ccitt_false(body))


def decode_frame(buf: bytes, offset: int = 0) -> Frame | None:
    """Decode one frame at `offset`; None if short, unsynced or CRC-bad."""
    if len(buf) - offset < FRAME_LEN or buf[offset] != SOF:
        return None
    body = bytes(buf[offset + 1 : offset + 9])
    (counter, v_q15, i_q15) = struct.unpack("<Ihh", body)
    (crc,) = struct.unpack("<H", bytes(buf[offset + 9 : offset + 11]))
    if crc != crc16_ccitt_false(body):
        return None
    return Frame(counter=counter, v_q15=v_q15, i_q15=i_q15)


@dataclass
class FrameDecoder:
    """Stateful resynchronising L0 decoder; `feed` returns new valid frames."""

    _buf: bytearray = field(default_factory=bytearray, repr=False)
    frames: int = 0  # [counts] accepted frames lifetime
    crc_fails: int = 0  # [counts] SOF candidates rejected by CRC lifetime
    bytes_seen: int = 0  # [bytes] total bytes fed lifetime

    def feed(self, data: bytes) -> list[Frame]:
        """Consume bytes; return complete valid frames (tail stays buffered)."""
        self._buf += data
        self.bytes_seen += len(data)
        out: list[Frame] = []
        buf = self._buf
        while True:
            sof = buf.find(SOF.to_bytes(1, "little"))
            if sof < 0:  # no sync marker: drop pure garbage
                del buf[:]
                return out
            if sof > 0:  # skip garbage preceding the marker
                del buf[:sof]
            if len(buf) < FRAME_LEN:  # trailing partial frame: wait for more
                return out
            frame = decode_frame(buf, 0)
            if frame is None:
                self.crc_fails += 1
                del buf[:1]  # false sync (e.g. SOF inside payload): advance one byte
                continue
            out.append(frame)
            self.frames += 1
            del buf[:FRAME_LEN]
