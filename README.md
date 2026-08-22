<p align="center">
  <img src="logo.png" alt="Schwupp" width="128">
</p>

<h1 align="center">Schwupp</h1>

<p align="center">
  Cast your screen, YouTube and media to Chromecast &amp; LG webOS TVs on Linux,
  with native screen mirroring.
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

| Feature | Chromecast / Google TV | LG webOS (2024+) | AirPlay 2 (Hisense, Samsung …) |
|---|---|---|---|
| Automatic discovery | ✅ | ✅ | ✅ |
| Local media files | ✅ | ✅ | ✅ |
| YouTube | ✅ | ✅ | ✅ (as video stream) |
| Web videos | ✅ | ✅ | ✅ |
| Play / pause / stop, volume | ✅ | ✅ | stop (play/pause device-dependent) |
| Screen mirroring (native, < 1 s) | ✅ | ✅ | — |
| Screen mirroring (HLS) | ✅ | ✅ | ✅ |
| Sound while mirroring | ✅ | ✅ | ✅ |

Most TVs are found and connected automatically. Newer LG webOS TVs (≈2024 and later)
work just like a Chromecast, including screen mirroring.

**AirPlay 2 TVs** (Hisense/VIDAA, Samsung Tizen, Sony, older non-Cast models …) pair
with the code shown on the TV the first time you connect; the pairing is stored, so
this happens only once.

### Other TVs

**Samsung, Sony, Panasonic, Philips and many Hisense** TVs without AirPlay are also
detected via DLNA and can play your media, web videos and YouTube, with playback and
volume control. (Screen mirroring on these needs a Chromecast.)

## Screen mirroring

Mirror your desktop to the TV. (Curious how it works? See
[docs/MIRRORING.md](docs/MIRRORING.md).)

## Architecture

```
app/
  discovery.py     unified discovery (Cast, webOS, AirPlay, DLNA)
  receivers/       device backends behind one interface:
    base.py          Receiver interface + feature gating
    chromecast.py    pychromecast
    webos.py         pywebostv (control/YouTube) + DLNA (media)
    airplay.py       pyatv (AirPlay 2: PIN pairing + play_url)
    dlna.py          generic UPnP media renderers
  dlna.py          minimal UPnP-AVTransport client
  server/          local HTTP server (files w/ Range, HLS, live stream)
  sources/         YouTube / web video (yt-dlp)
  mirror/          pluggable mirror engines (native, hls, dlnats)
  paths.py         locates VERSION/lang (git checkout, prefix, Flatpak)
  updater.py       self-update (git pull or verified ZIP staging)
  ui/              GTK4/libadwaita interface (adaptive desktop/phone)
tests/             protocol, server and config tests — no TV required
```

The GUI only talks to the `Receiver` interface; which actions appear is driven by
`receiver.supports(...)`. Mirror engines share one base class that handles screen
capture selection, the worker thread and error reporting back into the window.

## Installation

### Flatpak (recommended)

Pre-built, **GPG-signed** bundle for **x86_64 and aarch64** — ideal for the
phone, no build tools needed:

```bash
flatpak remote-add --if-not-exists schwupp https://misc-de.github.io/Schwupp/de.cais.Schwupp.flatpakrepo
flatpak install schwupp de.cais.Schwupp
flatpak run de.cais.Schwupp
```

Update later with `flatpak update de.cais.Schwupp`. The signing key is already
embedded in the `.flatpakrepo` file — nothing needs to be imported separately.

The Flatpak brings everything along, including the X11 screen capture element
and `wf-recorder` for wlroots compositors without a screen-cast portal.

> Prefer to run it from a Git checkout, or compile it yourself? See
> [Building from source &amp; project layout](BUILDING.md).

### From a Git checkout

```bash
git clone https://github.com/misc-de/Schwupp.git
cd Schwupp
make venv     # Python environment (uses the system GTK4/GStreamer)
make run
```

The system libraries needed for this route are listed in
[BUILDING.md](BUILDING.md#voraussetzungen). To get an app icon and menu entry,
run `make install PREFIX=$HOME/.local` (no root needed).

## First use

Open **Schwupp** from your app menu. The computer and TV must be on the same network
and the TV must be on. The first time you connect to an LG TV, a pairing dialog appears
on the TV — confirm it with the remote (the key is then stored). On AirPlay TVs
(e.g. Hisense) the TV shows a code instead — type it into the dialog in Schwupp
(also stored, one-time).

On startup Schwupp checks that everything it needs is installed: if something required
is missing it tells you and stops; if only optional parts are missing it lists them and
lets you continue anyway.

## Updates

Installed as a Flatpak: `flatpak update de.cais.Schwupp` (the settings page says
so as well).

Running from a Git checkout: go to **Settings → App → "Check for updates"**.
Schwupp checks for a newer version and updates itself in place — your settings
are kept — and then offers to restart.

## Credits

Schwupp stands on the shoulders of these projects:

| Project | Used for | License |
|---|---|---|
| [pychromecast](https://github.com/home-assistant-libs/pychromecast) | Google Cast protocol (incl. bundled `casttube` for YouTube) | LGPL-2.1 |
| [pywebostv](https://github.com/supersaiyanmode/PyWebOSTV) | LG webOS SSAP control | MIT |
| [pyatv](https://github.com/postlund/pyatv) | AirPlay 2 pairing & playback (Hisense, Samsung …) | MIT |
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
