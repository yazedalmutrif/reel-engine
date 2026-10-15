"""macOS launchd jobs for the engine (the Mac twin of scripts/register-tasks.ps1).

Three LaunchAgents in the user's GUI domain (they run only while Yazeed is logged in,
which is also when the login Keychain is unlocked for keyring):
  local.reelengine.tick    every 15 min at :00 :15 :30 :45
  local.reelengine.daily   every day at 03:30
  local.reelengine.weekly  every Saturday at 20:00 (launchd Weekday 6)

launchd calendar intervals use the Mac's system time zone, so status() warns when that
differs from [engine] timezone. Plists are built with plistlib (never string templates)
and hold no secrets. Every launchctl call is a list (no shell) through an injectable
`runner`, so tests never touch launchd. `install` / `uninstall` are run only by Yazeed
(`reel schedule install --mac` asks for a typed confirmation).
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess  # nosec B404 - fixed binary (launchctl), list args, no shell
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

JOBS = ("tick", "daily", "weekly")
LABEL_PREFIX = "local.reelengine."
LABELS = {job: LABEL_PREFIX + job for job in JOBS}
LAUNCHCTL = "/bin/launchctl"
BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

Runner = Callable[[list], "subprocess.CompletedProcess"]


def default_runner(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, timeout=30)  # nosec B603 - list args, fixed binary, no shell


def calendar(job: str) -> list[dict] | dict:
    if job == "tick":
        return [{"Minute": m} for m in (0, 15, 30, 45)]
    if job == "daily":
        return {"Hour": 3, "Minute": 30}
    if job == "weekly":
        return {"Weekday": 6, "Hour": 20, "Minute": 0}  # launchd: 0/7 = Sunday, 6 = Saturday
    raise ValueError(f"unknown job {job!r}")


def plist_dict(job: str, root: Path, log_dir: Path, home: Path | None = None) -> dict:
    root = Path(root).resolve()
    home = Path(home or Path.home())
    return {
        "Label": LABELS[job],
        "ProgramArguments": [str(root / ".venv" / "bin" / "python"), "-m", "reelengine.cli", job],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": f"{BASE_PATH}:{home / '.local' / 'bin'}", "REELENGINE_ROOT": str(root)},
        "StartCalendarInterval": calendar(job),
        "StandardOutPath": str(Path(log_dir) / f"launchd-{job}.log"),
        "StandardErrorPath": str(Path(log_dir) / f"launchd-{job}.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "RunAtLoad": False,
    }


def plists(cfg, root: Path | None = None, home: Path | None = None) -> dict[str, dict]:
    """{label: plist dict} for the three jobs."""
    root = Path(root or cfg.root)
    log_dir = cfg.data_dir / "logs"
    return {LABELS[j]: plist_dict(j, root, log_dir, home) for j in JOBS}


def write_plists(cfg, out_dir: Path, root: Path | None = None, home: Path | None = None) -> list[Path]:
    """Write <label>.plist files into out_dir (and create data/logs). Installs nothing."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    paths = []
    for label, d in plists(cfg, root, home).items():
        p = out_dir / f"{label}.plist"
        with p.open("wb") as fh:
            plistlib.dump(d, fh, fmt=plistlib.FMT_XML, sort_keys=True)
        os.chmod(p, 0o644)
        paths.append(p)
    return paths


def agents_dir(home: Path | None = None) -> Path:
    return Path(home or Path.home()) / "Library" / "LaunchAgents"


def domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def install_commands(paths: list[Path], uid: int | None = None, home: Path | None = None) -> list[list[str]]:
    """The exact commands `install` runs (also printed by `reel schedule plist`)."""
    cmds = []
    for p in paths:
        dest = agents_dir(home) / p.name
        cmds.append(["cp", str(p), str(dest)])
        cmds.append([LAUNCHCTL, "bootstrap", domain(uid), str(dest)])
    return cmds


def _loaded(label: str, runner: Runner, uid: int | None) -> bool:
    return runner([LAUNCHCTL, "print", f"{domain(uid)}/{label}"]).returncode == 0


