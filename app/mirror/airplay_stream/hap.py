"""HAP-Kopplung für AirPlay: Pair-Setup (PIN), Pair-Verify und die Rahmen danach.

AirPlay-Empfänger koppeln sich wie HomeKit-Zubehör:

* **Pair-Setup** (einmalig, mit der PIN vom Fernseher): SRP-6a mit SHA-512 und
  der 3072-Bit-Gruppe aus RFC 5054, danach tauschen beide Seiten ihre
  langlebigen Ed25519-Schlüssel verschlüsselt aus.
* **Pair-Verify** (jede Sitzung): X25519 plus Signaturen mit den gespeicherten
  Schlüsseln. Das gemeinsame Geheimnis daraus verschlüsselt den Steuerkanal
  und – über HKDF – auch den Videostrom.

Die Kopplungsdaten haben dasselbe Format wie die von pyatv
(``ltpk:ltsk:atv_id:client_id`` als Hex), damit die Kopplung, die Schwupp beim
Verbinden über pyatv anlegt, auch fürs Spiegeln gilt und der Fernseher nicht
zweimal nach einem Code fragt.
"""
from __future__ import annotations

import hashlib
import os
import struct
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# TLV8-Kennungen (HAP-Spezifikation)
TLV_METHOD = 0x00
TLV_IDENTIFIER = 0x01
TLV_SALT = 0x02
TLV_PUBLIC_KEY = 0x03
TLV_PROOF = 0x04
TLV_ENCRYPTED_DATA = 0x05
TLV_STATE = 0x06
TLV_ERROR = 0x07
TLV_RETRY_DELAY = 0x08
TLV_SIGNATURE = 0x0A
TLV_ACL = 0x12

# X-Apple-HKP: 3 = klassisches HAP (Fernseher anderer Hersteller),
# 5 = Bildschirmaufnahme (aktuelle Apple-Empfänger, mit Freigabe im M5).
PAIRING_LEGACY = 3
PAIRING_SCREEN = 5

# OPACK für {"com.apple.ScreenCapture": true} – ohne diese Freigabe lehnen
# aktuelle Apple-Empfänger eine Kopplung vom Typ 5 fürs Spiegeln ab.
SCREEN_CAPTURE_ACL = b"\xe1\x57com.apple.ScreenCapture\x01"

# SRP-6a, 3072 Bit (RFC 5054), g = 5
_N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
    "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
    "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
    "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
    "43DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF", 16)
_G = 5
_N_LEN = 384

_ERROR_NAMES = {1: "unbekannt", 2: "Code falsch", 3: "später erneut versuchen",
                4: "zu viele Geräte gekoppelt", 5: "zu viele Versuche",
                6: "nicht verfügbar", 7: "beschäftigt"}


