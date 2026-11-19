"""End-to-end wiki write-boundary checks for new credential shapes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jarvis.memory.wiki.atomic_writer import AtomicWriter
from jarvis.memory.wiki.protocols import PageUpdate


class _AcceptingRepo:
    async def parse(self, raw, path):
        return SimpleNamespace(is_schema_valid=True)


def test_provider_credential_never_reaches_disk(tmp_path, caplog):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = AtomicWriter(vault, tmp_path / "backups")
    target = vault / "secret.md"
    credential = "github_pat_" + "x" * 45
    result = asyncio.run(writer.apply(
        [PageUpdate(target, "create", "Owner token: " + credential)],
        repo=_AcceptingRepo(),
    ))
    assert result.blocked_pii == [target]
    assert result.applied == []
    assert not target.exists()
    assert not (tmp_path / "backups").exists()
    assert credential not in caplog.text


def test_safe_wiki_page_still_writes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    writer = AtomicWriter(vault, tmp_path / "backups")
    target = vault / "safe.md"
    body = "# JARVIS\nThe user prefers short, clear explanations."
    result = asyncio.run(writer.apply(
        [PageUpdate(target, "create", body)],
        repo=_AcceptingRepo(),
    ))
    assert result.applied == [target]
    assert result.blocked_pii == []
    assert target.read_text(encoding="utf-8") == body
