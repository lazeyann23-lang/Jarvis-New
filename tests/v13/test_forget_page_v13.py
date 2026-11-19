"""V13: one-page preview/confirmation, journal/index/backup cleanup, fail-closed guards."""
import asyncio
import sys
import types
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# Test-only import shim: the production code does not use aiosqlite here.
# The full project backend requires it, but it is absent in this runner.
if "aiosqlite" not in sys.modules:
    shim = types.ModuleType("aiosqlite")
    for attr in ("Connection", "Cursor", "Row"):
        setattr(shim, attr, type(attr, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.memory.wiki.forget_page import ForgetError, forget_page, preview_forget


def setup(tmp_path: Path):
    root = tmp_path / "vault"
    (root / "entities").mkdir(parents=True)
    (root / "concepts").mkdir()
    target = root / "entities" / "private.md"
    target.write_text("# Personal fact\nSecret example value\n", encoding="utf-8")
    other = root / "concepts" / "keep.md"
    other.write_text("# Keep this\n", encoding="utf-8")
    backups = tmp_path / "wiki-backups"
    backups.mkdir()
    archive = backups / "wiki-20261023.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in (target, other):
            tar.add(path, arcname=path.relative_to(root).as_posix())
    db = tmp_path / "jarvis.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE VIRTUAL TABLE wiki_fts USING fts5(path UNINDEXED, title, body);
        CREATE TABLE wiki_semantic_pages (path TEXT PRIMARY KEY, mtime TEXT, model TEXT);
        CREATE TABLE wiki_semantic_chunks (path TEXT, chunk INTEGER, text TEXT, PRIMARY KEY(path,chunk));
        CREATE TABLE wiki_candidate_journal (id INTEGER PRIMARY KEY, fact TEXT, target_path TEXT);
        CREATE TABLE wiki_candidate_evidence (candidate_id INTEGER PRIMARY KEY REFERENCES wiki_candidate_journal(id) ON DELETE CASCADE, evidence_turn_id TEXT);
        CREATE TABLE wiki_index_meta (singleton INTEGER PRIMARY KEY, path TEXT);
        INSERT INTO wiki_fts (path,title,body) VALUES ('entities/private.md','private','Secret example value');
        INSERT INTO wiki_fts (path,title,body) VALUES ('concepts/keep.md','keep','Safe');
        INSERT INTO wiki_semantic_pages VALUES ('entities/private.md','1','model');
        INSERT INTO wiki_semantic_pages VALUES ('concepts/keep.md','1','model');
        INSERT INTO wiki_semantic_chunks VALUES ('entities/private.md',0,'Secret example value');
        INSERT INTO wiki_semantic_chunks VALUES ('concepts/keep.md',0,'Safe');
        INSERT INTO wiki_candidate_journal VALUES (1,'Secret example value','entities/private.md');
        INSERT INTO wiki_candidate_journal VALUES (2,'Other fact','concepts/keep.md');
        INSERT INTO wiki_candidate_evidence VALUES (1,'turn-1');
        INSERT INTO wiki_index_meta VALUES (1,'entities/private.md');
        """)
    return root, backups, db, target, other, archive


# Group 1: functionality

def test_preview_is_non_destructive_and_informs_limitations(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    preview = preview_forget(root, backups, db, "entities/private.md")
    assert target.is_file() and other.is_file()
    assert preview.index_rows["wiki_fts"] == 1
    assert preview.index_rows["wiki_semantic_chunks"] == 1
    assert preview.journal_ids == (1,)
    assert len(preview.backups) == 1
    assert "NOT erased" in " ".join(preview.summary()["limitations"])


def test_preview_fingerprint_changes_when_page_is_edited(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    first = preview_forget(root, backups, db, "entities/private.md")
    target.write_text("New text", encoding="utf-8")
    assert preview_forget(root, backups, db, "entities/private.md").fingerprint != first.fingerprint
    with pytest.raises(ForgetError, match="changed"):
        forget_page(root, backups, db, first)
    assert target.read_text() == "New text"


# Group 2: integration (real SQLite FTS5 and tar archives)

def test_confirm_removes_exact_page_and_derived_data(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    preview = preview_forget(root, backups, db, "entities/private.md")
    result = forget_page(root, backups, db, preview)
    assert result["ok"] and result["remaining_copies_possible"]
    assert result["backup_archives_cleaned"] == 1
    assert not target.exists() and other.is_file()
    with tarfile.open(archive, "r:gz") as tar:
        assert tar.getnames() == ["concepts/keep.md"]
    with sqlite3.connect(db) as conn:
        for table in ("wiki_fts", "wiki_semantic_pages", "wiki_semantic_chunks", "wiki_candidate_journal"):
            col = "target_path" if table == "wiki_candidate_journal" else "path"
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='entities/private.md'").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='concepts/keep.md'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM wiki_candidate_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT path FROM wiki_index_meta").fetchone()[0] is None


def test_no_database_and_no_backups_are_supported(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    archive.unlink()
    db.unlink()
    preview = preview_forget(root, backups, db, "entities/private.md")
    result = forget_page(root, backups, db, preview)
    assert result["page_removed"] and result["backup_archives_cleaned"] == 0


# Group 3: security/regression

@pytest.mark.parametrize("path", ["../outside.md", "/etc/passwd", "entities/../concepts/keep.md", "entities//private.md", "entities/private.md/extra", "_archive/entities/private.md", "entities\\private.md"])
def test_path_traversal_and_non_knowledge_paths_refused(tmp_path, path):
    root, backups, db, target, other, archive = setup(tmp_path)
    with pytest.raises(ForgetError):
        preview_forget(root, backups, db, path)
    assert target.exists()


def test_symlink_refused(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    target.unlink()
    target.symlink_to(other)
    with pytest.raises(ForgetError):
        preview_forget(root, backups, db, "entities/private.md")
    assert other.exists()


def test_corrupt_backup_fails_closed(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    archive.write_bytes(b"not a tar archive")
    with pytest.raises(ForgetError, match="unreadable"):
        preview_forget(root, backups, db, "entities/private.md")
    assert target.exists()


def test_changed_journal_invalidates_preview(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    preview = preview_forget(root, backups, db, "entities/private.md")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO wiki_candidate_journal VALUES (3,'another','entities/private.md')")
    with pytest.raises(ForgetError, match="changed"):
        forget_page(root, backups, db, preview)
    assert target.exists()


def test_unrelated_backup_members_preserved(tmp_path):
    root, backups, db, target, other, archive = setup(tmp_path)
    original = other.read_bytes()
    forget_page(root, backups, db, preview_forget(root, backups, db, "entities/private.md"))
    with tarfile.open(archive, "r:gz") as tar:
        assert tar.extractfile("concepts/keep.md").read() == original

# HTTP integration: use the real FastAPI routes with a small fake roster/model.
# No production backend services or external LLM are started.
def test_http_preview_confirmation_is_one_time_and_session_bound(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jarvis.ui.web import memory_vault_routes as routes

    root, backups, db, target, other, archive = setup(tmp_path)
    model = SimpleNamespace(pages={"entities/private.md": SimpleNamespace(
        path="entities/private.md", group="knowledge"
    )})

    async def fake_model(_request):
        return root, model

    writer = SimpleNamespace(backup_manager=SimpleNamespace(backup_dir=backups),
                             _serial_lock=asyncio.Lock())
    monkeypatch.setattr(routes, "_model", fake_model)
    monkeypatch.setattr(routes, "_require_curator", lambda: SimpleNamespace(_writer=writer))
    app = FastAPI()
    app.state.config = SimpleNamespace(memory=SimpleNamespace(data_dir=tmp_path))
    app.include_router(routes.router)
    with TestClient(app) as client:
        client.cookies.set("jarvis_session", "user-A")
        res = client.post("/api/wiki/pages/forget/preview", json={"path": "entities/private.md"})
        assert res.status_code == 200, res.text
        assert res.headers["cache-control"] == "no-store"
        data = res.json()
        assert data["confirmation_required"] == "DELETE entities/private.md"
        assert target.exists()
        payload = {"path": "entities/private.md", "preview_token": data["preview_token"],
                   "confirmation": "DELETE entities/private.md"}
        assert client.post("/api/wiki/pages/forget", json={**payload, "confirmation": "yes"}).status_code == 422
        client.cookies.set("jarvis_session", "user-B")
        assert client.post("/api/wiki/pages/forget", json=payload).status_code == 409
        assert target.exists()
        client.cookies.set("jarvis_session", "user-A")
        new = client.post("/api/wiki/pages/forget/preview", json={"path": "entities/private.md"}).json()
        payload["preview_token"] = new["preview_token"]
        res = client.post("/api/wiki/pages/forget", json=payload)
        assert res.status_code == 200, res.text
        assert res.json()["page_removed"]
        assert client.post("/api/wiki/pages/forget", json=payload).status_code in (404, 409)


def test_backup_rewrite_failure_does_not_remove_page(tmp_path, monkeypatch):
    from jarvis.memory.wiki import forget_page as module
    root, backups, db, target, other, archive = setup(tmp_path)
    preview = preview_forget(root, backups, db, "entities/private.md")

    def fail(_archive, _rel):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_rewrite_archive", fail)
    with pytest.raises(ForgetError, match="interrupted"):
        forget_page(root, backups, db, preview)
    assert target.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wiki_candidate_journal WHERE id=1").fetchone()[0] == 1


def test_oversized_page_refused_before_preview(tmp_path):
    from jarvis.memory.wiki.forget_page import MAX_PAGE_BYTES
    root, backups, db, target, other, archive = setup(tmp_path)
    target.write_bytes(b"x" * (MAX_PAGE_BYTES + 1))
    with pytest.raises(ForgetError, match="too large"):
        preview_forget(root, backups, db, "entities/private.md")
