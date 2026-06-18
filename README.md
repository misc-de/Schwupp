<p align="center">
  <img src="logo.png" alt="Schwupp" width="128">
</p>

<h1 align="center">Schwupp</h1>

<p align="center">
  Cast your screen, YouTube and media to Chromecast &amp; LG webOS TVs on Linux —
  native sub-second mirroring, no third-party tools.
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

## Quick start

First install the system libraries for your distribution (see
[Installation](#installation) — `pacman` / `apt` / `dnf`), then:

```bash
git clone https://github.com/misc-de/Schwupp.git
cd Schwupp
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
./installation.sh          # registers the app icon + menu entry
```

Now launch **Schwupp** from your application menu. On start it checks its
dependencies: if something **required** is missing it tells you and stops; if only
**optional** parts are missing it lists them and lets you continue anyway.

## Features per device

| Feature | Chromecast / Google TV | LG webOS (2024+) ¹ |
|---|---|---|
| Automatic discovery | ✅ (mDNS `_googlecast`) | ✅ (`_airplay` + port-8009 Cast probe) |
| Local media files | ✅ HTTP server + Range | ✅ via Cast |
| YouTube | ✅ native app | ✅ via Cast |
| Web videos (yt-dlp) | ✅ | ✅ |
| Play/Pause/Stop, volume | ✅ | ✅ |
| **Screen mirroring** | ✅ native (**<1 s**) / HLS | ✅ native (**<1 s**) / HLS (~7 s) |

¹ Newer LG webOS TVs (≈2024+) expose a built-in **hidden Google Cast receiver**
(port 8009, not announced via mDNS). Schwupp detects it automatically and drives the
LG like a Chromecast — including screen mirroring. Older LG models without it are
still usable as DLNA renderers (see below).

### Other TVs via DLNA

**Samsung, Sony, Panasonic, Philips and many Hisense** TVs are discovered
automatically as **UPnP/DLNA media renderers** (SSDP). They play local media files
and yt-dlp-resolved web/YouTube videos with play/pause/stop — and volume when the TV
exposes UPnP RenderingControl. (No screen mirroring — that needs Cast — and no native
app launch; DLNA has no apps.) If a TV is *also* a Cast or webOS device, the richer
backend takes precedence automatically.

## Native low-latency mirroring

Schwupp includes a **hand-built native Cast-streaming engine** that mirrors the screen
with **sub-second latency** (confirmed live: the difference between monitor and TV is
not perceptible). It speaks the real Cast streaming protocol end to end:

- H.264 capture/encode via GStreamer → **AES-128-CTR** encryption → **Cast-RTP**
  packetization → UDP to the negotiated port (Offer/Answer over the webrtc namespace)
- **RTCP Sender Reports** with a clock locked to the sent frames *and* corrected for the
  TV↔PC clock offset (measured live from the receiver's XR packets)
- **Retransmission** in response to the receiver's Cast-NACK feedback

A robust **HLS engine** (~7 s) is available as a fallback. Full write-up of every
approach tried (native, HLS, DLNA, browser, AirPlay, Miracast):
[docs/MIRRORING.md](docs/MIRRORING.md).

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

Install the system libraries for your distribution.

**Arch / Manjaro:**

```bash
sudo pacman -S python gtk4 libadwaita gobject-introspection \
    gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly \
    gst-libav yt-dlp
```

**Debian / Ubuntu** (and derivatives):

```bash
sudo apt install python3 python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly gstreamer1.0-libav yt-dlp
```

**Fedora / CentOS / RHEL** (dnf):

```bash
sudo dnf install python3 python3-gobject gtk4 libadwaita \
    gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free \
    gstreamer1-plugins-ugly gstreamer1-libav yt-dlp
```

> On CentOS/RHEL enable **EPEL** (and **RPM Fusion** for `gstreamer1-plugins-ugly` /
> `gstreamer1-libav`, which carry patent-encumbered codecs). `yt-dlp` can alternatively
> be installed via `pip`.

Then create the project venv (with access to the system GTK/GStreamer):

```bash
python -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

## System integration (app icon + menu entry)

```bash
./installation.sh            # icon (from logo.png) + desktop entry in ~/.local
./installation.sh --uninstall   # remove again
```

Runs in user context (no `sudo`), installs no extra packages, and registers the icon
as `de.cais.Schwupp` in the hicolor theme. Afterwards **Schwupp** appears in your app
menu and launches like any other desktop app.

## First use

Open **Schwupp** from your app menu. The computer and TV must be on the same network
and the TV must be on. The first time you connect to an LG TV, a pairing dialog appears
on the TV — confirm it with the remote (the key is then stored).

## Updates

**Settings → App → "Check for updates"** compares the local `VERSION` with the one on
GitHub (`misc-de/Schwupp`) and updates itself:

- **git clone with a remote** → `git fetch` + `git pull`
- **otherwise** (e.g. ZIP download) → downloads the `main` branch ZIP and overlays it
  (`.git`, `.venv` are left intact; the config lives in `~/.config/schwupp` and is
  never overwritten)

After updating, the app offers to restart.

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
