#!/usr/bin/env python3
"""Aggressiver Cast-Receiver-Wecker + automatischer nativer Mirror.

Feuert parallel alle bekannten Discovery-/Weck-Vektoren auf den TV (SSDP-Flut,
mDNS _googlecast, AirPlay/DIAL-HTTP, TCP-Anklopfen) und pollt Port 8009.
Sobald der Cast-Receiver wach ist, startet sofort der native RTP-Mirror.

Aufruf:  DISPLAY=:0.0 .venv/bin/python tools/wake_and_mirror.py [sekunden]
"""
import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pychromecast  # früh importieren – verhindert Import-Deadlock zwischen Threads

TV = "192.168.0.33"
WAKE_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 150
stop = threading.Event()


def port_open(p: int, timeout: float = 1.0) -> bool:
    try:
        socket.create_connection((TV, p), timeout=timeout).close()
        return True
    except OSError:
        return False


# --- Weck-Vektoren ---------------------------------------------------------
def ssdp_flood() -> None:
    sts = [
        "urn:dial-multiscreen-org:service:dial:1",
        "urn:dial-multiscreen-org:device:dial:1",
        "ssdp:all",
        "urn:schemas-upnp-org:device:MediaRenderer:1",
        "urn:lge:service:webos-second-screen:1",
        "roku:ecp",
    ]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    while not stop.is_set():
        for st in sts:
            m = "\r\n".join(['M-SEARCH * HTTP/1.1', 'HOST:239.255.255.250:1900',
                             'MAN:"ssdp:discover"', 'MX:1', f'ST:{st}', '', '']).encode()
            try: s.sendto(m, ('239.255.255.250', 1900))
            except OSError: pass
        time.sleep(1)


def mdns_disco() -> None:
    import zeroconf
    from pychromecast.discovery import CastBrowser, SimpleCastListener
    zc = zeroconf.Zeroconf()
    br = CastBrowser(SimpleCastListener(), zc)
    br.start_discovery()
    stop.wait()
    try: br.stop_discovery(); zc.close()
    except Exception: pass


def http_pokes() -> None:
    urls = [f"http://{TV}:7000/info",
            f"http://{TV}:8008/ssdp/device-desc.xml",
            f"http://{TV}:8008/setup/eureka_info",
            f"http://{TV}:9080/", f"http://{TV}:1255/"]
    while not stop.is_set():
        for u in urls:
            try: urllib.request.urlopen(u, timeout=1).read()
            except Exception: pass
        time.sleep(1.5)


def tcp_knock() -> None:
    while not stop.is_set():
        for p in (8008, 8009):
            port_open(p, 0.6)
        time.sleep(0.8)


def cast_connect_attempts() -> None:
    # Verbindungsversuch selbst kann den Receiver triggern
    while not stop.is_set():
        try:
            cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
            cc.wait(timeout=3)
            cc.disconnect()
        except Exception:
            pass
        time.sleep(2)


# --- Nativer Mirror --------------------------------------------------------
def run_native_mirror() -> None:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    import pychromecast
    from app.mirror.cast_streaming import rtp
    from app.mirror.cast_streaming.control import CastStreamingControl, video_stream
    from app.mirror.cast_streaming.crypto import encrypt_frame

    Gst.init(None)
    W, H, FPS, BR = 1280, 720, 30, 4_000_000
    VSSRC, VPT = 100001, 96
    cc = pychromecast.get_chromecast_from_host((TV, 8009, None, "OLED55G29LA", "gallery"))
    cc.wait(timeout=15)
    ctrl = CastStreamingControl(); cc.register_handler(ctrl)
    print(">>> Mirroring-App + OFFER", flush=True)
    ctrl.launch(); time.sleep(3)
    vkey, viv = os.urandom(16), os.urandom(16)
    ctrl.send_offer(video_stream(0, VSSRC, vkey.hex(), viv.hex(), W, H, FPS, BR))
    ans = ctrl.wait_answer(12)
    print(">>> ANSWER:", ans, flush=True)
    if not ans or "udpPort" not in ans:
        print("kein udpPort – Abbruch"); return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("", 0))
    dest = (TV, ans["udpPort"])
    desc = (f"ximagesrc use-damage=false ! video/x-raw,framerate={FPS}/1 ! videoscale add-borders=true "
            f"! videoconvert ! video/x-raw,width={W},height={H} "
            f"! x264enc tune=zerolatency speed-preset=ultrafast bitrate={BR//1000} key-int-max={FPS*2} "
            f"! video/x-h264,profile=main,stream-format=byte-stream ! h264parse config-interval=-1 "
            f"! appsink name=s emit-signals=true sync=false max-buffers=2 drop=true")
    pipe = Gst.parse_launch(desc)
    st = {"fid": 1, "seq": 0, "pk": 0}

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
        for p in pk: sock.sendto(p, dest)
        st["pk"] += len(pk); st["fid"] = fid + 1
        return Gst.FlowReturn.OK

    pipe.get_by_name("s").connect("new-sample", on_sample)
    pipe.set_state(Gst.State.PLAYING)
    print(">>> nativer RTP-Mirror läuft – Bild am TV? (30 s)", flush=True)
    time.sleep(30)
    print(f">>> gesendet: {st['fid']-1} Frames / {st['pk']} Pakete", flush=True)
    pipe.set_state(Gst.State.NULL)
    try: cc.quit_app()
    except Exception: pass


def main() -> None:
    for f in (ssdp_flood, mdns_disco, http_pokes, tcp_knock, cast_connect_attempts):
        threading.Thread(target=f, daemon=True).start()
    print(f"Prügele {WAKE_SECONDS}s auf {TV} ein…", flush=True)
    opened = False
    t0 = time.time()
    while time.time() - t0 < WAKE_SECONDS:
        if port_open(8009):
            opened = True
            print(f"[{int(time.time()-t0)}s] >>> 8009 OFFEN! <<<", flush=True)
            break
        if int(time.time() - t0) % 10 < 2:
            print(f"[{int(time.time()-t0)}s] 8009 zu (8008={port_open(8008)}, 9080={port_open(9080)})", flush=True)
        time.sleep(2)
    stop.set()
    time.sleep(1)
    if opened:
        run_native_mirror()
    else:
        print("8009 blieb zu – kein Durchkommen.")


if __name__ == "__main__":
    main()
