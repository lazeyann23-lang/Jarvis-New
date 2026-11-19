"""Explicit, scoped removal of ONE user-created Wiki knowledge page.

This is not a general-purpose 'forget me' operation. It removes an exact
vault-relative page, matching candidate-journal records, search index rows,
and exact-path copies in the locally managed tar backups. It deliberately
never claims to erase chat transcripts, unrelated notes, OS snapshots, or
copies under other names. Callers must obtain a preview and verify it again
immediately before invoking ``forget_page`` under the curator writer lock.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MAX_PAGE_BYTES = 2_000_000


class ForgetError(RuntimeError):
    """The removal cannot be completed safely; do not report success."""


_DB_TABLES = (
    "wiki_fts", "wiki_semantic_chunks", "wiki_semantic_pages",
    "wiki_candidate_journal", "wiki_index_meta",
)


def _safe_page(root: Path, rel: str) -> Path:
    candidate = PurePosixPath(rel)
    if (not rel or "\\" in rel or candidate.is_absolute()
            or any(p in ("", ".", "..") for p in candidate.parts)
            or candidate.as_posix() != rel or candidate.suffix != ".md"
            or candidate.parts[0] not in ("entities", "projects", "concepts")):
        raise ForgetError("Only a vault-relative knowledge page may be removed.")
    root = root.resolve(strict=True)
    target = root / rel
    if target.is_symlink() or not target.is_file() or target.resolve() != target.absolute():
        raise ForgetError("The page is missing, linked, or not a regular file.")
    if not target.resolve().is_relative_to(root):
        raise ForgetError("The page escapes the vault.")
    if target.stat().st_size > MAX_PAGE_BYTES:
        raise ForgetError("The page is too large for safe interactive deletion.")
    return target


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def _db_counts(db_path: Path, rel: str) -> tuple[dict[str, int], tuple[int, ...]]:
    counts = {name: 0 for name in _DB_TABLES}
    if not db_path.is_file():
        return counts, ()
    try:
        with sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=3) as conn:
            present = _tables(conn)
            for name in _DB_TABLES:
                if name in present:
                    counts[name] = int(conn.execute(
                        f"SELECT COUNT(*) FROM {name} WHERE {'target_path' if name == 'wiki_candidate_journal' else 'path'} = ?",
                        (rel,),
                    ).fetchone()[0])
            ids = tuple(int(row[0]) for row in conn.execute(
                "SELECT id FROM wiki_candidate_journal WHERE target_path = ? ORDER BY id", (rel,)
            )) if "wiki_candidate_journal" in present else ()
            return counts, ids
    except sqlite3.Error as exc:
        raise ForgetError("The memory database could not be inspected.") from exc


def _backup_matches(backup_dir: Path, rel: str) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    """Scan managed tar archives; a damaged archive is a hard stop."""
    if not backup_dir.exists():
        return ()
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ForgetError("The backup directory is not safe to inspect.")
    found = []
    for archive in sorted(backup_dir.glob("*.tar.gz")):
        if archive.is_symlink() or not archive.is_file():
            raise ForgetError("A backup is a symbolic link or not a regular file.")
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = tuple(m.name for m in tar if m.isfile() and m.name == rel)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ForgetError("A managed backup is unreadable; no deletion was performed.") from exc
        if members:
            found.append((archive, members))
    return tuple(found)


@dataclass(frozen=True)
class ForgetPreview:
    path: str
    page_sha256: str
    fingerprint: str
    index_rows: dict[str, int]
    journal_ids: tuple[int, ...]
    backups: tuple[tuple[Path, tuple[str, ...]], ...]

    def summary(self) -> dict:
        return {
            "path": self.path,
            "index_rows": {k: v for k, v in self.index_rows.items() if k != "wiki_candidate_journal"},
            "journal_candidates": len(self.journal_ids),
            "managed_backup_archives": [p.name for p, _ in self.backups],
            "limitations": [
                "Only this page and exact-path copies in locally managed tar backups are removed.",
                "Other pages, chat history, pending journal candidates, exports, external backups and OS snapshots are NOT erased.",
                "SQLite/WAL/FTS storage may retain recoverable remnants until database maintenance; secure physical erasure is not guaranteed.",
                "New conversations or other sources may recreate a deleted fact.",
            ],
        }


def preview_forget(vault_root: Path, backup_dir: Path, db_path: Path, rel: str) -> ForgetPreview:
    target = _safe_page(vault_root, rel)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    counts, ids = _db_counts(db_path, rel)
    backups = _backup_matches(backup_dir, rel)
    # Changes to the page, journal, index, or any matching backup invalidate a
    # previous preview. Fingerprint never exposes the page contents.
    state = repr((rel, digest, counts, ids, tuple(
        (str(p), p.stat().st_size, p.stat().st_mtime_ns, members)
        for p, members in backups
    )))
    fingerprint = hashlib.sha256(state.encode("utf-8")).hexdigest()
    return ForgetPreview(rel, digest, fingerprint, counts, ids, backups)


def _rewrite_archive(archive: Path, rel: str) -> Path:
    """Build a clean replacement without touching the original until ready."""
    fd, temp = tempfile.mkstemp(prefix=".jarvis-forget-", suffix=".tar.gz", dir=archive.parent)
    os.close(fd)
    staged = Path(temp)
    try:
        with tarfile.open(archive, "r:gz") as source, tarfile.open(staged, "w:gz") as dest:
            for member in source:
                if member.name == rel:
                    continue
                if member.isfile():
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ForgetError("Backup member could not be read.")
                    with stream:
                        dest.addfile(member, stream)
                else:
                    dest.addfile(member)
        os.chmod(staged, archive.stat().st_mode & 0o777)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def forget_page(vault_root: Path, backup_dir: Path, db_path: Path,
                expected: ForgetPreview) -> dict:
    """Remove a previewed page; raise if anything changed before confirmation.

    The caller serialises with the Wiki curator writer lock. No new snapshot is
    created, because doing so would itself reintroduce the deleted content.
    """
    current = preview_forget(vault_root, backup_dir, db_path, expected.path)
    if current.fingerprint != expected.fingerprint:
        raise ForgetError("The page or its dependencies changed; preview again.")
    target = _safe_page(vault_root, expected.path)
    original = target.read_bytes()
    if hashlib.sha256(original).hexdigest() != expected.page_sha256:
        raise ForgetError("The page changed; preview again.")
    staged: list[tuple[Path, Path]] = []
    conn: sqlite3.Connection | None = None
    deleted_page = False
    replaced_backups = 0
    try:
        # Stage every backup rewrite first: a corrupt/unwritable archive must
        # not cause the live page to be removed.
        backup_fingerprints = {}
        for archive, _members in current.backups:
            stat = archive.stat()
            backup_fingerprints[archive] = (stat.st_size, stat.st_mtime_ns)
            staged.append((archive, _rewrite_archive(archive, current.path)))
            new_stat = archive.stat()
            if (new_stat.st_size, new_stat.st_mtime_ns) != backup_fingerprints[archive]:
                raise ForgetError("A backup changed during preparation.")
        if db_path.exists():
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("BEGIN IMMEDIATE")
            tables = _tables(conn)
            actual_ids = tuple(int(r[0]) for r in conn.execute(
                "SELECT id FROM wiki_candidate_journal WHERE target_path=? ORDER BY id", (current.path,)
            )) if "wiki_candidate_journal" in tables else ()
            if actual_ids != current.journal_ids:
                raise ForgetError("Journal entries changed; preview again.")
            for table in ("wiki_fts", "wiki_semantic_chunks", "wiki_semantic_pages"):
                if table in tables:
                    conn.execute(f"DELETE FROM {table} WHERE path=?", (current.path,))
            if "wiki_candidate_journal" in tables:
                conn.execute("DELETE FROM wiki_candidate_journal WHERE target_path=?", (current.path,))
            if "wiki_index_meta" in tables:
                conn.execute("UPDATE wiki_index_meta SET path=NULL WHERE path=?", (current.path,))
        # Page first, then backups, then commit DB. If a later step fails,
        # restore the page (the sanitized backups remain privacy-safe).
        if target.read_bytes() != original:
            raise ForgetError("The page changed while backups were being prepared.")
        for archive, _staged_path in staged:
            stat = archive.stat()
            if (stat.st_size, stat.st_mtime_ns) != backup_fingerprints[archive]:
                raise ForgetError("A backup changed while preparing deletion.")
        target.unlink()
        deleted_page = True
        for archive, staged_path in staged:
            os.replace(staged_path, archive)
            replaced_backups += 1
        if conn is not None:
            conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        if deleted_page and not target.exists():
            # Restore the original if the operation was interrupted. The
            # partial backup cleanup may be retained, never reintroducing data.
            fd, restore_name = tempfile.mkstemp(prefix=".jarvis-restore-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(original)
                os.replace(restore_name, target)
            finally:
                Path(restore_name).unlink(missing_ok=True)
        raise ForgetError("Removal was interrupted; check the vault and backups before retrying.") from exc
    finally:
        for _archive, staged_path in staged:
            staged_path.unlink(missing_ok=True)
        if conn is not None:
            conn.close()
    return {
        "ok": True,
        "path": current.path,
        "page_removed": not target.exists(),
        "backup_archives_cleaned": replaced_backups,
        "journal_candidates_removed": len(current.journal_ids),
        "index_rows_removed": current.index_rows,
        "remaining_copies_possible": True,
        "limitations": current.summary()["limitations"],
    }