def install(cfg, *, runner: Runner = default_runner, uid: int | None = None, home: Path | None = None,
            root: Path | None = None) -> list[str]:
    """Write the plists, copy them to ~/Library/LaunchAgents and bootstrap them (a loaded job
    is booted out first, so re-running updates it). Returns a log of what was done."""
    done: list[str] = []
    src = write_plists(cfg, cfg.data_dir / "launchd", root, home)
    dest_dir = agents_dir(home)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for p in src:
        label = p.stem
        dest = dest_dir / p.name
        if _loaded(label, runner, uid):
            runner([LAUNCHCTL, "bootout", f"{domain(uid)}/{label}"])
            done.append(f"booted out the old {label}")
        shutil.copyfile(p, dest)
        os.chmod(dest, 0o644)
        r = runner([LAUNCHCTL, "bootstrap", domain(uid), str(dest)])
        if r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", "replace").strip() if isinstance(r.stderr, bytes) else str(r.stderr or "")
            raise RuntimeError(f"launchctl bootstrap {label} failed (rc={r.returncode}): {err[:300]}")
        done.append(f"installed {label} -> {dest}")
    return done


def uninstall(*, runner: Runner = default_runner, uid: int | None = None, home: Path | None = None) -> list[str]:
    done: list[str] = []
    for label in LABELS.values():
        if _loaded(label, runner, uid):
            runner([LAUNCHCTL, "bootout", f"{domain(uid)}/{label}"])
            done.append(f"booted out {label}")
        f = agents_dir(home) / f"{label}.plist"
        if f.exists():
            f.unlink()
            done.append(f"removed {f}")
    return done or ["nothing installed"]


_KV = re.compile(r"^\t([A-Za-z][\w ]*?) = (.*)$", re.MULTILINE)


def parse_print(text: str) -> dict:
    """Pick state / last exit code / runs out of `launchctl print` (top-level keys only)."""
    kv: dict[str, str] = {}
    for k, v in _KV.findall(text):
        kv.setdefault(k.strip(), v.strip())
    runs = kv.get("runs")
    return {
        "state": kv.get("state"),
        "last_exit_code": kv.get("last exit code"),
        "runs": int(runs) if runs and runs.isdigit() else runs,
    }


def status(*, runner: Runner = default_runner, uid: int | None = None, home: Path | None = None) -> dict[str, dict]:
    """Read-only: {label: {exists, plist_installed, state, last_exit_code, runs}}."""
    out: dict[str, dict] = {}
    for label in LABELS.values():
        installed = (agents_dir(home) / f"{label}.plist").exists()
        try:
            r = runner([LAUNCHCTL, "print", f"{domain(uid)}/{label}"])
        except Exception as exc:  # noqa: BLE001
            out[label] = {"exists": False, "plist_installed": installed, "error": str(exc)[:200]}
            continue
        if r.returncode != 0:
            out[label] = {"exists": False, "plist_installed": installed}
            continue
        text = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else str(r.stdout or "")
        out[label] = {"exists": True, "plist_installed": installed, **parse_print(text)}
    return out


def tz_warning(cfg, now: datetime | None = None, local_offset: timedelta | None = None) -> str | None:
    """launchd fires on the Mac's local clock; warn if it isn't the engine's time zone offset."""
    now = now or datetime.now().astimezone()
    local = local_offset if local_offset is not None else now.astimezone().utcoffset()
    engine = now.astimezone(cfg.tz).utcoffset()
    if local != engine:
        return (f"this Mac's time zone (UTC{_fmt(local)}) differs from engine.timezone {cfg['engine']['timezone']} "
                f"(UTC{_fmt(engine)}): launchd runs daily/weekly on the Mac's clock. Set the Mac to Riyadh time "
                f"(System Settings > General > Date & Time) or the jobs fire at the wrong hour.")
    return None


def _fmt(td) -> str:
    mins = int(td.total_seconds() // 60) if td is not None else 0
    sign = "+" if mins >= 0 else "-"
    return f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
