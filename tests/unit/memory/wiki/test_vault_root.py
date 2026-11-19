"""Vault-root resolution must not depend on the process CWD (spec A7)."""
from jarvis.core.paths import repo_root
from jarvis.memory.wiki.vault_root import resolve_vault_root


def test_installed_runtime_keeps_relative_state_outside_the_program(
    monkeypatch, tmp_path,
):
    from jarvis.core.config import ensure_project_root_cwd
    from jarvis.memory.wiki.db_path import resolve_wiki_db_path

    monkeypatch.chdir(tmp_path)
    home = tmp_path / "Jarvis"
    home.mkdir()
    monkeypatch.setenv("JARVIS_RUNTIME_HOME", str(home))

    assert ensure_project_root_cwd() == home
    assert resolve_wiki_db_path("data") == home / "data" / "jarvis.db"
    vault = resolve_vault_root("wiki/obsidian-vault")
    assert vault.path == home / "wiki" / "obsidian-vault"
    assert vault.source == "runtime_home"


def test_relative_root_anchors_to_repo_root_not_cwd(tmp_path):
    res = resolve_vault_root("wiki/obsidian-vault", cwd=tmp_path)
    assert res.path == (repo_root() / "wiki" / "obsidian-vault").resolve()
    assert res.source == "repo_root"


def test_absolute_root_passes_through(tmp_path):
    res = resolve_vault_root(tmp_path / "vault", cwd=tmp_path)
    assert res.path == (tmp_path / "vault").resolve()
    assert res.source == "absolute"
    assert res.legacy_conflict is False


def test_populated_legacy_cwd_vault_wins_and_flags_conflict(tmp_path):
    """Hermetic: anchored vault EMPTY + legacy CWD vault POPULATED → legacy wins."""
    anchor = tmp_path / "repo"
    (anchor / "wiki" / "obsidian-vault").mkdir(parents=True)  # exists but empty
    old_cwd = tmp_path / "elsewhere"
    legacy = old_cwd / "wiki" / "obsidian-vault"
    legacy.mkdir(parents=True)
    (legacy / "log.md").write_text("# log\n", encoding="utf-8")

    res = resolve_vault_root("wiki/obsidian-vault", cwd=old_cwd, anchor=anchor)

    assert res.path == legacy.resolve()
    assert res.source == "legacy_cwd"
    assert res.legacy_conflict is True


def test_populated_anchored_vault_wins_over_populated_legacy(tmp_path):
    """Hermetic: BOTH populated → the anchor wins, conflict still flagged."""
    anchor = tmp_path / "repo"
    anchored = anchor / "wiki" / "obsidian-vault"
    anchored.mkdir(parents=True)
    (anchored / "log.md").write_text("# anchored log\n", encoding="utf-8")
    old_cwd = tmp_path / "elsewhere"
    legacy = old_cwd / "wiki" / "obsidian-vault"
    legacy.mkdir(parents=True)
    (legacy / "log.md").write_text("# legacy log\n", encoding="utf-8")

    res = resolve_vault_root("wiki/obsidian-vault", cwd=old_cwd, anchor=anchor)

    assert res.path == anchored.resolve()
    assert res.source == "repo_root"
    assert res.legacy_conflict is True


def test_none_falls_back_to_default_relative_root(tmp_path):
    res = resolve_vault_root(None, cwd=tmp_path)
    assert res.path.name == "obsidian-vault"
    assert res.path.is_absolute()
