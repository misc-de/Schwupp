#!/usr/bin/env python3
"""Nativer Cast-Mirror v2 — LOW LATENCY (<1s), live bestätigt.

Die drei entscheidenden Bausteine (hart erarbeitet):
  1. frame_id 0-BASIERT, erstes gesendetes Frame MUSS ein Keyframe sein
     (Receiver wartet sonst ewig auf das nie existierende Frame 0 → ACK bleibt 0xff).
  2. RTCP Sender Report (PT=200) mit Uhr, die an den ZULETZT gesendeten Frame
     gekoppelt ist (gleiche Zeitbasis wie die RTP-Timestamps).
  3. Retransmission: Receiver fordert fehlende Pakete per Cast-NACK (PT=206,
     magic 'CAST') an; ohne erneutes Senden bleibt der Decoder stehen.

Erfolgssignal (rein datengetrieben): ACK_frame zählt mit der gesendeten
frame_id hoch (mod 256), loss bleibt 0.
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
DURATION = 40
TARGET_DELAY = int(sys.argv[1]) if len(sys.argv) > 1 else 150   # ms, Playout-Puffer


def walk(d):
    off = 0; out = []
    while off + 4 <= len(d):
        pt = d[off+1]; ln = struct.unpack("!H", d[off+2:off+4])[0]; size = (ln+1)*4
        if size <= 0 or off+size > len(d): break
        out.append((pt, off, size)); off += size
    return out


def parse_nacks(d):
    for pt, off, size in walk(d):
        if pt == 206 and d[off+12:off+16] == b'CAST':
            fci = d[off+12:off+size]; ack = fci[4]; lc = fci[5]; fields = []
            o = 8
            while o + 4 <= len(fci):
                fields.append((fci[o], struct.unpack("!H", fci[o+1:o+3])[0], fci[o+3])); o += 4
            return ack, lc, fields
    return None


def ntp_to_unix(n):
    return (n >> 32) - NTP_EPOCH + ((n & 0xFFFFFFFF) / (1 << 32))


def find_xr_reftime(d):  # Receiver Reference Time (BT=4) -> 64bit-NTP der TV-Uhr
    for pt, off, size in walk(d):
        if pt == 207:
            o = off + 8
            while o + 4 <= off + size:
                bt = d[o]; blen = struct.unpack("!H", d[o+2:o+4])[0]
                if bt == 4 and o + 12 <= len(d):
                    return struct.unpack("!Q", d[o+4:o+12])[0]
                o += 4 + blen * 4
    return None


def main():
    try:
        socket.create_connection((TV, 8009), timeout=3).close()
    except OSError:
        print("8009 zu – Cast-Receiver wecken."); return
    Gst.init(None)
    cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
    cc.wait(timeout=15)
    ctrl = CastStreamingControl(); cc.register_handler(ctrl); ctrl.launch(); time.sleep(3)
    vkey, viv = os.urandom(16), os.urandom(16)
    print(f"target_delay={TARGET_DELAY}ms", flush=True)
    ctrl.send_offer(video_stream(0, VSSRC, vkey.hex(), viv.hex(), W, H, FPS, BR, target_delay=TARGET_DELAY))
    ans = ctrl.wait_answer(12); print("ANSWER:", ans, flush=True)
    if not ans or "udpPort" not in ans:
        print("kein udpPort"); return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("", 0))
    dest = (TV, ans["udpPort"])
    st = {"fid": 0, "seq": 0, "pk": 0, "oct": 0, "run": True,
          "last_rtp": 0, "last_wall": 0, "started": False, "rtx": 0, "coff": 0.0}
    buf_pkts = {}; seen = {}

    def find_full(f8):
        cands = [f for f in buf_pkts if (f & 0xff) == f8]
        return max(cands) if cands else None

    def rx():
        sock.settimeout(0.3)
        while st["run"]:
            try:
                d, _ = sock.recvfrom(2048)
                rn = find_xr_reftime(d)   # TV-Uhr-Offset laufend messen
                if rn is not None:
                    off = ntp_to_unix(rn) - time.time()
                    st["coff"] = off if st["coff"] == 0.0 else st["coff"] * 0.85 + off * 0.15
                r = parse_nacks(d)
                if not r: continue
                ack, lc, fields = r
                for f8, pid, mask in fields:
                    ff = find_full(f8)
                    if ff is None: continue
                    pkts = buf_pkts.get(ff, {})
                    want = list(pkts) if pid == 0xffff else [pid] + [pid+1+i for i in range(8) if mask & (1 << i)]
                    for p in want:
                        if p in pkts: sock.sendto(pkts[p], dest); st["rtx"] += 1
                now = time.time() - st.get("t0", 0)
                if now - seen.get("t", -9) > 2:
                    seen["t"] = now; print(f"  [{now:4.1f}s] ACK={ack} loss={lc} rtx={st['rtx']} fid={st['fid']-1} coff={st['coff']:+.3f}s", flush=True)
            except (socket.timeout, OSError): pass
    threading.Thread(target=rx, daemon=True).start()

    def srloop():
        while st["run"]:
            if st["started"] and st["last_wall"]:
                w = st["last_wall"] + st["coff"]  # auf TV-Uhr verschieben
                sec = int(w) + NTP_EPOCH; frac = int((w % 1) * (1 << 32)) & 0xFFFFFFFF
                sock.sendto(struct.pack("!BBHIIIIII", 0x80, 200, 6, VSSRC, sec, frac,
                            st["last_rtp"] & 0xFFFFFFFF, st["pk"] & 0xFFFFFFFF, st["oct"] & 0xFFFFFFFF), dest)
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
        b = sm.get_buffer(); ok, mi = b.map(Gst.MapFlags.READ)
        if not ok: return Gst.FlowReturn.OK
        try: data = bytes(mi.data)
        finally: b.unmap(mi)
        is_key = not (b.get_flags() & Gst.BufferFlags.DELTA_UNIT)
        if not st["started"]:
            if not is_key: return Gst.FlowReturn.OK   # erst ab erstem Keyframe = frame 0
            st["started"] = True
        fid = st["fid"]; ts = int((b.pts or 0) * 9 // 100000) & 0xFFFFFFFF
        enc = encrypt_frame(data, fid, vkey, viv)
        pk, st["seq"] = rtp.packetize(payload=enc, frame_id=fid, is_key=is_key, reference_frame_id=fid-1,
                                      ssrc=VSSRC, payload_type=VPT, rtp_timestamp=ts, seq=st["seq"])
        buf_pkts[fid] = {i: p for i, p in enumerate(pk)}
        for old in [f for f in buf_pkts if f < fid - 90]: del buf_pkts[old]
        for p in pk: sock.sendto(p, dest); st["oct"] += len(p) - 12
        st["pk"] += len(pk); st["fid"] = fid + 1; st["last_rtp"] = ts; st["last_wall"] = time.time()
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    st["t0"] = time.time()
    pipe.set_state(Gst.State.PLAYING)
    print(f">>> Low-Latency-Mirror läuft ({DURATION}s) – Maus bewegen, ACK muss hochzählen", flush=True)
    time.sleep(DURATION); st["run"] = False
    print(f">>> Ende. Frames={st['fid']-1} rtx={st['rtx']}", flush=True)
    pipe.set_state(Gst.State.NULL)
    try: cc.quit_app()
    except Exception: pass


if __name__ == "__main__":
    main()
