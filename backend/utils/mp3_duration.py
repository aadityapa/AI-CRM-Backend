"""Length of an MP3 stream from its frame headers — no decoder, no I/O (28 Sep 2026).

The TTS API returns audio bytes and no usage figures, and the CEO's cost page
prices spoken questions per minute. Guessing the length from the text
(`ai_pricing.TTS_CHARS_PER_SECOND`) was ±30 %; summing the frames is exact for
CBR and VBR alike, because every MPEG audio frame carries its own bitrate and
sample rate. Works on a partial stream too (a clip that was cut off mid-flight
is priced for what was actually produced).
"""
from __future__ import annotations

# Bitrate (kbps) tables, index = the 4-bit header field. Column: MPEG-1 Layer III
# and MPEG-2/2.5 Layer III (OpenAI's TTS is Layer III at 24 kHz, MPEG-2).
_BITRATES_L3 = {
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),   # MPEG-1
    2: (22050, 24000, 16000),   # MPEG-2
    0: (11025, 12000, 8000),    # MPEG-2.5
}


def _id3v2_size(data: bytes) -> int:
    if len(data) >= 10 and data[:3] == b"ID3":
        b = data[6:10]
        return 10 + ((b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 | (b[2] & 0x7F) << 7 | (b[3] & 0x7F))
    return 0


def mp3_duration_seconds(data: bytes) -> float:
    """Seconds of audio in `data`, 0.0 when nothing parseable. Pure."""
    if not data:
        return 0.0
    i = _id3v2_size(data)
    n = len(data)
    seconds = 0.0
    while i + 4 <= n:
        b0, b1, b2 = data[i], data[i + 1], data[i + 2]
        if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
            i += 1
            continue
        version_bits = (b1 >> 3) & 0x03      # 0 = MPEG-2.5, 2 = MPEG-2, 3 = MPEG-1
        layer_bits = (b1 >> 1) & 0x03        # 1 = Layer III
        if version_bits == 1 or layer_bits != 1:
            i += 1
            continue
        bitrate_idx = (b2 >> 4) & 0x0F
        sr_idx = (b2 >> 2) & 0x03
        padding = (b2 >> 1) & 0x01
        if bitrate_idx in (0, 15) or sr_idx == 3:
            i += 1
            continue
        table = _BITRATES_L3[1 if version_bits == 3 else 2]
        bitrate = table[bitrate_idx] * 1000
        sample_rate = _SAMPLE_RATES[version_bits][sr_idx]
        samples = 1152 if version_bits == 3 else 576
        frame_len = int(samples // 8 * bitrate / sample_rate) + padding
        if frame_len <= 0:
            i += 1
            continue
        seconds += samples / sample_rate
        i += frame_len
    return round(seconds, 3)
