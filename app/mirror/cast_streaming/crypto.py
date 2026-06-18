"""AES-128-CTR-Verschlüsselung der Cast-Streaming-Frame-Payloads.

Nonce-Berechnung exakt wie openscreen ``cast/streaming/impl/frame_crypto.cc``:
  1. 16 Null-Bytes
  2. frame_id (untere 32 Bit) als Big-Endian an Offset 8 (Bytes 8..11)
  3. XOR mit dem 16-Byte iv_mask (aus dem OFFER)
Der so entstehende 16-Byte-Block ist der Start-Counter für AES-128-CTR.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def frame_nonce(frame_id: int, iv_mask: bytes) -> bytes:
    nonce = bytearray(16)
    nonce[8:12] = (frame_id & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(n ^ m for n, m in zip(nonce, iv_mask))


def encrypt_frame(data: bytes, frame_id: int, key: bytes, iv_mask: bytes) -> bytes:
    """Verschlüsselt eine komplette Frame-Payload mit AES-128-CTR."""
    cipher = Cipher(algorithms.AES(key), modes.CTR(frame_nonce(frame_id, iv_mask)))
    enc = cipher.encryptor()
    return enc.update(data) + enc.finalize()
