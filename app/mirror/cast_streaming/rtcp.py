"""RTCP für Cast-Streaming: Sender Report bauen, Receiver-Feedback lesen.

Der Receiver antwortet mit einem RTCP-**Compound** (RR + XR + Cast-Feedback in
einem UDP-Paket), deshalb muss man beim Parsen alle Sub-Pakete durchlaufen.
Drei Dinge brauchen wir daraus:

* **Cast-NACK** (PT=206, Magic ``CAST``) – welche Pakete erneut zu senden sind.
  Die ``media_ssrc`` sagt, ob Video oder Audio gemeint ist.
* **XR Receiver Reference Time** (PT=207, Blocktyp 4) – die Uhr des Geräts. Ohne
  diese Korrektur erscheinen unsere Frames "aus der Zukunft" und werden am TV
  um den Uhr-Versatz verzögert abgespielt (~0,8 s beim getesteten LG).
* Wir selbst senden **Sender Reports** (PT=200) mit derselben, korrigierten Uhr.

Bewusst frei von pychromecast/GStreamer – reine Bytes, damit direkt testbar.
"""
from __future__ import annotations

import struct

NTP_EPOCH = 2208988800  # Sekunden zwischen 1900 und 1970

PT_SENDER_REPORT = 200
PT_RTPFB = 206
PT_XR = 207
XR_RECEIVER_REFERENCE_TIME = 4
CAST_MAGIC = b"CAST"


def walk(d: bytes):
    """Iteriert die Sub-Pakete eines RTCP-Compound: (packet_type, offset, size)."""
    off = 0
    while off + 4 <= len(d):
        pt = d[off + 1]
        ln = struct.unpack("!H", d[off + 2:off + 4])[0]
        size = (ln + 1) * 4
        if size <= 0 or off + size > len(d):
            break
        yield pt, off, size
        off += size


def ntp_to_unix(n: int) -> float:
    """64-Bit-NTP-Zeitstempel -> Unix-Sekunden (mit Nachkommastellen)."""
    return (n >> 32) - NTP_EPOCH + ((n & 0xFFFFFFFF) / (1 << 32))


def unix_to_ntp(seconds: float) -> tuple[int, int]:
    """Unix-Zeit -> (NTP-Sekunden, NTP-Bruchteil) als 32-Bit-Paar."""
    return int(seconds) + NTP_EPOCH, int((seconds % 1) * (1 << 32)) & 0xFFFFFFFF


def parse_nacks(d: bytes):
    """Cast-Feedback -> ``(media_ssrc, [(frame8, packet_id, bitmask)])``.

    Ohne Treffer ``(None, None)``. ``packet_id == 0xFFFF`` heißt "ganzes Frame".
    """
    for pt, off, size in walk(d):
        if pt == PT_RTPFB and d[off + 12:off + 16] == CAST_MAGIC:
            media_ssrc = struct.unpack("!I", d[off + 8:off + 12])[0]
            fci = d[off + 12:off + size]
            fields = []
            o = 8
            while o + 4 <= len(fci):
                fields.append((fci[o], struct.unpack("!H", fci[o + 1:o + 3])[0], fci[o + 3]))
                o += 4
            return media_ssrc, fields
    return None, None


def find_xr_reftime(d: bytes) -> int | None:
    """XR (PT=207) Receiver Reference Time (BT=4) -> 64-Bit-NTP der Geräte-Uhr."""
    for pt, off, size in walk(d):
        if pt == PT_XR:
            o = off + 8
            while o + 4 <= off + size:
                bt = d[o]
                blen = struct.unpack("!H", d[o + 2:o + 4])[0]
                if bt == XR_RECEIVER_REFERENCE_TIME and o + 12 <= len(d):
                    return struct.unpack("!Q", d[o + 4:o + 12])[0]
                o += 4 + blen * 4
    return None


def sender_report(ssrc: int, wall_clock: float, rtp_timestamp: int,
                  packets: int, octets: int) -> bytes:
    """Baut einen RTCP-Sender-Report (PT=200).

    *wall_clock* ist die **bereits auf die Geräte-Uhr korrigierte** Unix-Zeit des
    zuletzt gesendeten Frames; *rtp_timestamp* dessen RTP-Zeit. Beide zusammen
    sagen dem Receiver, wann er das Frame ausgeben soll.
    """
    sec, frac = unix_to_ntp(wall_clock)
    return struct.pack("!BBHIIIIII", 0x80, PT_SENDER_REPORT, 6, ssrc, sec, frac,
                       rtp_timestamp & 0xFFFFFFFF,
                       packets & 0xFFFFFFFF, octets & 0xFFFFFFFF)
