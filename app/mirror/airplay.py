"""AirPlay-Mirror-Engine auf Basis von ``doubletake``.

Warum ein Hilfsprogramm statt des eigenen Capture-Stacks? AirPlay-Spiegelung
ist ein eigenes Protokoll (RTSP + verschlüsselter Videostrom, SRP-6a-Pairing,
ChaCha20-Poly1305) – und für Nicht-Apple-Sender bis vor kurzem gar nicht
verfügbar. ``doubletake`` implementiert es; Schwupp ruft es so auf, wie es auch
``wf-recorder`` und ``yt-dlp`` einbindet: als eigenständiges Programm mit
klarer Schnittstelle.

Das bedeutet zugleich: Diese Engine bringt **ihre eigene** Bildschirmaufnahme
und ihr eigenes Encoding mit. Sie nutzt daher nicht die Capture-Kette der
Basisklasse, sondern startet den Prozess direkt.

Gerätekopplung: Beim ersten Mal zeigt der Fernseher eine PIN, die über den
``pin_callback`` des Receivers abgefragt und dem Prozess auf die Standardeingabe
gereicht wird. Die Zugangsdaten landen neben der App-Konfiguration und werden
danach wiederverwendet.

Netzwerk: Der Empfänger prüft während des Verbindungsaufbaus per UDP einen
Zeitsynchronisations-Port beim Sender. Wird der von einer Firewall verworfen,
kommt die Sitzung zwar zustande, aber **es erscheint kein Bild** – ein
Fehlerbild, das sich nur schwer von einem Codec-Problem unterscheiden lässt.
Deshalb wird ein fester, dokumentierbarer Portbereich verwendet (siehe
:data:`PORT_RANGE` und docs/MIRRORING.md).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .engine import MirrorEngine

# Fester UDP-Bereich für Timing/Audio – so lässt sich eine Firewall-Regel
# angeben, statt zufällige Ports freigeben zu müssen. Drei Ports sind das
# Minimum je gleichzeitigem Empfänger.
PORT_RANGE = "60000-60010"

# H.264 statt HEVC: Mit HEVC verlangt das Protokoll die größte Fläche, die der
# Empfänger meldet – beim getesteten Hisense also 4K. Das Gerät nahm den Strom
# dann zwar an, zeigte aber nur ein Standbild. Mit H.264 (1080p) läuft er rund.
# Wer einen Empfänger hat, der HEVC sauber dekodiert, stellt "hevc" ein.
DEFAULT_CODEC = "h264"

_BINARY = "doubletake"
_START_TIMEOUT = 60.0   # s, bis "mirror session ready" erscheint
_PIN_PROMPT = "Enter the PIN"
_READY_MARKER = "mirror session ready"
_PAIRED_MARKER = "pairing complete"


def _find_binary() -> str | None:
    """Sucht doubletake: erst die Umgebungsvariable, dann PATH, dann /app/bin."""
    override = os.environ.get("SCHWUPP_DOUBLETAKE")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which(_BINARY)
    if found:
        return found
    bundled = Path("/app/bin") / _BINARY      # Flatpak
    return str(bundled) if os.access(bundled, os.X_OK) else None


def credentials_path(config) -> Path:  # noqa: ANN001
    """Ablage der Kopplungsdaten – neben der App-Konfiguration."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "schwupp" / "doubletake-credentials.json"


