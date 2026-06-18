#!/usr/bin/env python3
"""Nativer Cast-Mirror: RTP-Stream zuerst, dann SR dazuschalten.

Phase 1 (0-8s): nur RTP (buf.pts-Timestamps) → Bild baut sich mit ~8s Puffer auf.
Phase 2 (ab 8s): RTCP-Sender-Reports dazu → soll den Puffer auf targetDelay drücken.
Schneidet das Receiver-RTCP (PT=201 RR / 207 XR) mit, um die Session zu prüfen.
"""
import os, socket, struct, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pychromecast, gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from app.mirror.cast_streaming import rtp
from app.mirror.cast_streaming.control import CastStreamingControl, video_stream
from app.mirror.cast_streaming.crypto import encrypt_frame

TV = "192.168.0.33"
W, H, FPS, BR = 1280, 720, 30, 4_000_000
VSSRC, VPT = 100001, 96
NTP_EPOCH = 2208988800
PHASE1 = 8       # s RTP-only
PHASE2 = 27      # s mit SR


def main():
    try:
        socket.create_connection((TV, 8009), timeout=3).close()
    except OSError:
        print("8009 zu – TV/Cast wecken."); return
    Gst.init(None)
    cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
    cc.wait(timeout=15)
    ctrl = CastStreamingControl(); cc.register_handler(ctrl); ctrl.launch(); time.sleep(3)
    vkey, viv = os.urandom(16), os.urandom(16)
    ctrl.send_offer(video_stream(0, VSSRC, vkey.hex(), viv.hex(), W, H, FPS, BR, target_delay=400))
    ans = ctrl.wait_answer(12); print("ANSWER:", ans, flush=True)
    if not ans or "udpPort" not in ans:
        print("kein udpPort"); return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("", 0))
    dest = (TV, ans["udpPort"])
    T0 = time.time()
    st = {"fid": 1, "seq": 0, "pk": 0, "oct": 0, "run": True, "sr": False, "fb": 0}

    def rx():
        sock.settimeout(0.4)
        while st["run"]:
            try:
                d, _ = sock.recvfrom(2048); st["fb"] += 1
            except (socket.timeout, OSError):
                pass
    threading.Thread(target=rx, daemon=True).start()

    def srloop():
        while st["run"]:
            # SR an den ZULETZT gesendeten Frame koppeln: gleiche Uhr wie die RTP-ts!
            if st["sr"] and st.get("last_wall"):
                w = st["last_wall"]; sec = int(w) + NTP_EPOCH; frac = int((w % 1) * (1 << 32)) & 0xFFFFFFFF
                sr = struct.pack("!BBHIIIIII", 0x80, 200, 6, VSSRC, sec, frac, st["last_rtp"] & 0xFFFFFFFF,
                                 st["pk"] & 0xFFFFFFFF, st["oct"] & 0xFFFFFFFF)
                sock.sendto(sr, dest)
            time.sleep(0.2)
    threading.Thread(target=srloop, daemon=True).start()

    desc = (f"ximagesrc use-damage=false ! video/x-raw,framerate={FPS}/1 ! videoscale add-borders=true ! videoconvert "
            f"! video/x-raw,width={W},height={H} ! x264enc tune=zerolatency speed-preset=ultrafast bitrate={BR//1000} key-int-max={FPS} "
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
        fid = st["fid"]; ts = int((buf.pts or 0) * 9 // 100000) & 0xFFFFFFFF
        enc = encrypt_frame(data, fid, vkey, viv)
        pk, st["seq"] = rtp.packetize(payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid-1,
                                      ssrc=VSSRC, payload_type=VPT, rtp_timestamp=ts, seq=st["seq"])
        for p in pk: sock.sendto(p, dest); st["oct"] += len(p) - 12
        st["pk"] += len(pk); st["fid"] = fid + 1
        st["last_rtp"] = ts; st["last_wall"] = time.time()  # für SR-Korrelation
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    print(f">>> PHASE 1: nur RTP ({PHASE1}s) – Bild erscheint (mit ~8s Verzögerung)", flush=True)
    time.sleep(PHASE1)
    print(">>> PHASE 2: ===== SR EIN ===== jetzt auf Latenz achten! (Maus bewegen)", flush=True)
    st["sr"] = True
    for i in range(PHASE2):
        time.sleep(1)
        if i % 5 == 4:
            print(f"    [{i+1}s nach SR] Receiver-Feedback-Pakete: {st['fb']}", flush=True)
    st["run"] = False
    print(f">>> Ende. Frames={st['fid']-1}, Feedback={st['fb']}", flush=True)
    pipe.set_state(Gst.State.NULL)
    try: cc.quit_app()
    except Exception: pass


if __name__ == "__main__":
    main()