class PairingError(Exception):
    """Kopplung abgelehnt oder Antwort des Geräts unbrauchbar."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# -- TLV8 -----------------------------------------------------------------------

def tlv_encode(items: list[tuple[int, bytes]]) -> bytes:
    """TLV8 in der angegebenen Reihenfolge; Werte über 255 Byte werden geteilt."""
    out = bytearray()
    for tag, value in items:
        if not value:
            out += bytes((tag, 0))
            continue
        for start in range(0, len(value), 255):
            chunk = value[start:start + 255]
            out += bytes((tag, len(chunk))) + chunk
    return bytes(out)


def tlv_decode(data: bytes) -> dict[int, bytes]:
    """TLV8 lesen; aufeinanderfolgende Teile einer Kennung werden verbunden."""
    result: dict[int, bytes] = {}
    pos = 0
    while pos + 2 <= len(data):
        tag, length = data[pos], data[pos + 1]
        pos += 2
        if pos + length > len(data):
            break
        result[tag] = result.get(tag, b"") + data[pos:pos + length]
        pos += length
    return result


def check_tlv_error(stage: str, tlv: dict[int, bytes]) -> None:
    error = tlv.get(TLV_ERROR)
    if error:
        code = error[0]
        raise PairingError(f"{stage}: {_ERROR_NAMES.get(code, 'Fehler')} ({code})", code)


# -- Hilfen ---------------------------------------------------------------------

def hkdf_sha512(secret: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA512(), length=length, salt=salt,
                info=info).derive(secret)


def nonce_for(label: bytes | int) -> bytes:
    """ChaCha20-Nonce: 4 Null-Bytes + 8 Byte (Text wie b'PV-Msg02' oder Zähler LE)."""
    if isinstance(label, int):
        return b"\x00" * 4 + struct.pack("<Q", label)
    return b"\x00" * (12 - len(label)) + label


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")


def _pad(data: bytes, length: int = _N_LEN) -> bytes:
    return data.rjust(length, b"\x00")


def _sha512(*parts: bytes) -> bytes:
    h = hashlib.sha512()
    for part in parts:
        h.update(part)
    return h.digest()


def _raw_private(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _raw_public(key) -> bytes:  # noqa: ANN001
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


# -- Kopplungsdaten -------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    """Langlebige Kopplung: Schlüssel des Empfängers + unser Schlüsselpaar."""

    ltpk: bytes        # Ed25519-Public-Key des Empfängers
    ltsk: bytes        # unser Ed25519-Private-Key (32-Byte-Seed)
    atv_id: bytes      # Kennung des Empfängers
    client_id: bytes   # unsere Kopplungs-ID

    def __str__(self) -> str:
        return ":".join(x.hex() for x in (self.ltpk, self.ltsk, self.atv_id, self.client_id))

    @classmethod
    def parse(cls, text: str | None) -> Credentials | None:
        """pyatv-Format lesen; unbrauchbares (auch Legacy mit 2 Feldern) -> None."""
        if not text:
            return None
        parts = text.split(":")
        if len(parts) != 4:
            return None
        try:
            ltpk, ltsk, atv_id, client_id = (bytes.fromhex(p) for p in parts)
        except ValueError:
            return None
        if len(ltpk) != 32 or len(ltsk) != 32 or not atv_id or not client_id:
            return None
        return cls(ltpk, ltsk, atv_id, client_id)


# -- Pair-Setup -----------------------------------------------------------------

class PairSetup:
    """SRP-6a-Kopplung mit PIN. Die Nachrichten verschickt der Aufrufer.

    Ablauf: :meth:`m1` → Antwort an :meth:`m3` → Antwort an :meth:`m5` →
    Antwort an :meth:`finish` (liefert die :class:`Credentials`).
    """

    def __init__(self, pin: str, pairing_type: int = PAIRING_LEGACY,
                 pairing_id: bytes | None = None, private_seed: bytes | None = None) -> None:
        self.pin = pin.strip()
        self.pairing_type = pairing_type
        self.pairing_id = pairing_id or str(uuid.uuid4()).upper().encode()
        self._signing = Ed25519PrivateKey.from_private_bytes(private_seed or os.urandom(32))
        self._K = b""
        self._session_key = b""
        self._client_pub = b""
        self._proof = b""

    @staticmethod
    def m1() -> bytes:
        return tlv_encode([(TLV_METHOD, b"\x00"), (TLV_STATE, b"\x01")])

    def m3(self, m2: bytes, a_private: bytes | None = None) -> bytes:
        tlv = tlv_decode(m2)
        check_tlv_error("Kopplung (M2)", tlv)
        salt, server_pub = tlv.get(TLV_SALT), tlv.get(TLV_PUBLIC_KEY)
        if not salt or not server_pub:
            raise PairingError("Kopplung (M2): Salt oder Schlüssel fehlt")
        B = int.from_bytes(server_pub, "big")
        if B % _N == 0:
            raise PairingError("Kopplung (M2): ungültiger Schlüssel des Geräts")

        username = b"Pair-Setup"
        x = int.from_bytes(_sha512(salt, _sha512(username + b":" + self.pin.encode())), "big")
        k = int.from_bytes(_sha512(_pad(_int_bytes(_N)), _pad(_int_bytes(_G))), "big")
        a = int.from_bytes(a_private or os.urandom(32), "big") or 1
        A = pow(_G, a, _N)
        a_bytes, b_bytes = _int_bytes(A), _int_bytes(B)
        u = int.from_bytes(_sha512(_pad(a_bytes), _pad(b_bytes)), "big")
        S = pow((B - k * pow(_G, x, _N)) % _N, a + u * x, _N)
        self._K = _sha512(_int_bytes(S))

        h_n, h_g = _sha512(_int_bytes(_N)), _sha512(_int_bytes(_G))
        hxor = bytes(p ^ q for p, q in zip(h_n, h_g, strict=True))
        self._proof = _sha512(hxor, _sha512(username), salt, a_bytes, b_bytes, self._K)
        self._client_pub = a_bytes
        return tlv_encode([(TLV_STATE, b"\x03"), (TLV_PUBLIC_KEY, _pad(a_bytes)),
                           (TLV_PROOF, self._proof)])

    def m5(self, m4: bytes) -> bytes:
        tlv = tlv_decode(m4)
        check_tlv_error("Kopplung (M4)", tlv)
        expected = _sha512(self._client_pub, self._proof, self._K)
        if tlv.get(TLV_PROOF) != expected:
            # Ohne passenden Beweis hat das Gerät die PIN nicht gekannt.
            raise PairingError("Kopplung (M4): Code falsch oder Gerät nicht echt", 2)

        self._session_key = hkdf_sha512(self._K, b"Pair-Setup-Encrypt-Salt",
                                        b"Pair-Setup-Encrypt-Info")
        sign_key = hkdf_sha512(self._K, b"Pair-Setup-Controller-Sign-Salt",
                               b"Pair-Setup-Controller-Sign-Info")
        public = _raw_public(self._signing.public_key())
        signature = self._signing.sign(sign_key + self.pairing_id + public)
        items = [(TLV_IDENTIFIER, self.pairing_id), (TLV_PUBLIC_KEY, public),
                 (TLV_SIGNATURE, signature)]
        if self.pairing_type == PAIRING_SCREEN:
            items.append((TLV_ACL, SCREEN_CAPTURE_ACL))
        sealed = ChaCha20Poly1305(self._session_key).encrypt(
            nonce_for(b"PS-Msg05"), tlv_encode(items), None)
        return tlv_encode([(TLV_ENCRYPTED_DATA, sealed), (TLV_STATE, b"\x05")])

    def finish(self, m6: bytes) -> Credentials:
        tlv = tlv_decode(m6)
        check_tlv_error("Kopplung (M6)", tlv)
        try:
            plain = ChaCha20Poly1305(self._session_key).decrypt(
                nonce_for(b"PS-Msg06"), tlv.get(TLV_ENCRYPTED_DATA, b""), None)
        except InvalidTag as exc:
            raise PairingError("Kopplung (M6): Antwort nicht entschlüsselbar") from exc
        sub = tlv_decode(plain)
        identifier, public = sub.get(TLV_IDENTIFIER), sub.get(TLV_PUBLIC_KEY)
        if not identifier or not public or len(public) != 32:
            raise PairingError("Kopplung (M6): Kennung des Geräts fehlt")
        sign_key = hkdf_sha512(self._K, b"Pair-Setup-Accessory-Sign-Salt",
                               b"Pair-Setup-Accessory-Sign-Info")
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(
                sub.get(TLV_SIGNATURE, b""), sign_key + identifier + public)
        except InvalidSignature as exc:
            raise PairingError("Kopplung (M6): Signatur des Geräts falsch") from exc
        return Credentials(public, _raw_private(self._signing), identifier, self.pairing_id)


# -- Pair-Verify ------------------------------------------------------------------

class PairVerify:
    """Prüft eine gespeicherte Kopplung und liefert das gemeinsame Geheimnis.

    Ablauf: :meth:`m1` → Antwort an :meth:`m3` → Antwort an :meth:`finish`.
    """

    def __init__(self, credentials: Credentials, private: bytes | None = None) -> None:
        self.credentials = credentials
        self._private = X25519PrivateKey.from_private_bytes(private or os.urandom(32))
        self._public = _raw_public(self._private.public_key())
        self.shared_secret = b""

    def m1(self) -> bytes:
        return tlv_encode([(TLV_STATE, b"\x01"), (TLV_PUBLIC_KEY, self._public)])

    def m3(self, m2: bytes) -> bytes:
        tlv = tlv_decode(m2)
        check_tlv_error("Anmeldung (M2)", tlv)
        server_pub = tlv.get(TLV_PUBLIC_KEY, b"")[:32]
        if len(server_pub) != 32:
            raise PairingError("Anmeldung (M2): Schlüssel des Geräts fehlt")
        shared = self._private.exchange(X25519PublicKey.from_public_bytes(server_pub))
        key = hkdf_sha512(shared, b"Pair-Verify-Encrypt-Salt", b"Pair-Verify-Encrypt-Info")
        aead = ChaCha20Poly1305(key)
        try:
            plain = aead.decrypt(nonce_for(b"PV-Msg02"), tlv.get(TLV_ENCRYPTED_DATA, b""), None)
        except InvalidTag as exc:
            raise PairingError("Anmeldung (M2): Antwort nicht entschlüsselbar", 2) from exc
        sub = tlv_decode(plain)
        identifier = sub.get(TLV_IDENTIFIER, b"")
        if identifier != self.credentials.atv_id:
            raise PairingError("Anmeldung: ein anderes Gerät antwortet unter dieser Adresse", 2)
        try:
            Ed25519PublicKey.from_public_bytes(self.credentials.ltpk).verify(
                sub.get(TLV_SIGNATURE, b""), server_pub + identifier + self._public)
        except InvalidSignature as exc:
            raise PairingError("Anmeldung: Signatur des Geräts falsch", 2) from exc

        signing = Ed25519PrivateKey.from_private_bytes(self.credentials.ltsk)
        signature = signing.sign(self._public + self.credentials.client_id + server_pub)
        sealed = aead.encrypt(nonce_for(b"PV-Msg03"), tlv_encode(
            [(TLV_IDENTIFIER, self.credentials.client_id), (TLV_SIGNATURE, signature)]), None)
        self.shared_secret = shared
        return tlv_encode([(TLV_STATE, b"\x03"), (TLV_ENCRYPTED_DATA, sealed)])

    def finish(self, m4: bytes) -> bytes:
        if m4:
            check_tlv_error("Anmeldung (M4)", tlv_decode(m4))
        if not self.shared_secret:
            raise PairingError("Anmeldung nicht abgeschlossen")
        return self.shared_secret


# -- HAP-Rahmen (verschlüsselter Steuer- und Ereigniskanal) ----------------------

FRAME_LIMIT = 1024


class HapCipher:
    """ChaCha20-Poly1305 in HAP-Rahmen: [Länge LE16][Daten + 16-Byte-Tag].

    Die Länge ist zugleich die AAD, die Nonce ein Zähler je Richtung.
    """

    def __init__(self, write_key: bytes, read_key: bytes) -> None:
        self._write = ChaCha20Poly1305(write_key)
        self._read = ChaCha20Poly1305(read_key)
        self._write_nonce = 0
        self._read_nonce = 0
        self._inbox = b""

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray()
        for start in range(0, len(data), FRAME_LIMIT):
            chunk = data[start:start + FRAME_LIMIT]
            size = struct.pack("<H", len(chunk))
            out += size + self._write.encrypt(nonce_for(self._write_nonce), chunk, size)
            self._write_nonce += 1
        return bytes(out)

    def decrypt(self, data: bytes) -> bytes:
        """Nimmt Bytes vom Netz an und liefert alles, was vollständig entschlüsselbar ist."""
        self._inbox += data
        out = bytearray()
        while len(self._inbox) >= 2:
            size = struct.unpack("<H", self._inbox[:2])[0]
            if size < 1 or size > FRAME_LIMIT:
                raise PairingError(f"ungültige Rahmenlänge {size}")
            end = 2 + size + 16
            if len(self._inbox) < end:
                break
            try:
                out += self._read.decrypt(nonce_for(self._read_nonce),
                                          self._inbox[2:end], self._inbox[:2])
            except InvalidTag as exc:
                raise PairingError("Steuerkanal: Rahmen nicht entschlüsselbar") from exc
            self._read_nonce += 1
            self._inbox = self._inbox[end:]
        return bytes(out)


def control_cipher(shared_secret: bytes) -> tuple[HapCipher, bytes, bytes]:
    """Cipher für den Steuerkanal und die beiden Rohschlüssel (für shk/shiv)."""
    write_key = hkdf_sha512(shared_secret, b"Control-Salt", b"Control-Write-Encryption-Key")
    read_key = hkdf_sha512(shared_secret, b"Control-Salt", b"Control-Read-Encryption-Key")
    return HapCipher(write_key, read_key), write_key, read_key


def event_cipher(shared_secret: bytes) -> HapCipher:
    """Der Ereigniskanal dreht die Richtungen um: der Empfänger schreibt mit
    „Events-Write“, wir antworten mit „Events-Read“."""
    read_key = hkdf_sha512(shared_secret, b"Events-Salt", b"Events-Write-Encryption-Key")
    write_key = hkdf_sha512(shared_secret, b"Events-Salt", b"Events-Read-Encryption-Key")
    return HapCipher(write_key, read_key)


def video_key(shared_secret: bytes, stream_connection_id: int) -> bytes:
    """Schlüssel des Videostroms: HKDF über das Pair-Verify-Geheimnis."""
    salt = f"DataStream-Salt{stream_connection_id & 0xFFFFFFFFFFFFFFFF}".encode()
    return hkdf_sha512(shared_secret, salt, b"DataStream-Output-Encryption-Key")
