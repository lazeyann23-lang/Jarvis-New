"""Local, evidence-based memory of completed mission planning patterns.

Only APPROVED missions are eligible. The memory stores a bounded structural
plan (step slugs and repository requirements), not tool output, full prompts,
model reasoning, credentials, or generated worker instructions. Its keyword
lookup uses SHA-256 term digests rather than a second cleartext prompt index.

This is advisory context for the existing decomposer, never an authorization
mechanism, executable skill, or a replacement for fresh verification.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


from .events import EventEnvelope, MissionApproved, MissionPlanReady
from .state_machine import MissionState

if TYPE_CHECKING:
    from .manager import MissionManager

logger = logging.getLogger(__name__)

# Do not retain common filler and numerical identifiers in the term index.
_STOP = frozenset(
    "the and for are with from this that into please then them your you can make "
    "need want task have build create write using will would should after before "
    "und der die das den dem des eine einen ein einer einem ich du mir mich "
    "bitte auch noch soll sollen wird werden kann können möchte machen erstelle "
    "für mit von zum zur im in auf aus bei wie was wenn dann über ohne "
    "and or but est une que les dans pour avec por para con una uno los las".split()
)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TERM = re.compile(r"[^\W_][\w-]{2,}", re.UNICODE)
_MAX_TERMS = 48
_MAX_STEPS = 5


def _term_digests(text: str) -> tuple[str, ...]:
    """Sorted deterministic digests of useful Unicode search terms."""
    found: set[str] = set()
    for match in _TERM.finditer(text.casefold()[:6000]):
        token = match.group().strip("-")
        if (len(token) < 3 or len(token) > 40 or token in _STOP
                or any(char.isdigit() for char in token)):
            continue
        found.add(hashlib.sha256(token.encode("utf-8")).hexdigest())
        if len(found) >= _MAX_TERMS:
            break
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class SuccessfulPattern:
    mission_id: str
    steps: tuple[str, ...]
    n_workers: int
    needs_repo: tuple[bool, ...]
    matched_terms: int
    similarity: float


class MissionExperienceMemory:
    """Append-only (by mission ID), user-clearable local experience index.

    Connections are short-lived: no global connection, no startup migration and
    no background model calls. An approved mission is indexed at most once.
    """

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=3)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mission_experience (
                  mission_id TEXT PRIMARY KEY,
                  step_slugs TEXT NOT NULL,
                  needs_repo TEXT NOT NULL,
                  n_workers INTEGER NOT NULL,
                  term_count INTEGER NOT NULL,
                  approved_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mission_experience_terms (
                  digest TEXT NOT NULL,
                  mission_id TEXT NOT NULL,
                  PRIMARY KEY (digest, mission_id),
                  FOREIGN KEY (mission_id) REFERENCES mission_experience(mission_id)
                    ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_mission_experience_recency
                  ON mission_experience(approved_at_ms DESC);
                """
            )
            return conn
        except BaseException:
            conn.close()
            raise

    async def record_approved(
        self,
        *,
        mission_id: str,
        prompt: str,
        plan: list[dict[str, object]],
        n_workers: int,
        approved_at_ms: int,
    ) -> bool:
        """Record a vetted successful plan; return False for repeats/invalid data.

        Caller MUST prove the mission is APPROVED in the authoritative store.
        This method accepts only the structural subset required for future
        planning; all worker instructions and outputs are deliberately dropped.
        """
        # Reject malformed external or historical event data gracefully.
        # A malformed plan must not interrupt mission approval or the bus.
        if not isinstance(prompt, str) or not isinstance(plan, list):
            return False
        terms = _term_digests(prompt)
        if len(terms) < 2 or not 1 <= len(plan) <= _MAX_STEPS:
            return False
        if (type(n_workers) is not int or not 1 <= n_workers <= _MAX_STEPS
                or n_workers > len(plan)):
            return False
        slugs: list[str] = []
        repo: list[bool] = []
        for step in plan:
            if not isinstance(step, dict):
                return False
            slug = step.get("slug")
            needs_repo = step.get("needs_repo", True)
            if (not isinstance(slug, str) or _SLUG.fullmatch(slug) is None
                    or not isinstance(needs_repo, bool)):
                return False
            slugs.append(slug)
            repo.append(needs_repo)
        if (not isinstance(mission_id, str) or not mission_id
                or len(mission_id) > 128
                or type(approved_at_ms) is not int or approved_at_ms < 0):
            return False
        return await asyncio.to_thread(
            self._record_sync, mission_id, slugs, repo, n_workers, terms, approved_at_ms
        )

    def _record_sync(
        self, mission_id: str, slugs: list[str], repo: list[bool],
        n_workers: int, terms: tuple[str, ...], approved_at_ms: int,
    ) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO mission_experience
                    (mission_id, step_slugs, needs_repo, n_workers,
                     term_count, approved_at_ms)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (mission_id, json.dumps(slugs), json.dumps(repo),
                     n_workers, len(terms), approved_at_ms),
                )
                inserted = cursor.rowcount > 0
                cursor.close()
                if inserted:
                    conn.executemany(
                        "INSERT INTO mission_experience_terms(digest, mission_id) VALUES (?, ?)",
                        [(term, mission_id) for term in terms],
                    )
            return inserted
        finally:
            conn.close()

    async def relevant(self, prompt: str, *, limit: int = 3) -> list[SuccessfulPattern]:
        """Rank relevant successful patterns by overlap and specificity."""
        terms = _term_digests(prompt)
        if len(terms) < 2 or limit < 1:
            return []
        return await asyncio.to_thread(self._relevant_sync, terms, min(limit, 3))

    def _relevant_sync(
        self, terms: tuple[str, ...], limit: int,
    ) -> list[SuccessfulPattern]:
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in terms)
            rows = conn.execute(
                f"""SELECT e.mission_id, e.step_slugs, e.needs_repo,
                    e.n_workers, e.term_count, COUNT(*) AS overlap
                FROM mission_experience_terms t
                JOIN mission_experience e ON e.mission_id=t.mission_id
                WHERE t.digest IN ({placeholders})
                GROUP BY e.mission_id
                HAVING overlap >= 2
                ORDER BY overlap DESC, e.approved_at_ms DESC
                LIMIT 24""", terms,
            ).fetchall()
        finally:
            conn.close()
        matches = []
        for mission_id, slugs, repo, workers, saved_count, overlap in rows:
            # Dice coefficient penalizes incidental overlap between unrelated
            # requests with very different sets of task-specific keywords.
            similarity = 2 * overlap / (len(terms) + saved_count)
            if similarity < 0.34:
                continue
            try:
                matches.append(SuccessfulPattern(
                    mission_id=mission_id,
                    steps=tuple(json.loads(slugs)),
                    needs_repo=tuple(json.loads(repo)),
                    n_workers=workers,
                    matched_terms=overlap,
                    similarity=similarity,
                ))
            except (ValueError, TypeError):
                logger.warning("Invalid experience pattern ignored: %s", mission_id)
        return sorted(matches, key=lambda m: (-m.similarity, -m.matched_terms))[:limit]

    async def advice(self, prompt: str) -> str:
        """Small, purely structural past-success hint for the planning LLM."""
        patterns = await self.relevant(prompt)
        if not patterns:
            return ""
        lines = []
        for p in patterns:
            steps = ", ".join(p.steps)
            repo = "source checkout" if any(p.needs_repo) else "no source checkout"
            lines.append(f"- {p.n_workers} workers; steps: {steps}; {repo}.")
        return "\n".join(lines)[:650]

    async def stats(self) -> int:
        """Number of distinct reviewed successful missions in this memory."""
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM mission_experience").fetchone()[0])
        finally:
            conn.close()

    async def forget(self, mission_id: str) -> bool:
        """Forget one pattern without touching the original mission history."""
        if not mission_id:
            return False
        return await asyncio.to_thread(self._forget_sync, mission_id)

    def _forget_sync(self, mission_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM mission_experience WHERE mission_id = ?", (mission_id,)
                )
                deleted = cursor.rowcount > 0
                cursor.close()
            return deleted
        finally:
            conn.close()

    async def clear(self) -> None:
        """Delete ONLY learned patterns, not mission history or notebooks."""
        await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM mission_experience_terms")
                conn.execute("DELETE FROM mission_experience")
        finally:
            conn.close()


