"""Read-only view of the engine's Windows Task Scheduler tasks (for `reel doctor`).
Registering tasks is done only by scripts/register-tasks.ps1, run by Yazeed."""
from __future__ import annotations

import csv
import io
import subprocess  # nosec B404
import sys

TASK_NAMES = ("Brain - Reel Engine Tick", "Brain - Reel Engine Daily", "Brain - Reel Engine Weekly")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def parse_schtasks_csv(text: str) -> dict:
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {"exists": False}
    r = rows[0]
    return {
        "exists": True,
        "state": r.get("Status") or r.get("Scheduled Task State"),
        "next_run": r.get("Next Run Time"),
        "last_run": r.get("Last Run Time"),
        "last_result": r.get("Last Result"),
        "logon_mode": r.get("Logon Mode"),
    }


def tasks_status() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in TASK_NAMES:
        try:
            p = subprocess.run(  # nosec B603 B607 - fixed args
                ["schtasks", "/Query", "/TN", name, "/V", "/FO", "CSV"],
                capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW,
            )
            if p.returncode != 0:
                out[name] = {"exists": False}
                continue
            out[name] = parse_schtasks_csv(p.stdout.decode(errors="replace"))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"exists": False, "error": str(exc)}
    return out