class AirplayMirrorEngine(MirrorEngine):
    name = "airplay"
    display_name = "AirPlay"

    def __init__(self, receiver, server, config, on_error=None,
                 on_started=None) -> None:  # noqa: ANN001
        super().__init__(receiver, server, config, on_error, on_started)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None

    @staticmethod
    def check_available() -> tuple[bool, str]:
        if _find_binary() is None:
            return False, ("doubletake ist nicht installiert – es überträgt den "
                           "Bildschirm per AirPlay (siehe BUILDING.md)")
        return True, "Bereit"

    # -- Start ---------------------------------------------------------------
    def start(self) -> None:
        """Startet die Spiegelung.

        Überschreibt die Basisklasse: doubletake nimmt den Bildschirm selbst
        auf, eine Capture-Quelle von Schwupp wird also nicht gebraucht.
        """
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._run_guarded, args=("",), daemon=True,
                         name="mirror-airplay").start()

    def run(self, source_desc: str) -> None:  # noqa: ARG002
        binary = _find_binary()
        if binary is None:
            raise RuntimeError(self.check_available()[1])

        creds = credentials_path(self.config)
        creds.parent.mkdir(parents=True, exist_ok=True)
        first_pairing = not creds.exists()

        cmd = [binary, "-target", self.receiver.host, "-creds", str(creds),
               "-port-range", PORT_RANGE, "-fps", str(self.cfg_int("mirror_fps", 30)),
               "-video-codec", str(self.cfg("mirror_airplay_codec") or DEFAULT_CODEC)]
        bitrate = self.cfg_int("mirror_bitrate_kbps", 0)
        if bitrate:
            cmd += ["-bitrate", str(bitrate)]
        latency = self.cfg_int("mirror_target_delay_ms", 0)
        if latency:
            cmd += ["-target-latency-ms", str(latency)]
        if not self.cfg("mirror_audio"):
            cmd.append("-no-audio")
        if first_pairing:
            cmd.append("-pair")

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        self._await_ready(first_pairing)

    def _await_ready(self, first_pairing: bool) -> None:
        """Liest die Ausgabe mit, beantwortet die PIN-Abfrage und wartet auf den
        Start der Sitzung. Danach läuft der Prozess weiter, bis :meth:`stop`."""
        import time

        proc = self._proc
        assert proc is not None and proc.stdout is not None
        deadline = time.monotonic() + _START_TIMEOUT
        tail: list[str] = []

        while self._running and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            tail.append(line.rstrip())
            del tail[:-15]
            if _PIN_PROMPT in line:
                self._answer_pin()
                continue
            if _READY_MARKER in line:
                self._start_log_drain()
                return
        if proc.poll() is not None or not self._running:
            raise RuntimeError(self._explain("\n".join(tail)))
        raise RuntimeError("AirPlay-Spiegelung kam nicht zustande "
                           f"(Zeitüberschreitung):\n{tail[-1] if tail else ''}")

    def _answer_pin(self) -> None:
        """Fragt die am Fernseher angezeigte PIN ab und reicht sie durch."""
        ask = getattr(self.receiver, "pin_callback", None)
        if ask is None:
            raise RuntimeError("Der Fernseher verlangt einen Code, es steht aber "
                               "kein Eingabefeld zur Verfügung")
        pin = ask()
        if not pin:
            raise RuntimeError("Kopplung abgebrochen")
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            proc.stdin.write(f"{pin.strip()}\n")
            proc.stdin.flush()

    def _start_log_drain(self) -> None:
        """Liest die weitere Ausgabe weg, damit die Pipe nicht volläuft, und
        meldet ein unerwartetes Ende der Übertragung."""
        def drain() -> None:
            proc = self._proc
            if proc is None or proc.stdout is None:
                return
            last = ""
            for line in proc.stdout:
                last = line.rstrip()
            if self._running:       # nicht von uns beendet
                self.fail(f"AirPlay-Spiegelung abgebrochen: {last}" if last
                          else "AirPlay-Spiegelung abgebrochen")

        self._reader = threading.Thread(target=drain, daemon=True,
                                        name="mirror-airplay-log")
        self._reader.start()

    @staticmethod
    def _explain(output: str) -> str:
        """Macht aus der Programmausgabe eine Meldung, mit der man etwas anfangen kann."""
        lowered = output.lower()
        if "error: 2" in lowered or "pairing failed" in lowered:
            return ("Kopplung fehlgeschlagen – der Code stimmte nicht oder war "
                    "abgelaufen. Der Fernseher zeigt bei jedem Versuch einen neuen.")
        if "connection refused" in lowered or "no route to host" in lowered:
            return "Der Fernseher ist nicht erreichbar"
        if "credential" in lowered and "empty" in lowered:
            return "Es wurde kein Code eingegeben"
        return f"AirPlay-Spiegelung fehlgeschlagen:\n{output[-400:]}"

    # -- Stop ----------------------------------------------------------------
    def stop(self) -> None:
        self._running = False
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                proc.kill()
