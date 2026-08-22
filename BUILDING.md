# Schwupp aus dem Quelltext bauen

> **Für die normale Nutzung nicht nötig** – das [Flatpak](README.md#installation)
> ist der empfohlene Weg. Diese Seite richtet sich an Entwickler und alle, die
> selbst kompilieren möchten.

Was Schwupp ist und wie man es bedient, steht im [README](README.md).

---

## Voraussetzungen

- **Python ≥ 3.11**
- **GTK 4** und **libadwaita** samt PyGObject-Anbindung
- **GStreamer 1.x** mit den Plugin-Paketen *base*, *good*, *bad*, *ugly* und
  *libav* (x264-Encoder, HLS-Segmentierung, MPEG-TS)
- für die Bildschirmaufnahme: unter X11 nichts weiter, unter Wayland ein
  **ScreenCast-Portal** (`xdg-desktop-portal-gnome`/`-wlr`) **oder**
  `wf-recorder`
- *optional:* `yt-dlp` für Web-Videos (wird sonst ins venv installiert)

### Arch / Manjaro

```bash
sudo pacman -S --needed python python-gobject gtk4 libadwaita \
  gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav \
  wf-recorder            # nur für Wayland ohne Portal
```

### Debian / Ubuntu / Mobian

```bash
sudo apt install python3 python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
  wf-recorder
```

### Fedora

```bash
sudo dnf install python3 python3-gobject gtk4 libadwaita \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free \
  gstreamer1-libav wf-recorder
# gstreamer1-plugins-ugly (x264) kommt aus RPM Fusion
```

---

## Entwickeln

```bash
git clone https://github.com/misc-de/Schwupp.git
cd Schwupp
make venv        # legt .venv mit --system-site-packages an
make run         # startet die App aus dem Quellbaum
make check       # Tests, Linter und Metadaten prüfen
```

`make check` führt aus:

- **pytest** – Protokoll- (Cast-RTP, AES-CTR, RTCP), Server-, Konfigurations-
  und Übersetzungstests. Sie kommen ohne GTK/GStreamer und ohne Fernseher aus.
- **ruff** – Linter (Konfiguration in `pyproject.toml`).
- `desktop-file-validate` und `appstreamcli` für die Metadaten.

---

## Installieren

```bash
sudo make install                    # systemweit nach /usr/local
make install PREFIX=$HOME/.local     # nur für dich (gut fürs Telefon, kein root)
```

Installiert werden der Starter `schwupp`, das Python-Paket, die Übersetzungen
und die VERSION-Datei (unter `<prefix>/share/schwupp`) sowie `.desktop`-Eintrag,
AppStream-Metainfo und Icon. Entfernen mit `make uninstall` (gleiches `PREFIX`).

---

## Flatpak bauen

Das Manifest ist [`de.cais.Schwupp.yaml`](de.cais.Schwupp.yaml) (GNOME-Runtime 49).

```bash
flatpak install flathub org.gnome.Platform//49 org.gnome.Sdk//49
make flatpak-install     # baut und installiert für den angemeldeten Benutzer
flatpak run de.cais.Schwupp
```

Das Manifest bringt drei Dinge mit, die die Runtime nicht hat:

| Modul | wofür |
|---|---|
| `gst-plugins-good-ximagesrc` | X11-Bildschirmaufnahme – in der GNOME-Runtime ist gst-plugins-good ohne X11 gebaut |
| `wf-recorder` | Wayland-Aufnahme auf wlroots-Compositoren ohne Portal-Backend (z. B. phoc auf dem FLX1) |
| `yt-dlp` | gepinnte, per sha256 geprüfte Binary statt Nachladen zur Laufzeit |

Der Build läuft **offline**: Die Python-Abhängigkeiten stecken als gepinnte
Wheels/Sdists in `python3-modules.yaml`. Ändern sich Abhängigkeiten in
`requirements.txt`, muss die Datei neu erzeugt werden:

```bash
make pip-modules     # schreibt python3-modules.yaml neu – anschließend einchecken
```

### Auslieferung: eigenes Repo für beide Architekturen

Schwupp wird über ein selbst gehostetes OSTree-Repo verteilt, das **x86_64 und
aarch64** enthält. Jede Architektur wird nativ gebaut:

```bash
# 1. auf dem PC
make flatpak-build FP_GPG=<fingerprint> FP_GPGHOME=~/.local/share/schwupp-flatpak

# 2. auf dem Telefon (gleiches Repo auschecken)
make flatpak-build
#    danach das dort entstandene repo/ zurückkopieren:
scp -r furios@furis:~/Schwupp/repo repo-arm

# 3. wieder auf dem PC: zusammenführen und veröffentlichen
make flatpak-merge ARM_REPO=repo-arm
make flatpak-publish FP_GPG=<fingerprint> FP_GPGHOME=~/.local/share/schwupp-flatpak
```

`make flatpak-repo-info` zeigt, welche Architekturen im Repo liegen.
Anschließend wird das Verzeichnis `repo/` per HTTPS ausgeliefert (z. B. über
GitHub Pages), passend zur URL in `data/de.cais.Schwupp.flatpakrepo`.

Einen Signierschlüssel legt `make flatpak-gpg-key` an – in einem projekteigenen
GnuPG-Verzeichnis (`~/.local/share/schwupp-flatpak`), nicht im persönlichen
Schlüsselbund. Der öffentliche Teil gehört als `GPGKey=` in die `.flatpakrepo`.

---

## Projektaufbau

```
app/
  __main__.py      Einstiegspunkt, prüft die Laufzeit-Abhängigkeiten
  paths.py         findet VERSION/lang je nach Layout (Git-Klon, Prefix, Flatpak)
  config.py        JSON-Konfiguration mit gerätespezifischen Overrides
  i18n.py          Übersetzungen aus lang/*.json
  deps.py          Prüfung der erforderlichen/optionalen Komponenten
  net.py           LAN-IP passend zum Zielgerät ermitteln
  discovery.py     vereinheitlichte Suche (Cast, webOS, AirPlay, DLNA)
  dlna.py          minimaler UPnP-AVTransport-Client
  updater.py       Selbst-Update (git pull oder geprüftes ZIP-Staging)
  probe.py         Diagnose: was kann ein gefundenes Gerät?
  cast/            pychromecast-Sitzung (verbinden, abspielen, steuern)
  receivers/       Geräte-Backends hinter einer Schnittstelle
    base.py          Receiver-Interface + Feature-Gating
    chromecast.py    Chromecast / Google TV
    webos.py         LG webOS (SSAP-Steuerung + DLNA für Medien)
    airplay.py       AirPlay 2 (pyatv, PIN-Pairing)
    dlna.py          generische UPnP-MediaRenderer
  server/          lokaler HTTP-Server (Dateien mit Range, HLS, Live-Streams)
  sources/         YouTube-/Web-Video-Auflösung (yt-dlp)
  mirror/          Engines fürs Bildschirmspiegeln
    engine.py        Basisklasse: Capture-Auswahl, Worker-Thread, Fehler-Callback
    capture.py       X11/Portal/wf-recorder + Encoder- und Audioquellen-Wahl
    native.py        Cast-Streaming über RTP (Video + Ton, <1 s)
    hls.py           HLS-Segmente über den lokalen Server (robust)
    dlnats.py        endloser MPEG-TS an DLNA-Renderer
    cast_streaming/  Wire-Format: RTP-Paketisierung, AES-CTR, RTCP, Offer/Answer
  ui/              GTK4/libadwaita (adaptiv: Desktop & Telefon)
tests/             Tests ohne GTK/GStreamer und ohne Fernseher
tools/             Prototypen aus der Protokollarbeit (nicht Teil der App)
```
