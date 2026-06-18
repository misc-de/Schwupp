# Bildschirmspiegelung – Stand & Erkenntnisse

## ✅ Ergebnis: LG-Bildschirmspiegelung funktioniert (via Cast)

Der entscheidende Fund: **LG-webOS-TVs haben einen versteckten Google-Cast-Receiver
auf Port 8009** – sie kündigen ihn nur **nicht** per mDNS (`_googlecast`) an, deshalb
findet pychromecasts normale Discovery sie nie. Per **direkter Host-Verbindung**
(`get_chromecast_from_host`) geht Cast aber voll: Media, YouTube **und**
Bildschirmspiegelung via HLS.

| Empfänger | Spiegelung | Weg |
|---|---|---|
| **Chromecast / Google-TV** | ✅ | HLS-Engine (Default Media Receiver) |
| **LG webOS (OLED55G29LA)** | ✅ | versteckter Cast-Receiver (8009) + HLS-Engine |

Discovery probt bei LG-Geräten (gefunden via `_airplay`) den Port 8009 per
TLS-Handshake; bei Erfolg wird das Gerät als **Chromecast** angeboten.

### Was den Cast-HLS-Mirror zum Laufen brachte
Der LG-Cast-Receiver ist wählerischer als ein echter Chromecast. Nötig waren **alle**:
1. **CORS-Header** am HTTP-Server (`Access-Control-Allow-Origin: *`) – der Receiver
   lädt HLS-Segmente per XHR; ohne CORS → `idle_reason=ERROR`. ⭐ Der Hauptfehler.
2. **Master-Playlist** mit `CODECS`-Attribut (nicht nur die nackte Media-Playlist).
3. **H.264 Constrained-Baseline** + **stille AAC-Audiospur** (video-only wird verworfen).
4. **Segment-Vorlauf**: vor `play_media` warten, bis erste Segmente da sind (sonst IDLE).
5. **16:9-Rahmen mit `videoscale add-borders=true`** (Desktop ist 16:10 → sonst verzerrt).

Latenz: **~7 s** (HLS-Receiver-Puffer, kaum zu unterbieten). Für <1 s siehe „Nativ".

### Nativer Low-Latency-Mirror (Ausbaustufe, bestätigt machbar)
Der LG-Cast-Receiver unterstützt das **Cast-Streaming-Protokoll**:
- Mirroring-Receiver-Apps starten ✅ (`0F5096E8` „Chrome Mirroring`, `674A0243` „Screen Mirroring`)
- Namespace `urn:x-cast:com.google.cast.webrtc` vorhanden ✅
- **OFFER/ANSWER-Handshake erfolgreich** ✅ – Receiver liefert `udpPort`, akzeptiert
  H.264+Opus, meldet 3840×2160@60.

Es fehlt nur der **RTP-Sender** (Cast-RTP-Paketisierung + AES-128-CTR + RTCP, H.264
per UDP an den `udpPort`). Das ist openscreens `cast/streaming` in Python – ein
mehrtägiges Vorhaben, aber der Weg ist offen.

### ✅ GELÖST: Nativer Mirror läuft mit <1 s (nicht wahrnehmbar)
Der RTP-Sender ist gebaut und live bestätigt – die `native`-Engine ist jetzt **Default**
(`app/mirror/native.py`, PoC: `tools/native_mirror_v2.py`, Latenzmessung:
`tools/latency_probe.py`). Latenz „als Mensch nicht wahrnehmbar" (gespiegelte Stoppuhr
auf PC vs. TV praktisch synchron). Drei Bausteine, **jeder zwingend**:

1. **frame_id 0-basiert + erstes Frame = Keyframe.** Sonst wartet der Receiver ewig
   auf das nie existierende Frame 0 (Cast-NACK „frame 0 missing", ACK_frame=0xff =
   „nichts dekodiert", Abbruch nach ~10 s).
2. **RTCP-Sender-Report (PT=200)** mit Uhr, die an den letzten Frame gekoppelt **und um
   den TV-Uhr-Offset korrigiert** ist. Die LG-Uhr läuft ~0,8 s hinter der PC-Uhr → ohne
   Korrektur erscheinen Frames „aus der Zukunft" und werden 0,8 s zurückgehalten (= die
   gesamte Rest-Latenz). Offset wird laufend aus dem **XR-Paket** (PT=207, Receiver
   Reference Time) gemessen.
3. **Retransmission auf Cast-NACK** (PT=206, magic `CAST`): der Receiver fordert fehlende
   Pakete an; ein Paketpuffer der letzten ~90 Frames sendet sie erneut. Ohne Resend
   hungert der Decoder.

Datengetriebenes Erfolgssignal (ohne Hinschauen): **ACK_frame zählt mit der gesendeten
frame_id hoch (mod 256), loss bleibt 0.** Receiver-Feedback ist ein RTCP-**Compound**
(RR+XR+NACK) – beim Parsen alle Sub-Pakete durchlaufen. RTP+RTCP teilen sich **einen**
UDP-Port (rtcp-mux). `targetDelay` (150 ms) war übrigens *nicht* der Latenztreiber.

---

## Sackgassen (zur Doku, damit niemand sie erneut durchläuft)

Vor dem Cast-Fund wurden diese Wege erschöpfend getestet – **alle gescheitert**:

- **DLNA-Live** (7 Varianten: TS, fMP4, WebM, fake-size …): Der webOS-DLNA-Player
  (Chromium) braucht eine Datei mit komplettem Index; größenlose Live-Streams werden
  nach ~256 KB Probing verworfen („Datei kann nicht erkannt werden"). MP4-**Dateien**
  mit `faststart` laufen.
- **HLS im TV-Browser** (`com.webos.app.browser` + hls.js): Browser ignoriert die
  Ziel-URL (`target`), bleibt auf der Startseite; `close` → `403`.
- **AirPlay**: `/play`-Endpoint existiert (403 = nur Auth fehlt), aber Pairing scheitert
  – `/pair-pin-start` zeigt einen PIN, doch `/pair-setup-pin` → 403 und HAP `/pair-setup`
  → gekappt. Grund: `SupportsHKPairingAndAccessControl` (HomeKit-Zugriffskontrolle),
  keine TV-Option zum Lockern. AirPlay-*Mirror* bräuchte zudem FairPlay v3/v4 (Apples DRM).
- **Miracast/WFD**: Discovery + GO-Negotiation gelingen, aber die Wi-Fi-Direct-P2P-Gruppe
  ist unter NetworkManager nicht stabil (sofortiges `deinit`), dazu Single-Radio-Channel-
  Konflikt (STA 5 GHz Ch 52 vs. P2P Ch 149/157).
