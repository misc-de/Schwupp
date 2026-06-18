#!/usr/bin/env bash
# Startet Schwupp aus dem projektlokalen venv (mit Zugriff auf System-GTK/GStreamer).
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m app "$@"
