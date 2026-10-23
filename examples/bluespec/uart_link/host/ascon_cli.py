#!/usr/bin/env python3
"""Run a command against a LeanLink Ascon application."""

from __future__ import annotations

import sys
from pathlib import Path

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from leanlink.ascon_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
