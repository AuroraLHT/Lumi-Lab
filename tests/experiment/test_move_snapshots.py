"""scripts/move_snapshots.py: moves the stills without losing or overwriting any."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "move_snapshots.py"


@pytest.fixture
def move_snapshots():
    spec = importlib.util.spec_from_file_location("move_snapshots", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path, db: Path, uuid: str, raw: bytes = b"frame") -> None:
    (root / uuid).mkdir(parents=True)
    (root / uuid / "start.npy").write_bytes(raw)
    (root / uuid / "start.jpg").write_bytes(b"jpeg")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS snapshot (snapshot_uuid, raw_path, jpeg_path, raw_sha256)")
        conn.execute("INSERT INTO snapshot VALUES (?, ?, ?, ?)",
                     (uuid, f"{uuid}/start.npy", f"{uuid}/start.jpg", hashlib.sha256(raw).hexdigest()))


def _run(module, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["move_snapshots.py", "--force", *args])
    return module.main()


def test_moves_every_snapshot_and_verifies_it(move_snapshots, monkeypatch, tmp_path):
    src, dst, db = tmp_path / "cfg" / "snapshots", tmp_path / "database" / "snapshots", tmp_path / "growth.db"
    _snapshot(src, db, "a1")
    _snapshot(src, db, "b2")

    assert _run(move_snapshots, monkeypatch, "--from", str(src), "--to", str(dst), "--db", str(db)) == 0
    assert not src.exists()
    assert sorted(p.name for p in dst.iterdir()) == ["a1", "b2"]
    assert (dst / "a1" / "start.npy").read_bytes() == b"frame"


def test_dry_run_moves_nothing(move_snapshots, monkeypatch, tmp_path):
    src, dst, db = tmp_path / "old", tmp_path / "new", tmp_path / "growth.db"
    _snapshot(src, db, "a1")
    assert _run(move_snapshots, monkeypatch, "--from", str(src), "--to", str(dst), "--db", str(db), "--dry-run") == 0
    assert (src / "a1" / "start.npy").exists() and not dst.exists()


def test_refuses_to_overwrite_a_snapshot_already_there(move_snapshots, monkeypatch, tmp_path):
    src, dst, db = tmp_path / "old", tmp_path / "new", tmp_path / "growth.db"
    _snapshot(src, db, "a1", raw=b"old")
    (dst / "a1").mkdir(parents=True)
    (dst / "a1" / "start.npy").write_bytes(b"other")

    assert _run(move_snapshots, monkeypatch, "--from", str(src), "--to", str(dst), "--db", str(db)) == 1
    assert (src / "a1" / "start.npy").read_bytes() == b"old"
    assert (dst / "a1" / "start.npy").read_bytes() == b"other"


def test_verify_reports_missing_and_changed_files(move_snapshots, tmp_path):
    db = tmp_path / "growth.db"
    _snapshot(tmp_path / "root", db, "a1")
    _snapshot(tmp_path / "root", db, "b2")
    (tmp_path / "root" / "a1" / "start.jpg").unlink()
    (tmp_path / "root" / "b2" / "start.npy").write_bytes(b"tampered")

    problems = move_snapshots.verify(tmp_path / "root", move_snapshots._rows(db))
    assert len(problems) == 2
    assert any("missing" in p and "a1" in p for p in problems)
    assert any("SHA-256" in p and "b2" in p for p in problems)
