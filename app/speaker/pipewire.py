"""Ausgaben in PipeWire/PulseAudio, über die der Ton des Rechners abfließt.

* :class:`NullSink` – eine virtuelle Ausgabe (``module-null-sink``). Sie wird
  Standardausgabe, alle Programme spielen hinein, die Lautsprecher des Rechners
  bleiben still; ihr Monitor liefert den Ton für den Live-Stream. Läuft auch
  ohne Wiedergabe weiter (dann Stille), der Stream reißt also nicht ab.
* :class:`RaopSink` – eine AirPlay-Ausgabe aus PipeWires RAOP-Modul. Sie läuft
  in einem eigenen kleinen ``pipewire``-Prozess, weil pipewire-pulse das Modul
  nicht über ``pactl`` laden kann; endet der Prozess, verschwindet die Ausgabe.

Beide merken sich die vorherige Standardausgabe und stellen sie wieder her.
Die Lautstärketasten regeln danach die neue Ausgabe – bei AirPlay reicht
PipeWire das an den Lautsprecher weiter, beim Live-Stream skaliert der Monitor
den Pegel (``monitor.channel-volumes``).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

NULL_SINK = "schwupp_sound"
RAOP_SINK = "schwupp_raop"
# So lange darf es dauern, bis Avahi den Lautsprecher aufgelöst hat und
# PipeWires RAOP-Ausgabe erscheint.
RAOP_TIMEOUT_S = 15


def pactl(*args: str, timeout: float = 5) -> str:
    """``pactl`` aufrufen; wirft RuntimeError mit der Meldung von pactl."""
    try:
        out = subprocess.run(["pactl", *args], capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"pactl {args[0]}: {exc}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"pactl {args[0]}: {out.stderr.strip() or out.returncode}")
    return out.stdout.strip()


def pactl_available() -> bool:
    return shutil.which("pactl") is not None


def raop_available() -> bool:
    """Gibt es PipeWire samt RAOP-Modul (Arch: pipewire-zeroconf)?"""
    if shutil.which("pipewire") is None:
        return False
    return any(Path(d, "pipewire-0.3", "libpipewire-module-raop-discover.so").exists()
               for d in ("/usr/lib", "/usr/lib64", "/usr/lib/aarch64-linux-gnu",
                         "/usr/lib/x86_64-linux-gnu"))


def sinks() -> list[str]:
    """Namen aller Ausgaben."""
    return [line.split("\t")[1] for line in pactl("list", "short", "sinks").splitlines()
            if "\t" in line]


def default_sink() -> str:
    return pactl("get-default-sink")


def _quote(text: str) -> str:
    """Wert innerhalb von ``sink_properties`` (in einfachen Anführungszeichen;
    die ganze Liste steht in doppelten)."""
    return "'" + text.replace("'", "").replace('"', "") + "'"


class _DefaultSwitch:
    """Macht eine Ausgabe zur Standardausgabe und kann zurückwechseln."""

    def __init__(self) -> None:
        self._previous: str | None = None

    def _make_default(self, sink: str) -> None:
        previous = default_sink()
        if previous != sink:
            self._previous = previous
        pactl("set-default-sink", sink)

    def _restore_default(self, gone: str) -> None:
        previous, self._previous = self._previous, None
        if not previous or previous == gone:
            return
        with contextlib.suppress(RuntimeError):
            if previous in sinks():
                pactl("set-default-sink", previous)


class NullSink(_DefaultSwitch):
    """Virtuelle Ausgabe als Standard, deren Monitor den Live-Stream speist."""

    def __init__(self, description: str) -> None:
        super().__init__()
        self.description = description
        self._module: str | None = None

    @property
    def monitor(self) -> str:
        return f"{NULL_SINK}.monitor"

    def start(self) -> None:
        remove_stale_sinks()
        self._module = pactl(
            "load-module", "module-null-sink", f"sink_name={NULL_SINK}",
            f'sink_properties="device.description={_quote(self.description)}'
            ' monitor.channel-volumes=true device.icon_name=audio-speakers"')
        self._make_default(NULL_SINK)

    def stop(self) -> None:
        module, self._module = self._module, None
        self._restore_default(NULL_SINK)
        if module:
            with contextlib.suppress(RuntimeError):
                pactl("unload-module", module)


def remove_stale_sinks() -> None:
    """Räumt eine virtuelle Ausgabe weg, die ein abgestürztes Schwupp
    hinterlassen hat – sonst bliebe der Rechner stumm."""
    with contextlib.suppress(RuntimeError):
        for line in pactl("list", "short", "modules").splitlines():
            fields = line.split("\t")
            if len(fields) >= 3 and f"sink_name={NULL_SINK}" in fields[2]:
                pactl("unload-module", fields[0])


class RaopSink(_DefaultSwitch):
    """AirPlay-Ausgabe für genau einen Lautsprecher.

    Ein eigener ``pipewire``-Prozess lädt ``raop-discover`` mit einer Regel, die
    nur diesen Lautsprecher (IP und Port) zu einer Ausgabe macht. Die TXT-Daten
    (Verschlüsselung, Codec, Transport) wertet so PipeWire selbst aus, wie für
    jeden anderen AirPlay-Lautsprecher auch.
    """

    def __init__(self, host: str, port: int, description: str) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.description = description
        self._proc: subprocess.Popen | None = None
        self._conf: Path | None = None
        self._log: Path | None = None

    def _config(self) -> str:
        desc = self.description.replace('"', "")
        return f"""# Schwupp: AirPlay-Ausgabe für {desc}
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-raop-discover
        args = {{
            stream.rules = [
                {{ matches = [ {{ raop.ip = "{self.host}" raop.port = "{self.port}" }} ]
                   actions = {{ create-stream = {{
                       node.name = "{RAOP_SINK}"
                       node.description = "{desc}"
                       device.icon_name = "audio-speakers"
                       # sonst aux0/aux1: Stereo landete ungemischt auf Hilfskanälen
                       audio.position = [ FL FR ]
                   }} }} }}
            ]
        }}
    }}
]
"""

    def start(self) -> None:
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        self._conf = runtime / "schwupp" / "raop.conf"
        self._conf.parent.mkdir(parents=True, exist_ok=True)
        self._conf.write_text(self._config(), encoding="utf-8")
        # Ausgabe in eine Datei: eine Pipe, die keiner liest, hielte PipeWire an
        self._log = self._conf.with_suffix(".log")
        with self._log.open("w", encoding="utf-8") as log:
            self._proc = subprocess.Popen(["pipewire", "-c", str(self._conf)],
                                          stdout=subprocess.DEVNULL, stderr=log)
        deadline = time.monotonic() + RAOP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                err = self._log.read_text(encoding="utf-8", errors="replace").strip()
                self._proc = None
                raise RuntimeError(f"PipeWire-AirPlay-Ausgabe beendet: {err[-300:]}")
            if RAOP_SINK in sinks():
                self._make_default(RAOP_SINK)
                return
            time.sleep(0.3)
        self.stop()
        raise RuntimeError("Der AirPlay-Lautsprecher wurde nicht gefunden "
                           "(Avahi aktiv? Lautsprecher im selben Netz?)")

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        self._restore_default(RAOP_SINK)
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        for path in (self._conf, self._log):
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()
        self._conf = self._log = None
