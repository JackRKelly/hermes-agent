"""The kanban dashboard runs ``init_db`` (migrations + full integrity probe) once
per DB file per process, not on every request, and re-runs it when the file goes
missing or is replaced.

Every dashboard request used to call ``init_db``, which clears the connect cache
on purpose, so each ``/board`` read paid a full ``PRAGMA integrity_check``.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_conn_init_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def probed(monkeypatch):
    """Record every DB path the integrity probe runs on."""
    calls: list[Path] = []
    real = kbc._probe_integrity

    def _counting(path):
        calls.append(Path(path).resolve())
        return real(path)

    monkeypatch.setattr(kbc, "_probe_integrity", _counting)
    return calls


def _board(client, board=None):
    url = "/api/plugins/kanban/board" + (f"?board={board}" if board else "")
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def _task_titles(board_json):
    return {t["title"] for c in board_json["columns"] for t in c["tasks"]}


def _replace_with_copy(db: Path) -> None:
    """Swap ``db`` for a consistent copy of itself: same content, new file."""
    copy = db.with_name("kanban.copy.db")
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    with src, dst:
        src.backup(dst)
    src.close()
    dst.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    os.replace(copy, db)


def test_repeat_requests_probe_integrity_once(client, probed):
    db = kb.kanban_db_path().resolve()
    _board(client)
    _board(client)
    _board(client)
    assert probed == [db]


def test_replaced_db_file_is_probed_again(client, probed):
    db = kb.kanban_db_path().resolve()
    assert client.post("/api/plugins/kanban/tasks", json={"title": "survives the swap"}).status_code == 200
    _board(client)
    _replace_with_copy(db)
    after = _board(client)
    assert probed == [db, db]
    assert "survives the swap" in _task_titles(after)


def test_deleted_db_file_is_reinitialised(client):
    db = kb.kanban_db_path().resolve()
    _board(client)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    r = client.post("/api/plugins/kanban/tasks", json={"title": "after delete"})
    assert r.status_code == 200, r.text
    assert _task_titles(_board(client)) == {"after delete"}


def test_each_board_is_initialised_once(client, probed):
    for slug in ("alpha", "beta"):
        assert client.post("/api/plugins/kanban/boards", json={"slug": slug}).status_code == 200
    probed.clear()
    alpha = kb.kanban_db_path(board="alpha").resolve()
    beta = kb.kanban_db_path(board="beta").resolve()
    for _ in range(2):
        _board(client, "alpha")
        _board(client, "beta")
    assert sorted(map(str, probed)) == sorted(map(str, [alpha, beta]))
