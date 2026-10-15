"""reelengine.macsched: launchd plists (plistlib), install/uninstall/status through a fake
runner. Nothing here touches launchctl or ~/Library/LaunchAgents (home is a tmp dir)."""
from __future__ import annotations

import plistlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from reelengine import macsched

PRINT_SAMPLE = """gui/501/local.reelengine.tick = {
\tactive count = 0
\tpath = /Users/u/Library/LaunchAgents/local.reelengine.tick.plist
\ttype = LaunchAgent
\tstate = not running

\tprogram = /x/.venv/bin/python
\targuments = {
\t\t/x/.venv/bin/python
\t}
\truns = 12
\tlast exit code = 0

\tendpoints = {
\t\tstate = active
\t}
}
"""


class FakeRunner:
    def __init__(self, loaded: set[str] | None = None, bootstrap_rc: int = 0):
        self.calls: list[list[str]] = []
        self.loaded = set(loaded or ())
        self.bootstrap_rc = bootstrap_rc

    def __call__(self, args):
        self.calls.append(list(args))
        assert isinstance(args, list) and args[0] == macsched.LAUNCHCTL
        verb = args[1]
        if verb == "print":
            label = args[2].rsplit("/", 1)[1]
            if label in self.loaded:
                return SimpleNamespace(returncode=0, stdout=PRINT_SAMPLE.encode(), stderr=b"")
            return SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service")
        if verb == "bootstrap":
            if self.bootstrap_rc == 0:
                self.loaded.add(Path(args[3]).stem)
            return SimpleNamespace(returncode=self.bootstrap_rc, stdout=b"", stderr=b"Bootstrap failed: 5: Input/output error")
        if verb == "bootout":
            self.loaded.discard(args[2].rsplit("/", 1)[1])
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected launchctl verb {verb}")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def test_plists_shape(cfg, tmp_path, home):
    root = tmp_path / "repo"
    ps = macsched.plists(cfg, root, home)
    assert set(ps) == {"local.reelengine.tick", "local.reelengine.daily", "local.reelengine.weekly"}
    t = ps["local.reelengine.tick"]
    assert t["ProgramArguments"] == [str(root.resolve() / ".venv/bin/python"), "-m", "reelengine.cli", "tick"]
    assert t["WorkingDirectory"] == str(root.resolve())
    assert t["StartCalendarInterval"] == [{"Minute": 0}, {"Minute": 15}, {"Minute": 30}, {"Minute": 45}]
    assert ps["local.reelengine.daily"]["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}
    assert ps["local.reelengine.weekly"]["StartCalendarInterval"] == {"Weekday": 6, "Hour": 20, "Minute": 0}
    env = t["EnvironmentVariables"]
    assert env == {"PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{home}/.local/bin", "REELENGINE_ROOT": str(root.resolve())}
    assert t["StandardOutPath"].endswith("logs/launchd-tick.log") and t["StandardErrorPath"] == t["StandardOutPath"]
    assert (t["ProcessType"], t["LowPriorityIO"], t["Nice"], t["RunAtLoad"]) == ("Background", True, 5, False)


def test_write_plists_roundtrip_and_no_secrets(cfg, tmp_path, home, memory_keyring):
    from reelengine import secrets

    secrets.set_secret(secrets.TELEGRAM_TOKEN, "123456789:" + "A" * 35)
    out = tmp_path / "launchd"
    paths = macsched.write_plists(cfg, out, tmp_path / "repo", home)
    assert sorted(p.name for p in paths) == ["local.reelengine.daily.plist", "local.reelengine.tick.plist", "local.reelengine.weekly.plist"]
    for p in paths:
        raw = p.read_bytes()
        d = plistlib.loads(raw)
        assert d["Label"] == p.stem
        assert b"AAAAAAAAAA" not in raw and b"token" not in raw.lower() and b"secret" not in raw.lower()
        assert oct(p.stat().st_mode & 0o777) == "0o644"


def test_install_copies_and_bootstraps_with_lists(cfg, home):
    r = FakeRunner()
    done = macsched.install(cfg, runner=r, uid=501, home=home)
    agents = home / "Library" / "LaunchAgents"
    for label in macsched.LABELS.values():
        assert (agents / f"{label}.plist").exists()
        assert [macsched.LAUNCHCTL, "bootstrap", "gui/501", str(agents / f"{label}.plist")] in r.calls
    assert not any(c[1] == "bootout" for c in r.calls)
    assert len(done) == 3


def test_install_is_idempotent_boots_out_old_copy(cfg, home):
    r = FakeRunner(loaded={"local.reelengine.tick"})
    macsched.install(cfg, runner=r, uid=501, home=home)
    assert [macsched.LAUNCHCTL, "bootout", "gui/501/local.reelengine.tick"] in r.calls


def test_install_raises_on_bootstrap_failure(cfg, home):
    with pytest.raises(RuntimeError, match="bootstrap"):
        macsched.install(cfg, runner=FakeRunner(bootstrap_rc=5), uid=501, home=home)


def test_uninstall(cfg, home):
    r = FakeRunner()
    macsched.install(cfg, runner=r, uid=501, home=home)
    done = macsched.uninstall(runner=r, uid=501, home=home)
    assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))
    assert sum(1 for c in r.calls if c[1] == "bootout") == 3
    assert any("removed" in d for d in done)
    assert macsched.uninstall(runner=FakeRunner(), uid=501, home=home) == ["nothing installed"]


def test_status_read_only_and_parsed(home):
    r = FakeRunner(loaded={"local.reelengine.tick"})
    st = macsched.status(runner=r, uid=501, home=home)
    assert st["local.reelengine.tick"] == {"exists": True, "plist_installed": False, "state": "not running", "last_exit_code": "0", "runs": 12}
    assert st["local.reelengine.daily"] == {"exists": False, "plist_installed": False}
    assert all(c[1] == "print" for c in r.calls)


def test_status_survives_runner_error(home):
    def boom(args):
        raise FileNotFoundError("launchctl")

    st = macsched.status(runner=boom, uid=501, home=home)
    assert all(v["exists"] is False and "launchctl" in v["error"] for v in st.values())


def test_parse_print_never_exited():
    out = macsched.parse_print("x = {\n\tstate = waiting\n\tlast exit code = (never exited)\n}\n")
    assert out == {"state": "waiting", "last_exit_code": "(never exited)", "runs": None}


def test_tz_warning(cfg):
    now = datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
    assert macsched.tz_warning(cfg, now, local_offset=timedelta(hours=3)) is None
    w = macsched.tz_warning(cfg, now, local_offset=timedelta(hours=-4))
    assert w and "UTC-04:00" in w and "UTC+03:00" in w and "Asia/Riyadh" in w


def test_install_commands():
    cmds = macsched.install_commands([Path("/r/data/launchd/local.reelengine.tick.plist")], uid=501, home=Path("/h"))
    assert cmds == [["cp", "/r/data/launchd/local.reelengine.tick.plist", "/h/Library/LaunchAgents/local.reelengine.tick.plist"],
                    [macsched.LAUNCHCTL, "bootstrap", "gui/501", "/h/Library/LaunchAgents/local.reelengine.tick.plist"]]
