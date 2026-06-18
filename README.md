# Schwupp

Eigenständige Linux-App zum **Casten auf den Fernseher** – für **Chromecast/
Google-TV** *und* **LG webOS** (Multi-Backend), auf Desktop und Linux-Phone
(Phosh, z. B. FuriPhone FLX1). Python + GTK4/libadwaita, ohne Abhängigkeit von
fremden Kommandozeilen-Tools wie `catt`.

## Funktionen pro Gerät

| Funktion | Chromecast / Google-TV | LG webOS |
|---|---|---|
| Gerät automatisch finden | ✅ (mDNS `_googlecast`) | ✅ (`_airplay` + 8009-Cast-Probe) |
| Lokale Mediendateien | ✅ HTTP-Server + Range | ✅ via Cast |
| YouTube | ✅ native App | ✅ via Cast |
| Web-Videos (yt-dlp) | ✅ | ✅ |
| Play/Pause/Stopp, Lautstärke | ✅ | ✅ |
| **Bildschirm spiegeln** | ✅ HLS-Engine | ✅ HLS-Engine (~7 s Latenz) |

> **LG webOS:** Viele LG-TVs haben einen **versteckten Google-Cast-Receiver**
> (Port 8009, ohne mDNS-Ankündigung). Schwupp erkennt das (8009-Probe) und nutzt
> den LG wie einen Chromecast — inkl. **Bildschirmspiegelung via HLS** (~7 s Latenz).
> Für echtes Low-Latency-Mirroring (<1 s) ist der native Cast-Streaming-Sender als
> Ausbaustufe geplant (Offer/Answer bereits bestätigt). Details und alle erprobten
> Wege (DLNA/Browser/AirPlay/Miracast): [docs/MIRRORING.md](docs/MIRRORING.md).

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
  mirror/          austauschbare Mirror-Engines (hls, dlnats, native, openscreen)
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
  das Verzeichnis (`.git`, `.venv` u. a. bleiben unangetastet; die Config liegt
  ohnehin in `~/.config/schwupp` und wird nie überschrieben).

Nach dem Update bietet die App einen Neustart an.

**Release veröffentlichen** (für den Maintainer): `VERSION` erhöhen, committen und
nach `misc-de/Schwupp` (Branch `main`) pushen – Clients sehen die neue Version dann
beim nächsten Update-Check.

## Status

Funktionsfähig und live getestet (LG OLED55G29LA): Discovery, Pairing, YouTube,
lokale Medien, Steuerung. Chromecast-Pfad inkl. HLS-Bildschirmspiegelung
implementiert (testbar mit einem Cast-Gerät). Offene Ausbaustufen: native
Cast-Streaming-Engine (Low-Latency-Mirror auf Chromecast), Verfeinerung der
webOS-Lautstärke.
