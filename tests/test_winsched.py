"""reelengine.winsched: parsing `schtasks /Query /V /FO CSV` output and the
read-only tasks_status() (subprocess mocked: nothing touches Task Scheduler)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reelengine import winsched

HEADER = ('"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result",'
          '"Author","Task To Run","Start In","Comment","Scheduled Task State","Run As User","Schedule Type"')
ROW_TICK = ('"PC1","\\Brain - Reel Engine Tick","9/30/2026 10:15:00 AM","Ready","Interactive only",'
            '"9/30/2026 10:00:00 AM","0","PC1\\user","C:\\Users\\user\\Projects\\ReelEngine\\.venv\\Scripts\\pythonw.exe -m reelengine.cli tick",'
            '"C:\\Users\\user\\Projects\\ReelEngine","Reel Engine tick job","Enabled","user","One Time Only, Minute"')
ROW_TICK_2ND_TRIGGER = ROW_TICK.replace('"Ready"', '"Running"').replace('"0"', '"267009"')
SAMPLE = HEADER + "\r\n" + ROW_TICK + "\r\n"

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_parse_sample_csv():
    r = winsched.parse_schtasks_csv(SAMPLE)
    assert r == {
        "exists": True,
        "state": "Ready",
        "next_run": "9/30/2026 10:15:00 AM",
        "last_run": "9/30/2026 10:00:00 AM",
        "last_result": "0",
        "logon_mode": "Interactive only",
    }


def test_parse_uses_first_row_when_task_has_several_triggers():
    r = winsched.parse_schtasks_csv(HEADER + "\n" + ROW_TICK + "\n" + ROW_TICK_2ND_TRIGGER + "\n")
    assert r["state"] == "Ready" and r["last_result"] == "0"


def test_parse_falls_back_to_scheduled_task_state():
    text = '"TaskName","Scheduled Task State","Last Result"\n"\\Brain - Reel Engine Daily","Disabled","0"\n'
    r = winsched.parse_schtasks_csv(text)
    assert r["exists"] is True and r["state"] == "Disabled"
    assert r["next_run"] is None and r["logon_mode"] is None


@pytest.mark.parametrize("text", ["", HEADER + "\r\n"], ids=["empty", "header-only"])
def test_parse_no_rows_means_absent(text):
    assert winsched.parse_schtasks_csv(text) == {"exists": False}


def test_tasks_status_absent_tasks(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(winsched.subprocess, "run", fake_run)
    out = winsched.tasks_status()
    assert out == {name: {"exists": False} for name in winsched.TASK_NAMES}
    assert [c[3] for c in calls] == list(winsched.TASK_NAMES)
    for c in calls:  # read-only query, nothing else
        assert c[:2] == ["schtasks", "/Query"] and "/Create" not in c and "/Delete" not in c and "/Change" not in c


def test_tasks_status_mixed(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[3] == "Brain - Reel Engine Tick":
            return SimpleNamespace(returncode=0, stdout=SAMPLE.encode(), stderr=b"")
        if argv[3] == "Brain - Reel Engine Daily":
            raise FileNotFoundError("schtasks not found")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(winsched.subprocess, "run", fake_run)
    out = winsched.tasks_status()
    assert out["Brain - Reel Engine Tick"]["exists"] is True
    assert out["Brain - Reel Engine Tick"]["last_result"] == "0"
    assert out["Brain - Reel Engine Daily"]["exists"] is False and "schtasks not found" in out["Brain - Reel Engine Daily"]["error"]
    assert out["Brain - Reel Engine Weekly"] == {"exists": False}


def test_task_names_match_the_scripts():
    """register-tasks.ps1 / unregister-tasks.ps1 must use exactly the names doctor looks for."""
    assert winsched.TASK_NAMES == ("Brain - Reel Engine Tick", "Brain - Reel Engine Daily", "Brain - Reel Engine Weekly")
    for script in ("register-tasks.ps1", "unregister-tasks.ps1"):
        text = (SCRIPTS / script).read_text(encoding="utf-8")
        for name in winsched.TASK_NAMES:
            assert f"'{name}'" in text, f"{script} is missing {name!r}"


def test_mac_jobs_mirror_the_windows_tasks():
    """macsched (launchd) runs the same three jobs at the same times as register-tasks.ps1."""
    from reelengine import macsched

    text = (SCRIPTS / "register-tasks.ps1").read_text(encoding="utf-8")
    assert "-Daily -At '03:30'" in text and "-DaysOfWeek Saturday -At '20:00'" in text and "-Minutes 15" in text
    assert macsched.JOBS == ("tick", "daily", "weekly") and len(winsched.TASK_NAMES) == 3
    assert macsched.calendar("daily") == {"Hour": 3, "Minute": 30}
    assert macsched.calendar("weekly") == {"Weekday": 6, "Hour": 20, "Minute": 0}
    assert [d["Minute"] for d in macsched.calendar("tick")] == [0, 15, 30, 45]
