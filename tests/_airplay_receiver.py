"""Ein kleiner AirPlay-Empfänger für die Tests der eigenen Spiegel-Engine.

Er spielt die Gegenseite so, wie sie Fernseher sprechen: HAP-Kopplung mit PIN
(SRP-6a), Pair-Verify, verschlüsselter Steuerkanal, die SETUP-Folge
(Sitzung/Ton/Video), Zeitabfrage an den Sender (NTP), Ereigniskanal, und auf
dem Datenkanal entschlüsselt und prüft er jedes Bild. Was er sieht, steht in
Attributen, die die Tests auswerten.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import socket
import struct
import threading
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from app.mirror.airplay_stream import hap

_N, _G = hap._N, hap._G
CLOCK_ID = 0x4454424C54414B45


def _ib(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


def _h(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for p in parts:
        h.update(p)
    return h.digest()


def _raw(key) -> bytes:  # noqa: ANN001
    from cryptography.hazmat.primitives import serialization
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class FakeReceiver:
    LEGACY_FEATURES = 0x038BCB46007F8AD0 & ~(1 << 14) & ~(1 << 41)  # Drittanbieter, NTP
    PTP_FEATURES = LEGACY_FEATURES | (1 << 41)

    def __init__(self, *, pin: str = "1234", features: int = LEGACY_FEATURES,
                 source_version: str = "377.25.06", session_first: bool = False,
                 ptp: bool = False, ptp_clock: bool = True, reject_stream_connections: bool = False,
                 status_flags: int = 1 << 9, **knobs) -> None:
        self.pin = pin
        self.features = features
        self.source_version = source_version
        self.session_first = session_first
        self.ptp = ptp
        self.ptp_clock = ptp_clock
        self.reject_stream_connections = reject_stream_connections
        self.status_flags = status_flags
        # Schalter für Fehlerfälle:
        #   pin_start_status, refuse_hkp={"3", "5"}, backoff_once, verify_status,
        #   fail_setup={"control"|"audio"|"video": status}, no_data_port,
        #   timing_port (Empfänger nennt seinen Port, Sender fragt), async_ptp,
        #   drop_after_record, event_garbage
        self.knobs = knobs
        self.probes = threading.Event()
        self.identifier = b"AA:BB:CC:DD:EE:FF"
        self._ltsk = Ed25519PrivateKey.generate()
        self.ltpk = _raw(self._ltsk.public_key())
        self.controllers: dict[bytes, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.pin_starts = 0
        self.setups: list[dict] = []
        self.ntp_answered = threading.Event()
        self.codec_configs: list[tuple[bytes, float, float]] = []
        self.frames: list[tuple[bytes, bool, int, int]] = []
        self.heartbeats = 0
        self.event_acks = threading.Event()
        self.teardowns = 0
        self.frame_errors: list[str] = []
        self.audio_frames: list[tuple[int, int, int, bytes]] = []   # (pt, seq, rtp, Nutzlast)
        self.audio_syncs: list[bytes] = []
        self.audio_answers: list[bytes] = []
        self.audio_ctrl: socket.socket | None = None
        self.audio_sender_ctrl: tuple[str, int] | None = None
        self.close_data_after = 0       # nach so vielen Bildern den Datenkanal schließen
        self.got_frames = threading.Event()
        self._shared = b""
        self._video_scid = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    # -- Infrastruktur --------------------------------------------------------
    def close(self) -> None:
        self._closing = True
        self._sock.close()

    def _listen(self, handler) -> int:  # noqa: ANN001
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)

        def run() -> None:
            conn, _ = sock.accept()
            sock.close()
            handler(conn)

        threading.Thread(target=run, daemon=True).start()
        return sock.getsockname()[1]

    def _accept(self) -> None:
        while not self._closing:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            if self.knobs.get("reset_first"):
                self.knobs["reset_first"] -= 1
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()                        # RST statt Antwort, wie der Hisense
                continue
            threading.Thread(target=self._control, args=(conn,), daemon=True).start()

    def info(self) -> dict:
        return {"features": self.features, "statusFlags": self.status_flags,
                "sourceVersion": self.source_version, "deviceID": self.identifier.decode(),
                "displays": [{"widthPixels": 1920, "heightPixels": 1080}]}

    # -- Steuerkanal ----------------------------------------------------------
    def _control(self, conn: socket.socket) -> None:
        state: dict = {"cipher": None, "buffer": b""}
        try:
            while True:
                request = self._read_request(conn, state)
                if request is None:
                    return
                method, uri, headers, body = request
                self.requests.append((method, uri))
                status, extra, payload, enable = self._handle(method, uri, headers, body, state)
                self._respond(conn, state, headers.get("cseq", "0"), status, extra, payload)
                if state.get("drop"):
                    return
                if enable is not None:
                    state["cipher"] = enable
        except (OSError, hap.PairingError):
            return
        finally:
            conn.close()

    def _read_request(self, conn, state):  # noqa: ANN001, ANN202
        while b"\r\n\r\n" not in state["buffer"]:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            state["buffer"] += state["cipher"].decrypt(chunk) if state["cipher"] else chunk
        head, _, rest = state["buffer"].partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, uri, _version = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            rest += state["cipher"].decrypt(chunk) if state["cipher"] else chunk
        state["buffer"] = rest[length:]
        return method, uri, headers, rest[:length]

    def _respond(self, conn, state, cseq, status, extra, payload) -> None:  # noqa: ANN001
        if isinstance(payload, dict):
            body = plistlib.dumps(payload, fmt=plistlib.FMT_BINARY)
            ctype = "application/x-apple-binary-plist"
        elif isinstance(payload, str):
            body, ctype = payload.encode(), "text/plain"
        else:
            body, ctype = payload or b"", "application/octet-stream"
        lines = [f"RTSP/1.0 {status} {'OK' if status == 200 else 'Error'}", f"CSeq: {cseq}",
                 f"Content-Length: {len(body)}", "Server: AirTunes/" + self.source_version]
        if body:
            lines.append(f"Content-Type: {ctype}")
        for name, value in (extra or {}).items():
            lines.append(f"{name}: {value}")
        data = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        conn.sendall(state["cipher"].encrypt(data) if state["cipher"] else data)

    def _clock_headers(self) -> dict:
        if not self.ptp:
            return {}
        return {"X-Apple-RequestReceivedTimestamp": "5000000", "X-Apple-ProcessingTime": "2"}

    def _handle(self, method, uri, headers, body, state):  # noqa: ANN001, ANN202, C901
        if method == "GET" and uri == "/info":
            return 200, None, self.info(), None
        if uri == "/pair-pin-start":
            self.pin_starts += 1
            return self.knobs.get("pin_start_status", 200), None, b"", None
        if uri == "/pair-setup":
            if headers.get("x-apple-hkp") in self.knobs.get("refuse_hkp", ()):
                return 501, None, "pairing type unsupported", None
            if self.knobs.get("backoff_once") is not None:
                delay = self.knobs.pop("backoff_once")
                return 200, None, hap.tlv_encode([(hap.TLV_STATE, b"\x02"),
                                                  (hap.TLV_ERROR, b"\x03"),
                                                  (hap.TLV_RETRY_DELAY, bytes([delay]))]), None
            return (200, None) + self._pair_setup(body, state)
        if uri == "/pair-verify":
            if "verify_status" in self.knobs:
                return self.knobs["verify_status"], None, "verify refused", None
            return (200, None) + self._pair_verify(body, state)
        if method == "SETUP":
            return self._setup(plistlib.loads(body))
        if method in ("RECORD", "TEARDOWN") or uri == "/feedback":
            if method == "TEARDOWN":
                self.teardowns += 1
            if method == "RECORD" and self.knobs.get("drop_after_record"):
                state["drop"] = True
            if method == "RECORD" and "record_status" in self.knobs:
                return self.knobs["record_status"], None, "record refused", None
            return 200, self._clock_headers(), b"", None
        return 404, None, "unknown", None

    # -- Kopplung (Serverseite) -------------------------------------------------
    def _pair_setup(self, body: bytes, state: dict):  # noqa: ANN202
        tlv = hap.tlv_decode(body)
        step = tlv.get(hap.TLV_STATE, b"\x00")[0]
        if step == 1:
            state["salt"] = os.urandom(16)
            x = int.from_bytes(_h(state["salt"], _h(b"Pair-Setup:" + self.pin.encode())), "big")
            state["v"] = pow(_G, x, _N)
            state["b"] = int.from_bytes(os.urandom(32), "big")
            k = int.from_bytes(_h(_ib(_N).rjust(384, b"\0"), _ib(_G).rjust(384, b"\0")), "big")
            state["B"] = (k * state["v"] + pow(_G, state["b"], _N)) % _N
            return hap.tlv_encode([(hap.TLV_STATE, b"\x02"), (hap.TLV_SALT, state["salt"]),
                                   (hap.TLV_PUBLIC_KEY, _ib(state["B"]))]), None
        if step == 3:
            A = int.from_bytes(tlv[hap.TLV_PUBLIC_KEY], "big")
            a_b, b_b = _ib(A), _ib(state["B"])
            u = int.from_bytes(_h(a_b.rjust(384, b"\0"), b_b.rjust(384, b"\0")), "big")
            S = pow(A * pow(state["v"], u, _N) % _N, state["b"], _N)
            K = _h(_ib(S))
            hxor = bytes(p ^ q for p, q in zip(_h(_ib(_N)), _h(_ib(_G)), strict=True))
            expected = _h(hxor, _h(b"Pair-Setup"), state["salt"], a_b, b_b, K)
            if tlv.get(hap.TLV_PROOF) != expected:
                return hap.tlv_encode([(hap.TLV_STATE, b"\x04"), (hap.TLV_ERROR, b"\x02")]), None
            state["K"] = K
            return hap.tlv_encode([(hap.TLV_STATE, b"\x04"),
                                   (hap.TLV_PROOF, _h(a_b, expected, K))]), None
        if step == 5:
            K = state["K"]
            key = hap.hkdf_sha512(K, b"Pair-Setup-Encrypt-Salt", b"Pair-Setup-Encrypt-Info")
            sub = hap.tlv_decode(ChaCha20Poly1305(key).decrypt(
                hap.nonce_for(b"PS-Msg05"), tlv[hap.TLV_ENCRYPTED_DATA], None))
            sign = hap.hkdf_sha512(K, b"Pair-Setup-Controller-Sign-Salt",
                                   b"Pair-Setup-Controller-Sign-Info")
            client_id, client_pk = sub[hap.TLV_IDENTIFIER], sub[hap.TLV_PUBLIC_KEY]
            Ed25519PublicKey.from_public_bytes(client_pk).verify(
                sub[hap.TLV_SIGNATURE], sign + client_id + client_pk)
            state["acl"] = sub.get(hap.TLV_ACL)
            self.controllers[client_id] = client_pk
            acc = hap.hkdf_sha512(K, b"Pair-Setup-Accessory-Sign-Salt",
                                  b"Pair-Setup-Accessory-Sign-Info")
            reply = hap.tlv_encode([(hap.TLV_IDENTIFIER, self.identifier),
                                    (hap.TLV_PUBLIC_KEY, self.ltpk),
                                    (hap.TLV_SIGNATURE,
                                     self._ltsk.sign(acc + self.identifier + self.ltpk))])
            sealed = ChaCha20Poly1305(key).encrypt(hap.nonce_for(b"PS-Msg06"), reply, None)
            return hap.tlv_encode([(hap.TLV_STATE, b"\x06"),
                                   (hap.TLV_ENCRYPTED_DATA, sealed)]), None
        return b"", None

    def _pair_verify(self, body: bytes, state: dict):  # noqa: ANN202
        tlv = hap.tlv_decode(body)
        step = tlv.get(hap.TLV_STATE, b"\x00")[0]
        if step == 1:
            eph = X25519PrivateKey.generate()
            state["client_pub"] = tlv[hap.TLV_PUBLIC_KEY]
            state["eph_pub"] = _raw(eph.public_key())
            state["shared"] = eph.exchange(X25519PublicKey.from_public_bytes(state["client_pub"]))
            state["vkey"] = hap.hkdf_sha512(state["shared"], b"Pair-Verify-Encrypt-Salt",
                                            b"Pair-Verify-Encrypt-Info")
            sig = self._ltsk.sign(state["eph_pub"] + self.identifier + state["client_pub"])
            sealed = ChaCha20Poly1305(state["vkey"]).encrypt(
                hap.nonce_for(b"PV-Msg02"),
                hap.tlv_encode([(hap.TLV_IDENTIFIER, self.identifier),
                                (hap.TLV_SIGNATURE, sig)]), None)
            return hap.tlv_encode([(hap.TLV_STATE, b"\x02"),
                                   (hap.TLV_PUBLIC_KEY, state["eph_pub"]),
                                   (hap.TLV_ENCRYPTED_DATA, sealed)]), None
        sub = hap.tlv_decode(ChaCha20Poly1305(state["vkey"]).decrypt(
            hap.nonce_for(b"PV-Msg03"), tlv[hap.TLV_ENCRYPTED_DATA], None))
        client_pk = self.controllers.get(sub[hap.TLV_IDENTIFIER])
        try:
            if client_pk is None:
                raise ValueError("unbekannt")
            Ed25519PublicKey.from_public_bytes(client_pk).verify(
                sub[hap.TLV_SIGNATURE],
                state["client_pub"] + sub[hap.TLV_IDENTIFIER] + state["eph_pub"])
        except Exception:  # noqa: BLE001
            return hap.tlv_encode([(hap.TLV_STATE, b"\x04"), (hap.TLV_ERROR, b"\x02")]), None
        self._shared = state["shared"]
        read_key = hap.hkdf_sha512(self._shared, b"Control-Salt", b"Control-Write-Encryption-Key")
        write_key = hap.hkdf_sha512(self._shared, b"Control-Salt", b"Control-Read-Encryption-Key")
        return hap.tlv_encode([(hap.TLV_STATE, b"\x04")]), hap.HapCipher(write_key, read_key)

    # -- SETUP --------------------------------------------------------------------
    def _setup(self, request: dict):  # noqa: ANN202, C901
        self.setups.append(request)
        streams = request.get("streams")
        phase = "control" if not streams else {96: "audio", 110: "video"}[streams[0]["type"]]
        if phase in self.knobs.get("fail_setup", {}):
            return self.knobs["fail_setup"][phase], None, "refused", None
        timing = request.get("timingProtocol")
        if timing is not None and timing != ("PTP" if self.ptp else "NTP"):
            return 400, None, f"timingProtocol is {timing!r}", None
        if timing == "NTP" and request.get("timingPort"):
            threading.Thread(target=self._probe_ntp, args=(int(request["timingPort"]),),
                             daemon=True).start()
        response: dict = {}
        if timing is not None:
            response["eventPort"] = self._listen(self._events)
            if self.ptp and self.ptp_clock and not self.knobs.get("async_ptp"):
                response["timingPeerInfo"] = {"ClockID": CLOCK_ID}
            if self.knobs.get("async_ptp"):
                response["asyncPTPClockConfig"] = True
            if self.knobs.get("timing_port"):
                response["timingPort"] = self._timing_server()
        if not streams:
            if not self.session_first:
                return 455, None, "streams first", None
            return 200, self._clock_headers(), response, None
        stream = streams[0]
        if stream["type"] == 96:
            if len(stream.get("shk", b"")) != 32:
                return 400, None, "encrypted audio descriptor omitted 32-byte shk", None
            if "streamConnections" in stream and self.reject_stream_connections:
                return 400, None, "audio descriptor must use controlPort", None
            if self.knobs.get("no_audio_ports"):
                response["streams"] = [{"type": 96}]
                return 200, self._clock_headers(), response, None
            data_port, ctrl_port = self._audio_server(stream["shk"], int(stream.get(
                "controlPort") or stream.get("streamConnections", {}).get(
                "streamConnectionTypeRTCP", {}).get("streamConnectionKeyPort", 0)))
            if "streamConnections" in stream:
                response["streams"] = [{"type": 96, "streamConnections": {
                    "streamConnectionTypeRTP": {"streamConnectionKeyPort": data_port},
                    "streamConnectionTypeRTCP": {"streamConnectionKeyPort": ctrl_port}}}]
            else:
                response["streams"] = [{"type": 96, "dataPort": data_port,
                                        "controlPort": ctrl_port}]
            return 200, self._clock_headers(), response, None
        self._video_scid = int(stream["streamConnectionID"])
        if self.knobs.get("no_data_port"):
            response["streams"] = [{"type": 110}]
            return 200, None, response, None
        port = 1 if self.knobs.get("closed_data_port") else self._listen(self._data)
        response["streams"] = [{"type": 110, "dataPort": port}]
        return 200, None, response, None

    def _probe_ntp(self, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        request = bytearray(32)
        request[0], request[1] = 0x80, 0xD2
        request[24:32] = struct.pack(">Q", 123456)
        sock.sendto(bytes(request), ("127.0.0.1", port))
        try:
            reply, _ = sock.recvfrom(64)
        except OSError:
            return
        finally:
            sock.close()
        if reply[1] == 0xD3 and reply[8:16] == bytes(request[24:32]):
            self.ntp_answered.set()

    def _audio_server(self, key: bytes, sender_ctrl_port: int) -> tuple[int, int]:
        """Ton-Daten entschlüsseln (ChaCha20, Nonce hinten, AAD Zeit+SSRC) und
        Sync-Pakete sowie Antworten auf Nachforderungen sammeln."""
        from app.mirror.airplay_stream import audio as audio_mod

        aead = ChaCha20Poly1305(key)
        data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        data.bind(("127.0.0.1", 0))
        ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctrl.bind(("127.0.0.1", 0))
        self.audio_ctrl = ctrl
        self.audio_sender_ctrl = ("127.0.0.1", sender_ctrl_port)

        def open_packet(packet: bytes):  # noqa: ANN202
            pt, seq, rtp = packet[1] & 0x7F, *struct.unpack(">HI", packet[2:8])
            body, nonce = packet[12:-8], packet[-8:]
            clear = audio_mod.red_header_length(body) if pt == audio_mod.PT_RED else 0
            plain = aead.decrypt(b"\0" * 4 + nonce, body[clear:], packet[4:12])
            return pt, seq, rtp, body[:clear] + plain

        def run_data() -> None:
            data.settimeout(5)
            while not self._closing:
                try:
                    packet, _ = data.recvfrom(2048)
                except OSError:
                    return
                try:
                    self.audio_frames.append(open_packet(packet))
                except Exception as exc:  # noqa: BLE001
                    self.frame_errors.append(f"Ton: {exc!r}")

        def run_ctrl() -> None:
            ctrl.settimeout(5)
            while not self._closing:
                try:
                    packet, _ = ctrl.recvfrom(2048)
                except OSError:
                    return
                if packet[1] in (audio_mod.PT_SYNC_NTP, audio_mod.PT_SYNC_PTP):
                    self.audio_syncs.append(packet)
                else:
                    self.audio_answers.append(packet)

        threading.Thread(target=run_data, daemon=True).start()
        threading.Thread(target=run_ctrl, daemon=True).start()
        self.open_audio_packet = open_packet
        return data.getsockname()[1], ctrl.getsockname()[1]

    def request_audio(self, first: int, count: int) -> None:
        """Fordert Ton-Pakete nach (0xd5), wie es ein Empfänger bei Verlust tut."""
        self.audio_ctrl.sendto(struct.pack(">BBHHH", 0x80, 0xD5, 9, first, count),
                               self.audio_sender_ctrl)

    def _timing_server(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)

        def run() -> None:
            try:
                data, _addr = sock.recvfrom(64)
            except OSError:
                return
            finally:
                sock.close()
            if data[:2] == b"\x80\xd2":
                self.probes.set()

        threading.Thread(target=run, daemon=True).start()
        return sock.getsockname()[1]

    def _events(self, conn: socket.socket) -> None:
        write_key = hap.hkdf_sha512(self._shared, b"Events-Salt", b"Events-Write-Encryption-Key")
        read_key = hap.hkdf_sha512(self._shared, b"Events-Salt", b"Events-Read-Encryption-Key")
        cipher = hap.HapCipher(write_key, read_key)
        body = plistlib.dumps({"type": "updateTimingPeerInfo", "value": {"ClockID": CLOCK_ID}},
                              fmt=plistlib.FMT_BINARY)
        if self.knobs.get("event_garbage"):
            body = b"kein plist"
        request = (f"POST /command RTSP/1.0\r\nCSeq: 7\r\nContent-Length: {len(body)}\r\n"
                   "Content-Type: application/x-apple-binary-plist\r\n\r\n").encode() + body
        try:
            conn.sendall(cipher.encrypt(request))
            reply = b""
            while b"\r\n\r\n" not in reply:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                reply += cipher.decrypt(chunk)
            if reply.startswith(b"RTSP/1.0 200") and b"CSeq: 7" in reply:
                self.event_acks.set()
            while conn.recv(4096):
                pass
        except OSError:
            return

    def _read_exact(self, conn: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise OSError("geschlossen")
            data += chunk
        return data

    def _data(self, conn: socket.socket) -> None:
        aead = ChaCha20Poly1305(hap.video_key(self._shared, self._video_scid))
        nonce = 0
        try:
            while True:
                header = self._read_exact(conn, 128)
                size = struct.unpack_from("<I", header)[0]
                payload = self._read_exact(conn, size) if size else b""
                kind = header[4]
                if kind == 0x01:
                    w, h = struct.unpack_from("<ff", header, 16)
                    self.codec_configs.append((payload, w, h))
                elif kind == 0x02:
                    self.heartbeats += 1
                elif kind == 0x00:
                    plain = aead.decrypt(hap.nonce_for(nonce), payload, header)
                    nonce += 1
                    pos = 0
                    while pos < len(plain):            # AVCC: Längen müssen aufgehen
                        pos += 4 + struct.unpack_from(">I", plain, pos)[0]
                    if pos != len(plain):
                        self.frame_errors.append("AVCC-Längen passen nicht")
                    ts = struct.unpack_from("<Q", header, 8)[0]
                    timeline = struct.unpack_from("<Q", header, 40)[0]
                    self.frames.append((plain, bool(header[5] & 0x10), ts, timeline))
                    self.got_frames.set()
                    if self.close_data_after and len(self.frames) >= self.close_data_after:
                        conn.close()
                        return
        except Exception as exc:  # noqa: BLE001
            if not isinstance(exc, OSError):
                self.frame_errors.append(repr(exc))
        finally:
            conn.close()


def wait_for(predicate, timeout: float = 3.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
