#!/usr/bin/env python3
"""Run every chain watcher once. This is what the scheduled task executes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import regal_watch as rw
import cinemark_watch as cw

config = rw.load_json(rw.CONFIG_PATH, None)
if config is None:
    print("no config.json")
    sys.exit(2)

status = "--status" in sys.argv
for name, mod in (("regal", rw), ("cinemark", cw)):
    try:
        mod.check_once(config, announce=not status)
    except Exception as e:  # noqa: BLE001
        rw.log(f"{name} ERROR during check: {e!r}")