class MissionExperienceCollector:
    """Non-fatal subscriber storing *only* approved mission plans."""

    def __init__(self, manager: MissionManager, memory: MissionExperienceMemory) -> None:
        self._manager = manager
        self._memory = memory
        self._unsubscribe = None

    def start(self) -> None:
        self._unsubscribe = self._manager.bus.subscribe_all(self._on_event)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def _on_event(self, env: EventEnvelope) -> None:
        if not isinstance(env.payload, MissionApproved):
            return
        try:
            # Do not rely solely on event payloads; a mission must really have
            # reached the approved state in the event store.
            view = await self._manager.store.get_mission_view(env.mission_id)
            if view is None or view[1] != MissionState.APPROVED.value:
                return
            events = await self._manager.store.events_for_mission(env.mission_id)
            plan_event = next(
                (e.payload for e in reversed(events)
                 if isinstance(e.payload, MissionPlanReady)),
                None,
            )
            if plan_event is None:
                return
            recorded = await self._memory.record_approved(
                mission_id=env.mission_id,
                prompt=view[0],
                plan=plan_event.plan,
                n_workers=plan_event.n_workers,
                approved_at_ms=env.ts_ms,
            )
            if recorded:
                logger.info("Approved mission plan learned: %s", env.mission_id)
        except Exception:  # noqa: BLE001 — memory must never break missions
            logger.warning("Mission experience recording failed", exc_info=True)


__all__ = ["MissionExperienceMemory", "MissionExperienceCollector", "SuccessfulPattern"]
