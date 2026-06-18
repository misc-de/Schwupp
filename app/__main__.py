"""Einstiegspunkt: ``python -m app``."""
from __future__ import annotations

import sys


def main() -> int:
    from .ui.app import SchwuppApp

    return SchwuppApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
