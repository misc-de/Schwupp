<p align="center">
  <img src="logo.png" alt="Schwupp" width="128">
</p>

<h1 align="center">Schwupp</h1>

<p align="center">
  Cast your screen, YouTube and media to Chromecast &amp; LG webOS TVs on Linux —
  native sub-second mirroring.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/GTK4-libadwaita-green.svg" alt="GTK4 / libadwaita">
</p>

---
⚠️ **AI-assisted project**  

---
**Schwupp** is a self-contained casting app for Linux **desktops** and Linux **phones**
(Phosh, e.g. FuriPhone FLX1). It streams your screen, YouTube and local media to the TV
and supports **both Chromecast / Google TV *and* LG webOS** from a single adaptive
GTK4/libadwaita interface — without depending on external CLI tools like `catt`.

The interface follows your system language (English and German included; English is
the fallback for unsupported locales).

## Features per device

| Feature | Chromecast / Google TV | LG webOS (2024+) |
|---|---|---|
| Automatic discovery | ✅ | ✅ |
| Local media files | ✅ | ✅ |
| YouTube | ✅ | ✅ |
| Web videos | ✅ | ✅ |
| Play / pause / stop, volume | ✅ | ✅ |
| Screen mirroring — high latency (> 7 s) | ✅ | ✅ |
| Screen mirroring — low latency (< 1 s) | ✅ | ✅ |

Most TVs are found and connected automatically. Newer LG webOS TVs (≈2024 and later)
work just like a Chromecast, including screen mirroring.

### Other TVs

**Samsung, Sony, Panasonic, Philips and many Hisense** TVs are also detected and can
play your media, web videos and YouTube, with playback and volume control. (Screen
mirroring on these needs a Chromecast.)

## Screen mirroring

Mirror your desktop to the TV with **sub-second latency**. (Curious how it works? See
[docs/MIRRORING.md](docs/MIRRORING.md).)

## Architecture

```
app/
  discovery.py     unified discovery (Chromecast + LG via zeroconf)
  receivers/       device backends behind one interface:
    base.py          Receiver interface + feature gating
    chromecast.py    pychromecast
    webos.py         pywebostv (control/YouTube) + DLNA (media)
  dlna.py          minimal UPnP-AVTransport client (webOS media)
  server/          local HTTP server (files w/ Range, HLS, live stream)
  sources/         YouTube / web video (yt-dlp)
  mirror/          pluggable mirror engines (native, hls, dlnats, openscreen)
  updater.py       self-update (git pull or GitHub ZIP)
  ui/              GTK4/libadwaita interface (adaptive desktop/phone)
```

The GUI only talks to the `Receiver` interface; which actions appear is driven by
`receiver.supports(...)`.

## Installation

### 1. Install the system libraries

**Arch / Manjaro:**

```bash
sudo pacman -S python gtk4 libadwaita gobject-introspection \
    gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav
```

**Debian / Ubuntu** (and derivatives):

```bash
sudo apt install python3 python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav
```

**Fedora / CentOS / RHEL** (dnf):

```bash
sudo dnf install python3 python3-gobject gtk4 libadwaita \
    gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free \
    gstreamer1-plugins-ugly gstreamer1-libav
```

> On CentOS/RHEL enable **EPEL** (and **RPM Fusion** for `gstreamer1-plugins-ugly` /
> `gstreamer1-libav`, which carry patent-encumbered codecs).

The Python dependencies (including `yt-dlp`) are installed into the venv in the next
step — no extra system package needed.

### 2. Download Schwupp and set up its Python environment

```bash
git clone https://github.com/misc-de/Schwupp.git
cd Schwupp
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Add Schwupp to your system (app icon + menu entry)

From inside the cloned `Schwupp` folder:

```bash
./installation.sh            # icon + menu entry in ~/.local (no sudo)
./installation.sh --uninstall   # remove again
```

Afterwards **Schwupp** appears in your app menu and launches like any other desktop
app — no terminal needed.

## First use

Open **Schwupp** from your app menu. The computer and TV must be on the same network
and the TV must be on. The first time you connect to an LG TV, a pairing dialog appears
on the TV — confirm it with the remote (the key is then stored).

On startup Schwupp checks that everything it needs is installed: if something required
is missing it tells you and stops; if only optional parts are missing it lists them and
lets you continue anyway.

## Updates

Go to **Settings → App → "Check for updates"**. Schwupp checks for a newer version and
updates itself in place — your settings are kept — and then offers to restart.

## Credits

Schwupp stands on the shoulders of these projects:

| Project | Used for | License |
|---|---|---|
| [pychromecast](https://github.com/home-assistant-libs/pychromecast) | Google Cast protocol (incl. bundled `casttube` for YouTube) | LGPL-2.1 |
| [pywebostv](https://github.com/supersaiyanmode/PyWebOSTV) | LG webOS SSAP control | MIT |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | web video extraction (fallback) | Unlicense |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | mDNS service discovery | LGPL-2.1 |
| [cryptography](https://github.com/pyca/cryptography) | AES-128-CTR for the native mirror | Apache-2.0 / BSD |
| [requests](https://github.com/psf/requests) | HTTP for the updater | Apache-2.0 |
| [PyGObject](https://pygobject.gnome.org/) · [GTK4](https://gtk.org/) · [libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) | the user interface | LGPL |
| [GStreamer](https://gstreamer.freedesktop.org/) | screen capture & H.264 encoding | LGPL |

The native Cast-streaming wire format was derived from the public
[Chromium Open Screen Library](https://chromium.googlesource.com/openscreen/) /
`media/cast` sources (no code copied). The self-update flow is modeled on the author's
[DrivePulse](https://github.com/misc-de/DrivePulse) app.

These dependencies keep their own licenses; the table above is informational.

## License

Released under the [MIT License](LICENSE) © 2026 misc-de.
