#!/usr/bin/env python3
"""Latenz-Messung des nativen Mirrors per laufender Stoppuhr.

Öffnet ein großes Uhr-Fenster (Sekunden.Hundertstel). Der Bildschirm wird
gespiegelt, also erscheint dieselbe Uhr verzögert am TV. Differenz der
abgelesenen Werte (PC-Monitor vs. TV) = End-to-End-Latenz.

Aufruf:  DISPLAY=:0.0 .venv/bin/python tools/latency_probe.py [target_delay_ms]
"""
import importlib.util
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

# Großes Uhr-Fenster: läuft mit, wird mitgespiegelt.
clock = Gst.parse_launch(
    'videotestsrc is-live=true pattern=black ! video/x-raw,width=900,height=460,framerate=60/1 '
    '! timeoverlay halignment=center valignment=center font-desc="Monospace Bold 120" '
    'time-mode=elapsed-running-time ! videoconvert ! ximagesink sync=false'
)
clock.set_state(Gst.State.PLAYING)

# GLib-MainLoop in eigenem Thread, damit das Fenster Events bekommt.
loop = GLib.MainLoop()
threading.Thread(target=loop.run, daemon=True).start()
print("Uhr-Fenster offen. Vergleiche SS.hh auf PC-Monitor vs. TV.", flush=True)

# native_mirror_v2.main() laden und starten (spiegelt den Bildschirm inkl. Uhr).
path = os.path.join(os.path.dirname(__file__), "native_mirror_v2.py")
spec = importlib.util.spec_from_file_location("nmv2", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    m.main()
finally:
    clock.set_state(Gst.State.NULL)
    loop.quit()
