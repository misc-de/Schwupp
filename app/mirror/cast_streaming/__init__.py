"""Natives Cast-Streaming (Low-Latency-Mirroring) – Sender-Implementierung.

Bausteine (nachgebaut nach openscreen cast/streaming):
  crypto    – AES-128-CTR-Verschlüsselung der Frame-Payload (Nonce je frame_id)
  rtp       – Cast-RTP-Packetizer (Frame -> UDP-Pakete)
  control   – Offer/Answer-Handshake über urn:x-cast:com.google.cast.webrtc
  session   – fügt Encoder + Crypto + RTP + RTCP + UDP zur Streaming-Sitzung
"""
