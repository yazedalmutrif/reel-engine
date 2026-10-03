"""Shared fixtures for the Reel Engine unit tests.

Safety rules (every test):
- an in-memory keyring replaces Windows Credential Manager (autouse), so no test can
  read or overwrite Yazeed's real Instagram token;
- data_dir and inbox live under tmp_path; toasts and vault writes are off;
- the Instagram hosts point at tests/mock_graph.py, never at graph.instagram.com;
- logging is configured once without the rotating file / console handlers, so no log
  file is held open inside a tmp dir and nothing lands on a captured stream.
"""
from __future__ import annotations

import copy
import sqlite3
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from reelengine import db, log
from reelengine.config import load_config


# ------------------------------------------------------------------ keyring
class MemoryKeyring(KeyringBackend):
    """Dict-backed keyring backend (tests only)."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.store[(service, username)]
        except KeyError:
            raise PasswordDeleteError("not found") from None


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    try:
        yield kr
    finally:
        keyring.set_keyring(previous)


@pytest.fixture(autouse=True, scope="session")
def _quiet_logging():
    # configure the 'reelengine' logger once with no file and no console handler
    log.setup_logging(None, console=False)
    yield


@pytest.fixture(autouse=True)
def _no_crash_env(monkeypatch):
    monkeypatch.delenv("REELENGINE_CRASH_AT", raising=False)


# ------------------------------------------------------------------ config / db
def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def base_overrides(tmp_path: Path) -> dict:
    return {
        "paths": {"data_dir": str(tmp_path / "data"), "inbox": str(tmp_path / "inbox")},
        "notify": {"toast": False},
        "vault": {"enabled": False},
    }


@pytest.fixture
def make_cfg(tmp_path):
    def _make(extra: dict | None = None):
        return load_config(overrides=_merge(base_overrides(tmp_path), extra or {}))

    return _make


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


# ------------------------------------------------------------------ mock Graph API
@pytest.fixture
def mock_graph():
    from mock_graph import MockGraph

    with MockGraph() as m:
        yield m


@pytest.fixture
def graph_cfg(make_cfg, mock_graph):
    """Config whose Instagram hosts are the local mock (fast poll settings)."""
    return make_cfg({
        "instagram": {
            "graph_host": mock_graph.base,
            "container_host": mock_graph.base,
            "upload_host": mock_graph.base,
            "poll_interval_s": 5,
            "poll_timeout_s": 10,
        }
    })


@pytest.fixture
def graph_conn(graph_cfg):
    c = db.connect(graph_cfg.db_path)
    yield c
    c.close()


# ------------------------------------------------------------------ helpers
def all_text_values(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(table, column, value) for every TEXT value in every table."""
    out = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for row in conn.execute(f"SELECT * FROM {t}"):  # nosec B608 - table names from sqlite_master
            for c, v in zip(cols, tuple(row)):
                if isinstance(v, str):
                    out.append((t, c, v))
    return out
