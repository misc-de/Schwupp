"""Offer/Answer-Handshake für Cast-Streaming (urn:x-cast:com.google.cast.webrtc).

Startet die Mirroring-Receiver-App, sendet ein OFFER mit Stream-Definitionen
(Codec, SSRC, AES-Key/IV) und empfängt das ANSWER (udpPort, akzeptierte Streams).
"""
from __future__ import annotations

import threading

from pychromecast.controllers import BaseController

WEBRTC_NAMESPACE = "urn:x-cast:com.google.cast.webrtc"
MIRRORING_APP_ID = "674A0243"   # "Screen Mirroring" (alternativ 0F5096E8)


class CastStreamingControl(BaseController):
    def __init__(self) -> None:
        super().__init__(WEBRTC_NAMESPACE, MIRRORING_APP_ID)
        self._answer_event = threading.Event()
        self._answer: dict | None = None
        self._seq = 0

    def receive_message(self, message, data) -> bool:  # noqa: ANN001
        if data.get("type") == "ANSWER" and data.get("result") == "ok":
            self._answer = data.get("answer")
            self._answer_event.set()
        elif data.get("type") == "ANSWER":
            self._answer = {"error": data}
            self._answer_event.set()
        return True

    def send_offer(self, video: dict, audio: dict | None = None) -> None:
        """Sendet ein OFFER. *video*/*audio* sind die Stream-Definitionen."""
        self._seq += 1
        streams = [video] + ([audio] if audio else [])
        self.send_message({
            "type": "OFFER",
            "seqNum": self._seq,
            "offer": {
                "castMode": "mirroring",
                "receiverGetStatus": True,
                "supportedStreams": streams,
            },
        })

    def wait_answer(self, timeout: float = 12.0) -> dict | None:
        return self._answer if self._answer_event.wait(timeout) else None


def video_stream(index: int, ssrc: int, aes_key_hex: str, aes_iv_hex: str,
                 width: int, height: int, fps: int, bitrate: int,
                 target_delay: int = 400) -> dict:
    return {
        "index": index, "type": "video_source", "codecName": "h264",
        "rtpProfile": "cast", "rtpPayloadType": 96, "ssrc": ssrc,
        "maxFrameRate": str(fps), "timeBase": "1/90000", "maxBitRate": bitrate,
        "profile": "main", "level": "4", "aesKey": aes_key_hex, "aesIvMask": aes_iv_hex,
        "resolutions": [{"width": width, "height": height}],
        "receiverRtcpEventLog": True, "targetDelay": target_delay,
    }


def audio_stream(index: int, ssrc: int, aes_key_hex: str, aes_iv_hex: str) -> dict:
    return {
        "index": index, "type": "audio_source", "codecName": "opus",
        "rtpProfile": "cast", "rtpPayloadType": 127, "ssrc": ssrc,
        "bitRate": 128000, "timeBase": "1/48000", "channels": 2,
        "aesKey": aes_key_hex, "aesIvMask": aes_iv_hex,
        "receiverRtcpEventLog": True, "targetDelay": 200,
    }
