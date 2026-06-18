#!/usr/bin/env python3
"""Nativer Cast-Mirror MIT RTCP-Sender-Reports (Latenz-Fix).

RTP-Timestamps und RTCP-SR teilen sich dieselbe monotone Uhr; das SR mappt
diese auf die Wall-Clock (NTP), damit der Receiver die Frames mit dem
ausgehandelten targetDelay (statt mit großem Default-Puffer) abspielt.
"""
import os, socket, struct, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pychromecast
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from app.mirror.cast_streaming import rtp
from app.mirror.cast_streaming.control import CastStreamingControl, video_stream
from app.mirror.cast_streaming.crypto import encrypt_frame

TV = "192.168.0.33"
W, H, FPS, BR = 1280, 720, 30, 4_000_000
VSSRC, VPT = 100001, 96
NTP_EPOCH = 2208988800  # 1900->1970


def ntp_now():
    t = time.time()
    return int(t) + NTP_EPOCH, int((t % 1) * (1 << 32)) & 0xFFFFFFFF


def build_sr(ssrc, rtp_ts, pkts, octets):
    sec, frac = ntp_now()
    # V=2,P=0,RC=0 -> 0x80 ; PT=200 (SR) ; length=6 (28 Bytes)
    return struct.pack("!BBHIIIIII", 0x80, 200, 6, ssrc, sec, frac,
                       rtp_ts & 0xFFFFFFFF, pkts & 0xFFFFFFFF, octets & 0xFFFFFFFF)


def main():
    try:
        socket.create_connection((TV, 8009), timeout=3).close()
    except OSError:
        print("8009 zu – TV/Cast wecken und erneut starten."); return
    Gst.init(None)
    cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
    cc.wait(timeout=15)
    ctrl = CastStreamingControl(); cc.register_handler(ctrl)
    print(">>> Mirroring-App + OFFER", flush=True)
    ctrl.launch(); time.sleep(3)
    vkey, viv = os.urandom(16), os.urandom(16)
    ctrl.send_offer(video_stream(0, VSSRC, vkey.hex(), viv.hex(), W, H, FPS, BR, target_delay=600))
    ans = ctrl.wait_answer(12)
    print(">>> ANSWER:", ans, flush=True)
    if not ans or "udpPort" not in ans:
        print("kein udpPort"); return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("", 0))
    dest = (TV, ans["udpPort"])

    mono0 = time.monotonic()
    st = {"fid": 1, "seq": 0, "pk": 0, "oct": 0, "run": True}

    def rtp_ts_now():
        return int((time.monotonic() - mono0) * 90000) & 0xFFFFFFFF

    # RTCP-SR periodisch (alle 0.5 s) auf denselben Port (rtcp-mux)
    def rtcp_loop():
        while st["run"]:
            sock.sendto(build_sr(VSSRC, rtp_ts_now(), st["pk"], st["oct"]), dest)
            time.sleep(0.5)
    threading.Thread(target=rtcp_loop, daemon=True).start()

    desc = (f"ximagesrc use-damage=false ! video/x-raw,framerate={FPS}/1 ! videoscale add-borders=true "
            f"! videoconvert ! video/x-raw,width={W},height={H} "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={BR//1000} key-int-max={FPS} "
            f"! video/x-h264,profile=main,stream-format=byte-stream ! h264parse config-interval=-1 "
            f"! appsink name=s emit-signals=true sync=false max-buffers=1 drop=true")
    pipe = Gst.parse_launch(desc)

    def on_sample(s):
        sm = s.emit("pull-sample")
        if not sm: return Gst.FlowReturn.OK
        buf = sm.get_buffer(); ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok: return Gst.FlowReturn.OK
        try: data = bytes(mi.data)
        finally: buf.unmap(mi)
        is_key = not (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        fid = st["fid"]; ts = rtp_ts_now()
        enc = encrypt_frame(data, fid, vkey, viv)
        pk, st["seq"] = rtp.packetize(payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid-1,
                                      ssrc=VSSRC, payload_type=VPT, rtp_timestamp=ts, seq=st["seq"])
        for p in pk:
            sock.sendto(p, dest); st["oct"] += len(p) - 12
        st["pk"] += len(pk); st["fid"] = fid + 1
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    print(">>> nativer RTP+RTCP-Mirror läuft – Latenz jetzt? (35 s, Maus bewegen)", flush=True)
    time.sleep(35)
    st["run"] = False
    print(f">>> gesendet: {st['fid']-1} Frames / {st['pk']} Pakete", flush=True)
    pipe.set_state(Gst.State.NULL)
    try: cc.quit_app()
    except Exception: pass


if __name__ == "__main__":
    main()
