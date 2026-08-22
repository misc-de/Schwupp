"""AES-128-CTR-Frame-Verschlüsselung (openscreen frame_crypto.cc-kompatibel)."""
from __future__ import annotations

from app.mirror.cast_streaming.crypto import encrypt_frame, frame_nonce


def test_nonce_places_frame_id_at_offset_8():
    nonce = frame_nonce(0x01020304, bytes(16))
    assert nonce == bytes(8) + b"\x01\x02\x03\x04" + bytes(4)


def test_nonce_is_xored_with_the_iv_mask():
    mask = bytes(range(16))
    assert frame_nonce(0, mask) == mask
    nonce = frame_nonce(0xFF, mask)
    expected = bytearray(mask)
    expected[11] ^= 0xFF
    assert nonce == bytes(expected)


def test_nonce_truncates_frame_id_to_32_bit():
    assert frame_nonce(0x1_0000_0000, bytes(16)) == frame_nonce(0, bytes(16))


def test_encryption_round_trips():
    key, iv = bytes(range(16)), bytes(range(16, 32))
    plain = b"the quick brown fox" * 10
    cipher = encrypt_frame(plain, 42, key, iv)
    assert cipher != plain
    assert len(cipher) == len(plain)
    # CTR ist symmetrisch: nochmal durch dieselbe Operation ergibt den Klartext.
    assert encrypt_frame(cipher, 42, key, iv) == plain


def test_different_frames_get_different_keystreams():
    key, iv = bytes(16), bytes(16)
    plain = b"A" * 32
    assert encrypt_frame(plain, 1, key, iv) != encrypt_frame(plain, 2, key, iv)


def test_empty_payload():
    assert encrypt_frame(b"", 0, bytes(16), bytes(16)) == b""
