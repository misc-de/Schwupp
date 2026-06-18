#!/usr/bin/env python3
"""Eigenständiger PoC für den nativen Cast-Streaming-Mirror.

Wartet, bis der LG-Cast-Receiver (Port 8009) wach ist, baut dann den
Offer/Answer-Handshake auf und streamt den Bildschirm nativ per Cast-RTP
(H.264, AES-128-CTR) an den ausgehandelten UDP-Port.

So weckst du den Cast-Receiver, falls 8009 zu ist:
  - von einem Android-Handy/Chrome-Browser einmal etwas auf den LG casten, oder
  - den TV aktiv benutzen (eine App öffnen).

Aufruf:
  DISPLAY=:0.0 .venv/bin/python tools/native_mirror_poc.py
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

import pychromecast  # noqa: E402

from app.mirror.cast_streaming import rtp  # noqa: E402
from app.mirror.cast_streaming.control import CastStreamingControl, video_stream  # noqa: E402
from app.mirror.cast_streaming.crypto import encrypt_frame  # noqa: E402

TV = "192.168.0.33"
W, H, FPS, BR = 1280, 720, 30, 4_000_000
VSSRC, VPT = 100001, 96


def wait_for_cast(host: str, port: int = 8009) -> None:
    print(f"Warte auf Cast-Receiver {host}:{port} … (TV wecken: casten/App öffnen)")
    while True:
        try:
            socket.create_connection((host, port), timeout=2).close()
            print("  -> 8009 offen!")
            return
        except OSError:
            time.sleep(3)
            print("  …", end="", flush=True)


def main() -> None:
    wait_for_cast(TV)
    Gst.init(None)
    cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
    cc.wait(timeout=15)
    ctrl = CastStreamingControl()
    cc.register_handler(ctrl)
    print("Mirroring-App starten + OFFER …")
    ctrl.launch()
    time.sleep(3)
    vkey, viv = os.urandom(16), os.urandom(16)
    ctrl.send_offer(video_stream(0, VSSRC, vkey.hex(), viv.hex(), W, H, FPS, BR))
    ans = ctrl.wait_answer(12)
    print("ANSWER:", ans)
    if not ans or "udpPort" not in ans:
        print("Kein gültiges ANSWER – Abbruch."); return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("", 0))
    dest = (TV, ans["udpPort"])

    desc = (
        f"ximagesrc use-damage=false ! video/x-raw,framerate={FPS}/1 ! videoscale add-borders=true "
        f"! videoconvert ! video/x-raw,width={W},height={H} "
        f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={BR // 1000} key-int-max={FPS * 2} "
        f"! video/x-h264,profile=main,stream-format=byte-stream ! h264parse config-interval=-1 "
        f"! appsink name=s emit-signals=true sync=false max-buffers=2 drop=true"
    )
    pipe = Gst.parse_launch(desc)
    st = {"fid": 1, "seq": 0, "pk": 0}

    def on_sample(s):
        sample = s.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(mi.data)
        finally:
            buf.unmap(mi)
        is_key = not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        fid = st["fid"]
        rtp_ts = int((buf.pts or 0) * 9 // 100000) & 0xFFFFFFFF
        enc = encrypt_frame(data, fid, vkey, viv)
        pk, st["seq"] = rtp.packetize(
            payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid - 1,
            ssrc=VSSRC, payload_type=VPT, rtp_timestamp=rtp_ts, seq=st["seq"])
        for p in pk:
            sock.sendto(p, dest)
        st["pk"] += len(pk); st["fid"] = fid + 1
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    print(">>> streame nativen RTP-Mirror – Bild am TV? (25 s)")
    try:
        time.sleep(25)
    finally:
        print(f"gesendet: {st['fid'] - 1} Frames / {st['pk']} Pakete")
        pipe.set_state(Gst.State.NULL)
        cc.quit_app()


if __name__ == "__main__":
    main()
