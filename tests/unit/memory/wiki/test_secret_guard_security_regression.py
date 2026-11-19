"""Secret guard: avoid logging values and preserve ordinary wiki content."""
from __future__ import annotations

import time

from jarvis.memory.wiki.secret_guard import contains_secret, find_secrets


def test_diagnostics_only_return_pattern_names():
    credential = "glpat-" + "X" * 30
    hits = find_secrets("The secret is " + credential)
    assert hits == ["provider_key"]
    assert credential not in repr(hits)


def test_benign_notes_and_commit_hashes_still_pass():
    notes = (
        "This note explains a GitHub fine-grained token format. "
        "The commit hash is " + "f" * 40 + ". "
    ) * 200
    assert not contains_secret(notes)
    assert find_secrets(notes) == []


def test_long_nonsecret_note_does_not_hang():
    text = "JARVIS remembers tasks and explains network concepts. " * 5000
    start = time.perf_counter()
    assert not contains_secret(text)
    assert time.perf_counter() - start < 5.0
