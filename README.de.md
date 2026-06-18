<p align="center">
  <img src="logo.png" alt="Schwupp" width="128">
</p>

<h1 align="center">Schwupp</h1>

<p align="center">
  Bildschirm, YouTube und Medien auf Chromecast- &amp; LG-webOS-Fernseher casten –
  unter Linux, mit nativer Spiegelung unter 1&nbsp;s, ohne Fremdtools.
</p>

---

Eigenständige Linux-App zum **Casten auf den Fernseher** – für **Chromecast/
Google-TV** *und* **LG webOS** (Multi-Backend), auf Desktop und Linux-Phone
(Phosh, z. B. FuriPhone FLX1). Python + GTK4/libadwaita, ohne Abhängigkeit von
fremden Kommandozeilen-Tools wie `catt`.

🇬🇧 An English version of this guide is available in [README.md](README.md).

## Funktionen pro Gerät

| Funktion | Chromecast / Google-TV | LG webOS |
|---|---|---|
| Gerät automatisch finden | ✅ (mDNS `_googlecast`) | ✅ (`_airplay` + 8009-Cast-Probe) |
| Lokale Mediendateien | ✅ HTTP-Server + Range | ✅ via Cast |
| YouTube | ✅ native App | ✅ via Cast |
| Web-Videos (yt-dlp) | ✅ | ✅ |
| Play/Pause/Stopp, Lautstärke | ✅ | ✅ |
| **Bildschirm spiegeln** | ✅ nativ (**<1 s**) / HLS | ✅ nativ (**<1 s**) / HLS (~7 s) |

> **LG webOS:** Viele LG-TVs haben einen **versteckten Google-Cast-Receiver**
> (Port 8009, ohne mDNS-Ankündigung). Schwupp erkennt das (8009-Probe) und nutzt
> den LG wie einen Chromecast — inklusive Bildschirmspiegelung.

## Native Low-Latency-Spiegelung

Schwupp enthält eine **selbst gebaute native Cast-Streaming-Engine**, die den Bildschirm
mit **unter 1 s Latenz** spiegelt (live bestätigt: der Unterschied zwischen Monitor und
TV ist nicht wahrnehmbar). Sie spricht das echte Cast-Streaming-Protokoll:

- H.264-Capture/Encode via GStreamer → **AES-128-CTR** → **Cast-RTP**-Paketierung → UDP
  an den ausgehandelten Port (Offer/Answer über den webrtc-Namespace)
- **RTCP-Sender-Reports** mit einer Uhr, die an die gesendeten Frames gekoppelt **und**
  um den TV↔PC-Uhr-Offset korrigiert ist (live aus den XR-Paketen des Receivers gemessen)
- **Retransmission** als Antwort auf das Cast-NACK-Feedback des Receivers

Eine robuste **HLS-Engine** (~7 s) dient als Fallback. Alle erprobten Wege
(nativ, HLS, DLNA, Browser, AirPlay, Miracast): [docs/MIRRORING.md](docs/MIRRORING.md).

## Architektur

```
app/
  discovery.py     vereinheitlichte Discovery (Chromecast + LG via zeroconf)
  receivers/       Geräte-Backends hinter einer Schnittstelle:
    base.py          Receiver-Interface + Feature-Gating
    chromecast.py    pychromecast
    webos.py         pywebostv (Steuerung/YouTube) + DLNA (Medien)
  dlna.py          minimaler UPnP-AVTransport-Client (für webOS-Medien)
  server/          lokaler HTTP-Server (Dateien mit Range, HLS, Live-Stream)
  sources/         YouTube / Web-Video (yt-dlp)
  mirror/          austauschbare Mirror-Engines (native, hls, dlnats, openscreen)
  updater.py       Selbst-Update (git pull oder GitHub-ZIP)
  ui/              GTK4/libadwaita-Oberfläche (adaptiv Desktop/Phone)
```

Die GUI spricht nur das `Receiver`-Interface; welche Aktionen erscheinen, richtet
sich nach `receiver.supports(...)`.

## Installation

System-Bibliotheken (Manjaro/Arch):

```bash
sudo pacman -S python gtk4 libadwaita gobject-introspection \
    gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly \
    gst-libav yt-dlp
```

Projekt-Abhängigkeiten ins venv (mit Zugriff auf System-GTK/GStreamer):

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

## Ins System einbinden (App-Icon + Menü-Eintrag)

```bash
./installation.sh            # Icon (aus logo.png) + Desktop-Eintrag in ~/.local
./installation.sh --uninstall   # wieder entfernen
```

Läuft im Benutzerkontext (kein `sudo`), installiert keine zusätzlichen Pakete und
legt das Icon als `de.cais.Schwupp` im hicolor-Theme ab; danach erscheint **Schwupp**
im App-Menü und das Fenster nutzt das Logo.

## Starten

```bash
./run.sh
```

Voraussetzung: Gerät und Fernseher im selben Netz; der TV ist eingeschaltet.
Beim ersten Verbinden mit einem LG-TV erscheint dort ein Pairing-Dialog – mit der
Fernbedienung bestätigen (der Schlüssel wird gespeichert).

## Updates

In **Einstellungen → App** gibt es „Nach Updates suchen". Die App vergleicht die
lokale `VERSION` mit der auf GitHub (`misc-de/Schwupp`) und aktualisiert sich selbst:

- **git-Klon mit Remote** → `git fetch` + `git pull`.
- **sonst** (z. B. ZIP-Download) → lädt das ZIP des `main`-Branches und legt es über
  das Verzeichnis (`.git`, `.venv` bleiben unangetastet; die Config liegt ohnehin in
  `~/.config/schwupp` und wird nie überschrieben).

Nach dem Update bietet die App einen Neustart an.

## Credits & Lizenz

Schwupp nutzt u. a. [pychromecast](https://github.com/home-assistant-libs/pychromecast)
(inkl. `casttube`), [pywebostv](https://github.com/supersaiyanmode/PyWebOSTV),
[yt-dlp](https://github.com/yt-dlp/yt-dlp),
[python-zeroconf](https://github.com/python-zeroconf/python-zeroconf),
[cryptography](https://github.com/pyca/cryptography),
[requests](https://github.com/psf/requests),
[PyGObject/GTK4/libadwaita](https://gtk.org/) und
[GStreamer](https://gstreamer.freedesktop.org/) — jeweils unter ihren eigenen Lizenzen
(vollständige Tabelle im [englischen README](README.md#credits)). Das native
Cast-Wire-Format wurde aus den öffentlichen Quellen der
[Chromium Open Screen Library](https://chromium.googlesource.com/openscreen/) abgeleitet
(kein Code kopiert); der Update-Mechanismus ist an
[DrivePulse](https://github.com/misc-de/DrivePulse) angelehnt.

Veröffentlicht unter der [MIT-Lizenz](LICENSE) © 2026 misc-de.
