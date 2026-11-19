"""SQLite WAL event store for mission events.

Reuses the Phase-5 pattern from `jarvis/tasks/store.py:48-60`:
aiosqlite with `isolation_level=None`, `PRAGMA journal_mode=WAL`,
`synchronous=NORMAL`, `busy_timeout=5000`, `executescript()` migrations.

Dedicated DB file `data/missions.db` (see ADR-0009 §"SQLite strategy",
subagent report 3 recommendation) — separate lifecycle from `data/jarvis.db`.

Persist-before-publish atomicity (ADR-0009 Decision §4 + Risk #9/#10):
`append_and_publish()` first INSERTs with RETURNING seq, then publishes to the bus.
Crash in between = INSERT is persisted; recovery finds the event on startup.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from .event_bus import MissionBus
from .events import EventEnvelope
from .ownership import current_process_identity
from .state_machine import MissionState

log = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "missions_schema.sql"


class MissionEventStore:
    """SQLite WAL event store with append-and-publish atomicity."""

    def __init__(self, db_path: Path, bus: MissionBus) -> None:
        self._db_path = db_path
        self._bus = bus
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open the DB, set PRAGMAs, load schema (idempotent) + run migrations."""
        if self._conn is not None:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(
            self._db_path,
            isolation_level=None,  # autocommit — WAL is the lock manager
        )
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.executescript(schema_sql)
        self._conn = conn
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        """Idempotent column migrations for existing databases.

        SQLite has no `ADD COLUMN IF NOT EXISTS` — we read pragma_table_info
        and append only missing columns. Phase-3 columns (`iteration`,
        `cost_usd`) are already present in the CREATE statement; this is
        the upgrade path for pre-Phase-3 DBs from blocks 1/2.
        """
        # Read existing column names.
        cur = await self.conn.execute("PRAGMA table_info(missions)")
        rows = await cur.fetchall()
        await cur.close()
        existing_cols = {str(row[1]) for row in rows}

        if "iteration" not in existing_cols:
            await self.conn.execute(
                "ALTER TABLE missions ADD COLUMN iteration INTEGER NOT NULL DEFAULT 0"
            )
            log.info("MissionEventStore: migration applied — added 'iteration'")
        if "cost_usd" not in existing_cols:
            await self.conn.execute(
                "ALTER TABLE missions ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0.0"
            )
            log.info("MissionEventStore: migration applied — added 'cost_usd'")
        if "last_heartbeat_ms" not in existing_cols:
            await self.conn.execute(
                "ALTER TABLE missions ADD COLUMN last_heartbeat_ms INTEGER NOT NULL DEFAULT 0"
            )
            log.info("MissionEventStore: migration applied — added 'last_heartbeat_ms'")
        for column in ("owner_pid", "owner_start_ms"):
            if column not in existing_cols:
                await self.conn.execute(
                    f"ALTER TABLE missions ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
                log.info("MissionEventStore: migration applied — added '%s'", column)

        # V26: upgrade existing V25 databases without reclaiming old leases.
        cur = await self.conn.execute("PRAGMA table_info(mission_tool_execution_leases)")
        lease_columns = {str(row[1]) for row in await cur.fetchall()}
        await cur.close()
        if "activated_ms" not in lease_columns:
            await self.conn.execute(
                "ALTER TABLE mission_tool_execution_leases ADD COLUMN activated_ms INTEGER"
            )
            log.info("MissionEventStore: migration applied — added execution activation fence")
        # V29: the terminal-event trigger requires this column on upgraded DBs.
        # Existing one-shot trace reservations are never reset/reclaimed.
        if "cancel_requested_ms" not in lease_columns:
            await self.conn.execute(
                "ALTER TABLE mission_tool_execution_leases "
                "ADD COLUMN cancel_requested_ms INTEGER"
            )
            log.info("MissionEventStore: migration applied — added execution cancel latch")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("MissionEventStore: call open() before use")
        return self._conn

    # --- Append & Publish (atomic in the WAL sense) ---

    async def append_and_publish(self, envelope: EventEnvelope) -> int:
        """Persist event, then publish to bus. Returns the assigned seq.

        Crash behaviour: WAL + synchronous=NORMAL flushes the INSERT to disk;
        bus publish is lost. Recovery on startup reads
        `events_since(last_known_seq)` and re-broadcasts.

        Strict: `envelope.seq` MUST be None — seq is server-assigned.
        """
        if envelope.seq is not None:
            raise ValueError(
                "append_and_publish: envelope.seq must be None (server-assigned)"
            )

        payload_json = envelope.payload.model_dump_json()
        cur = await self.conn.execute(
            """
            INSERT INTO mission_events
                (event_id, mission_id, event_type, parent_event_id,
                 worker_id, source_actor, ts_ms, schema_version, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING seq
            """,
            (
                envelope.event_id,
                envelope.mission_id,
                envelope.payload.event_type,
                envelope.parent_event_id,
                envelope.worker_id,
                envelope.source_actor,
                envelope.ts_ms,
                envelope.schema_version,
                payload_json,
            ),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING seq returned no value")
        seq = int(row[0])

        envelope_with_seq = envelope.model_copy(update={"seq": seq})
        await self._bus.publish(envelope_with_seq)
        return seq

    # --- Read-API ---

    async def events_since(self, after_seq: int = 0) -> list[EventEnvelope]:
        """All events with `seq > after_seq`, sorted ascending by seq."""
        cur = await self.conn.execute(
            """
            SELECT seq, event_id, mission_id, event_type, parent_event_id,
                   worker_id, source_actor, ts_ms, schema_version, payload_json
            FROM mission_events
            WHERE seq > ?
            ORDER BY seq ASC
            """,
            (after_seq,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_envelope(row) for row in rows]

    async def is_active_for_tool_approval(self, mission_id: str) -> bool:
        """Fail closed against the header AND authoritative lifecycle event log.

        The header is a projection updated *after* MissionStateChanged is
        appended. A crash in that interval can leave a RUNNING header beside
        a persisted cancellation. Read both in one SQLite statement/snapshot
        and never grant from a stale header. This is a read barrier, not a
        distributed lock: another process can cancel after the SELECT.
        """
        active = {
            MissionState.RUNNING.value,
            MissionState.CRITIQUING.value,
            MissionState.LOOPING.value,
        }
        terminal = {
            MissionState.APPROVED.value,
            MissionState.FAILED.value,
            MissionState.CANCELLED.value,
            MissionState.TIMED_OUT.value,
        }
        known_states = {state.value for state in MissionState}
        terminal_events = {
            "MissionApproved", "MissionFailed", "MissionCancelled", "MissionTimedOut"
        }
        cur = await self.conn.execute(
            """
            SELECT m.state, e.event_type, e.payload_json
            FROM missions AS m
            LEFT JOIN mission_events AS e
              ON e.mission_id = m.id
             AND e.event_type IN (
                'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                'MissionCancelled', 'MissionTimedOut'
             )
            WHERE m.id = ?
            ORDER BY e.seq DESC
            """,
            (mission_id,),
        )
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        if not rows or str(rows[0][0]) not in active:
            return False
        # An active mission must have a persisted state transition. A header
        # without one is incomplete/corrupt and must not authorize tools.
        if rows[0][1] is None:
            return False
        latest_state: str | None = None
        for _, event_type, payload_json in rows:
            if event_type in terminal_events:
                return False  # Terminal events are sticky for safety.
            if event_type != "MissionStateChanged":
                return False
            try:
                state = json.loads(payload_json)["to_state"]
            except (TypeError, ValueError, KeyError):
                return False
            if not isinstance(state, str) or state not in known_states:
                return False
            if latest_state is None:
                latest_state = state
            if state in terminal:
                return False  # Includes crash before terminal header update.
        return latest_state in active

    async def try_claim_tool_decision(
        self, *, trace_id: str, mission_id: str, tool_name: str, decision: str
    ) -> bool:
        """Durably claim one mission tool decision across server processes.

        One SQLite INSERT ... SELECT statement atomically checks the persisted
        mission lifecycle and inserts the trace tombstone under SQLite's writer
        lock.  A competing process or restarted instance cannot claim the same
        trace again.  No arguments, credentials, or previews are stored.

        This is NOT an atomic cancellation-vs-EventBus-publish transaction:
        cancellation may commit after this INSERT but before local publish.
        """
        if decision not in {"approved", "denied", "auto_approved"}:
            return False
        if not trace_id or not mission_id or not tool_name:
            return False
        # The SQL reproduces the V19 fail-closed lifecycle check inside the
        # same statement as the durable claim.  A stale RUNNING header alone
        # never authorizes a claim, and any historic terminal/corrupt event
        # makes the mission ineligible.
        cur = await self.conn.execute(
            """
            INSERT INTO mission_tool_approval_decisions
                (trace_id, mission_id, tool_name, decision, claimed_ms)
            SELECT ?, m.id, ?, ?, CAST(strftime('%s','now') AS INTEGER) * 1000
            FROM missions AS m
            WHERE m.id = ?
              AND m.state IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND (
                SELECT CASE
                    WHEN e.event_type = 'MissionStateChanged'
                         AND json_valid(e.payload_json)
                    THEN json_extract(e.payload_json, '$.to_state')
                    ELSE NULL
                END
                FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                ORDER BY e.seq DESC LIMIT 1
              ) IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND NOT EXISTS (
                SELECT 1 FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                  AND (
                    e.event_type != 'MissionStateChanged'
                    OR COALESCE(
                        CASE WHEN json_valid(e.payload_json)
                             THEN json_extract(e.payload_json, '$.to_state')
                        END, '__invalid__'
                    ) NOT IN (
                        'PENDING', 'RUNNING', 'CRITIQUING', 'LOOPING',
                        'WAITING_CAPACITY'
                    )
                  )
              )
            ON CONFLICT(trace_id) DO NOTHING
            RETURNING trace_id
            """,
            (trace_id, tool_name, decision, mission_id),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        return row is not None

    async def compact_terminal_tool_decisions(
        self, *, before_ms: int, limit: int = 500
    ) -> int:
        """Opt-in bounded retention of decisions for *persistently terminal* missions.

        A terminal mission header AND a terminal lifecycle event are required;
        an incomplete or missing event log is never enough to delete audit.
        Every deleted decision atomically leaves a permanent trace tombstone,
        so a restart, competing connection or later mission state change
        cannot turn an old approval/denial into a second decision.

        This intentionally does not compact mission events or running-mission
        decisions. Tombstones are never automatically removed.
        """
        if type(before_ms) is not int or before_ms < 0:
            raise ValueError("before_ms must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        cur = await self.conn.execute(
            """
            DELETE FROM mission_tool_approval_decisions
            WHERE trace_id IN (
                SELECT a.trace_id
                FROM mission_tool_approval_decisions AS a
                JOIN missions AS m ON m.id = a.mission_id
                WHERE a.claimed_ms < ?
                  AND m.state IN ('APPROVED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
                  AND (
                    SELECT CASE
                        WHEN e.event_type IN (
                            'MissionApproved', 'MissionFailed',
                            'MissionCancelled', 'MissionTimedOut'
                        ) THEN e.event_type
                        WHEN e.event_type = 'MissionStateChanged'
                             AND json_valid(e.payload_json)
                        THEN json_extract(e.payload_json, '$.to_state')
                        ELSE NULL
                    END
                    FROM mission_events AS e
                    WHERE e.mission_id = a.mission_id
                      AND e.event_type IN (
                          'MissionStateChanged', 'MissionApproved',
                          'MissionFailed', 'MissionCancelled', 'MissionTimedOut'
                      )
                    ORDER BY e.seq DESC LIMIT 1
                  ) IN (
                      'APPROVED', 'FAILED', 'CANCELLED', 'TIMED_OUT',
                      'MissionApproved', 'MissionFailed',
                      'MissionCancelled', 'MissionTimedOut'
                  )
                ORDER BY a.claimed_ms, a.trace_id LIMIT ?
            )
            RETURNING trace_id
            """,
            (before_ms, limit),
        )
        try:
            return len(await cur.fetchall())
        finally:
            await cur.close()

    async def try_start_tool_execution(
        self, *, trace_id: str, mission_id: str, tool_name: str
    ) -> bool:
        """Atomically reserve one mission-tool execution across processes.

        The lifecycle header AND authoritative event log are checked in the
        same SQLite writer statement as the unique trace claim. A crash leaves
        the claim in place: replay must not re-run a potentially mutating tool.
        This does not lock a mission against a *later* cancellation.
        """
        if not trace_id or not mission_id or not tool_name:
            return False
        cur = await self.conn.execute(
            """
            INSERT INTO mission_tool_execution_leases
                (trace_id, mission_id, tool_name, started_ms)
            SELECT ?, m.id, ?, CAST(strftime('%s','now') AS INTEGER) * 1000
            FROM missions AS m
            WHERE m.id = ?
              AND m.state IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND (
                SELECT CASE
                    WHEN e.event_type = 'MissionStateChanged'
                         AND json_valid(e.payload_json)
                    THEN json_extract(e.payload_json, '$.to_state')
                    ELSE NULL
                END
                FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                ORDER BY e.seq DESC LIMIT 1
              ) IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND NOT EXISTS (
                SELECT 1 FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                  AND (
                    e.event_type != 'MissionStateChanged'
                    OR COALESCE(
                        CASE WHEN json_valid(e.payload_json)
                             THEN json_extract(e.payload_json, '$.to_state')
                        END, '__invalid__'
                    ) NOT IN (
                        'PENDING', 'RUNNING', 'CRITIQUING', 'LOOPING',
                        'WAITING_CAPACITY'
                    )
                  )
              )
            ON CONFLICT(trace_id) DO NOTHING
            RETURNING trace_id
            """,
            (trace_id, tool_name, mission_id),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        return row is not None

    async def try_activate_tool_execution(
        self, *, trace_id: str, mission_id: str, tool_name: str
    ) -> bool:
        """Activate a reserved trace only while its mission is still active.

        This second SQLite *writer* fence serializes activation with a
        cancellation event committed on another connection before activation.
        The unique reservation remains even when activation is refused.
        V29 also refuses an execution whose durable terminal-event latch is set.
        Activation is NOT a lock held while external tool code runs: a cancel
        immediately after this statement must still be handled cooperatively.
        """
        if not trace_id or not mission_id or not tool_name:
            return False
        cur = await self.conn.execute(
            """
            UPDATE mission_tool_execution_leases AS l
            SET activated_ms = CAST(strftime('%s','now') AS INTEGER) * 1000
            WHERE l.trace_id = ? AND l.mission_id = ? AND l.tool_name = ?
              AND l.outcome = 'running' AND l.activated_ms IS NULL
              AND l.cancel_requested_ms IS NULL
              AND EXISTS (
                  SELECT 1 FROM missions AS m WHERE m.id = l.mission_id
              AND m.state IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND (
                SELECT CASE
                    WHEN e.event_type = 'MissionStateChanged'
                         AND json_valid(e.payload_json)
                    THEN json_extract(e.payload_json, '$.to_state')
                    ELSE NULL
                END
                FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                ORDER BY e.seq DESC LIMIT 1
              ) IN ('RUNNING', 'CRITIQUING', 'LOOPING')
              AND NOT EXISTS (
                SELECT 1 FROM mission_events AS e
                WHERE e.mission_id = m.id
                  AND e.event_type IN (
                    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                    'MissionCancelled', 'MissionTimedOut'
                  )
                  AND (
                    e.event_type != 'MissionStateChanged'
                    OR COALESCE(
                        CASE WHEN json_valid(e.payload_json)
                             THEN json_extract(e.payload_json, '$.to_state')
                        END, '__invalid__'
                    ) NOT IN (
                        'PENDING', 'RUNNING', 'CRITIQUING', 'LOOPING',
                        'WAITING_CAPACITY'
                    )
                  )
              )
              )
            RETURNING trace_id
            """,
            (trace_id, mission_id, tool_name),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        return row is not None

    async def try_fence_tool_dispatch(
        self, *, trace_id: str, mission_id: str, tool_name: str
    ) -> bool:
        """Last writer-serialized permit immediately before scheduling a tool.

        A read-only preflight can see the old RUNNING snapshot while another
        connection holds an uncommitted cancellation writer transaction.  This
        no-op UPDATE waits for that writer and checks the sticky per-trace
        cancellation latch *after* it commits.  The V29/V30 triggers latch
        state/event changes in the same transaction as the cancellation.

        The SQLite lock ends with this statement. A cancellation committed
        afterwards can still race with external side effects: NOT a sandbox
        or an atomic cancellation-versus-tool-start transaction.
        """
        if not trace_id or not mission_id or not tool_name:
            return False
        cur = await self.conn.execute(
            """
            UPDATE mission_tool_execution_leases AS l
            SET activated_ms = activated_ms
            WHERE l.trace_id = ? AND l.mission_id = ? AND l.tool_name = ?
              AND l.outcome = 'running' AND l.activated_ms IS NOT NULL
              AND l.cancel_requested_ms IS NULL
              AND EXISTS (
                SELECT 1 FROM missions AS m
                WHERE m.id = l.mission_id
                  AND m.state IN ('RUNNING', 'CRITIQUING', 'LOOPING')
                  AND (
                    SELECT CASE
                        WHEN e.event_type = 'MissionStateChanged'
                             AND json_valid(e.payload_json)
                        THEN json_extract(e.payload_json, '$.to_state')
                        ELSE NULL
                    END
                    FROM mission_events AS e
                    WHERE e.mission_id = m.id
                      AND e.event_type IN (
                        'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                        'MissionCancelled', 'MissionTimedOut'
                      )
                    ORDER BY e.seq DESC LIMIT 1
                  ) IN ('RUNNING', 'CRITIQUING', 'LOOPING')
                  AND NOT EXISTS (
                    SELECT 1 FROM mission_events AS e
                    WHERE e.mission_id = m.id
                      AND e.event_type IN (
                        'MissionStateChanged', 'MissionApproved', 'MissionFailed',
                        'MissionCancelled', 'MissionTimedOut'
                      )
                      AND (
                        e.event_type != 'MissionStateChanged'
                        OR COALESCE(
                            CASE WHEN json_valid(e.payload_json)
                                 THEN json_extract(e.payload_json, '$.to_state')
                            END, '__invalid__'
                        ) NOT IN (
                            'PENDING', 'RUNNING', 'CRITIQUING', 'LOOPING',
                            'WAITING_CAPACITY'
                        )
                      )
                  )
              )
            RETURNING trace_id
            """,
            (trace_id, mission_id, tool_name),
        )
        try:
            return await cur.fetchone() is not None
        finally:
            await cur.close()

    async def is_execution_lease_uncancelled(
        self, *, trace_id: str, mission_id: str, tool_name: str
    ) -> bool:
        """Read the durable cancellation latch for an activated running trace.

        A brief pause/cancel followed by resume can be missed by a periodic
        mission-state poll.  The V29/V30 SQLite triggers preserve that stop
        signal on the *individual trace*, even when the header is active again.
        This read is not an atomic lock on future cancellation or tool effects.
        """
        if not trace_id or not mission_id or not tool_name:
            return False
        cur = await self.conn.execute(
            """
            SELECT 1 FROM mission_tool_execution_leases
            WHERE trace_id = ? AND mission_id = ? AND tool_name = ?
              AND outcome = 'running' AND activated_ms IS NOT NULL
              AND cancel_requested_ms IS NULL
            """,
            (trace_id, mission_id, tool_name),
        )
        try:
            return await cur.fetchone() is not None
        finally:
            await cur.close()

    async def finish_tool_execution(
        self, *, trace_id: str, outcome: str
    ) -> str:
        """Atomically persist the result *and* respect a committed cancel latch.

        V41: a different SQLite writer can pause/cancel a mission after the
        ToolExecutor's final post-result read but before this UPDATE.  The
        CASE executes under SQLite's single-writer lock, so a cancellation
        committed first wins and a requested ``completed`` becomes
        ``cancelled``.  Return the actual persisted outcome for the caller to
        reconcile with its ToolResult.  A missing/already-final trace is an
        error, never evidence of a successful completion.

        This is a commit-time result fence, not an atomic lock across the
        external tool's side effects or a cancellation committed afterwards.
        """
        if outcome not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid execution outcome")
        if not trace_id:
            raise ValueError("missing trace_id")
        cur = await self.conn.execute(
            """
            UPDATE mission_tool_execution_leases
            SET outcome = CASE
                WHEN ? = 'completed' AND cancel_requested_ms IS NOT NULL
                THEN 'cancelled' ELSE ? END,
                finished_ms = CAST(strftime('%s','now') AS INTEGER) * 1000
            WHERE trace_id = ? AND outcome = 'running'
            RETURNING outcome
            """,
            (outcome, outcome, trace_id),
        )
        try:
            row = await cur.fetchone()
        finally:
            await cur.close()
        if row is None:
            raise RuntimeError("execution lease missing or already finalized")
        return str(row[0])

    async def compact_finished_tool_executions(
        self, *, before_ms: int, limit: int = 500
    ) -> int:
        """Opt-in bounded retention without re-enabling old trace IDs.

        Only fully finished rows older than ``before_ms`` are eligible. The
        database trigger writes a permanent minimal tombstone atomically with
        each deletion. Running/crash-orphaned reservations are never pruned.
        No automatic compaction is performed at startup: the caller must
        explicitly choose its audit-retention policy and cutoff.
        """
        if type(before_ms) is not int or before_ms < 0:
            raise ValueError("before_ms must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        cur = await self.conn.execute(
            """
            DELETE FROM mission_tool_execution_leases
            WHERE trace_id IN (
                SELECT trace_id FROM mission_tool_execution_leases
                WHERE outcome != 'running' AND finished_ms IS NOT NULL
                  AND finished_ms < ?
                ORDER BY finished_ms, trace_id LIMIT ?
            )
            RETURNING trace_id
            """,
            (before_ms, limit),
        )
        try:
            return len(await cur.fetchall())
        finally:
            await cur.close()

    async def events_for_mission(self, mission_id: str) -> list[EventEnvelope]:
        """All events for a mission, sorted ascending by seq."""
        cur = await self.conn.execute(
            """
            SELECT seq, event_id, mission_id, event_type, parent_event_id,
                   worker_id, source_actor, ts_ms, schema_version, payload_json
            FROM mission_events
            WHERE mission_id = ?
            ORDER BY seq ASC
            """,
            (mission_id,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [self._row_to_envelope(row) for row in rows]

    # --- Mission header (separate, for recovery + UI snapshots) ---

    async def upsert_mission(
        self,
        *,
        mission_id: str,
        prompt: str,
        state: str,
        language: str,
        ts_ms: int,
        iteration: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Create a mission header or update state + iteration + cost.

        On insert: all fields are written.
        On update (ON CONFLICT): state/updated_ms always; iteration/cost_usd
        only when the new value > current — we do not want to overwrite with
        smaller values (cost accumulates monotonically, iteration only upward).
        """
        await self.conn.execute(
            """
            INSERT INTO missions (
                id, prompt, state, language, created_ms, updated_ms,
                iteration, cost_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                updated_ms = excluded.updated_ms,
                iteration = MAX(missions.iteration, excluded.iteration),
                cost_usd  = MAX(missions.cost_usd, excluded.cost_usd)
            """,
            (
                mission_id,
                prompt,
                state,
                language,
                ts_ms,
                ts_ms,
                iteration,
                cost_usd,
            ),
        )

    async def get_mission_view(
        self, mission_id: str
    ) -> tuple[str, str, str, int, float] | None:
        """Returns `(prompt, state, language, iteration, cost_usd)` or None.

        Convenience API for the Kontrollierer orchestrator (needs everything at once).
        The existing `get_mission_state()` remains unchanged — no breaking change.
        """
        cur = await self.conn.execute(
            "SELECT prompt, state, language, iteration, cost_usd FROM missions WHERE id = ?",
            (mission_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return (str(row[0]), str(row[1]), str(row[2]), int(row[3]), float(row[4]))

    async def advance_mission_iteration(
        self,
        mission_id: str,
        *,
        iteration: int,
        ts_ms: int,
    ) -> None:
        """Advance the header iteration without changing its lifecycle state.

        Critic events and cancellation can race. Reusing ``upsert_mission`` for
        progress would write a previously read state back into the header and
        could therefore revert a concurrent terminal transition. This narrow
        update keeps iteration monotonic while leaving state authoritative.
        """
        await self.conn.execute(
            """
            UPDATE missions
            SET updated_ms = ?, iteration = MAX(iteration, ?)
            WHERE id = ?
            """,
            (ts_ms, iteration, mission_id),
        )

    async def list_missions(
        self,
        *,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List of mission headers. The Phase-4 UI endpoint uses this for
        ``GET /api/missions``. Sorted by created_ms desc (newest first).

        Args:
            state: Optional filter (e.g. ``"RUNNING"``).
            limit: Maximum number of entries (default 100).
        """
        sql = (
            "SELECT id, prompt, state, language, created_ms, updated_ms,"
            " iteration, cost_usd FROM missions"
        )
        params: tuple[Any, ...]
        if state is not None:
            sql += " WHERE state = ?"
            params = (state,)
        else:
            params = ()
        sql += " ORDER BY created_ms DESC LIMIT ?"
        params = params + (int(limit),)
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return [
            {
                "id": str(row[0]),
                "prompt": str(row[1]),
                "state": str(row[2]),
                "language": str(row[3]),
                "created_ms": int(row[4]),
                "updated_ms": int(row[5]),
                "iteration": int(row[6]),
                "cost_usd": float(row[7]),
            }
            for row in rows
        ]

    async def list_non_terminal_missions(self) -> list[tuple[str, str, str]]:
        """Returns `(mission_id, prompt, state)` for non-terminal missions.

        Recovery on startup uses this to find stale RUNNING missions.
        """
        cur = await self.conn.execute(
            """
            SELECT id, prompt, state FROM missions
            WHERE state NOT IN ('APPROVED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
            ORDER BY created_ms ASC
            """
        )
        rows = await cur.fetchall()
        await cur.close()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    async def find_child_missions(
        self, parent_mission_id: str
    ) -> list[tuple[str, str]]:
        """Return ``(child_mission_id, current_state)`` for every mission whose
        ``MissionDispatched`` event names ``parent_mission_id`` as its parent.

        Newest first. The parent link lives only in the event payload (the
        ``missions`` header has no parent column), so we read the dispatch
        events and parse the payload in Python — the same idiom the sub-agent
        registry (``jarvis/agents/registry.py``) uses, avoiding a hard
        dependency on SQLite's JSON1 ``json_extract`` (cloud-first portability).

        Used by the rerun endpoint to stay idempotent: a parent must not spawn
        a second LIVE re-run child while a prior one is still active (forensic
        2026-06-22, mission 019eefcb-cee2 — a ``/rerun`` click-storm created
        nine children for one mission).
        """
        cur = await self.conn.execute(
            """
            SELECT m.id, m.state, e.payload_json
            FROM mission_events e
            JOIN missions m ON m.id = e.mission_id
            WHERE e.event_type = 'MissionDispatched'
            ORDER BY m.created_ms DESC
            """
        )
        rows = await cur.fetchall()
        await cur.close()
        out: list[tuple[str, str]] = []
        for mid, state, payload_json in rows:
            try:
                parent = json.loads(payload_json or "{}").get("parent_mission_id")
            except (ValueError, TypeError):
                parent = None
            if parent == parent_mission_id:
                out.append((str(mid), str(state)))
        return out

    async def get_mission_state(self, mission_id: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT state FROM missions WHERE id = ?",
            (mission_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return str(row[0]) if row is not None else None

    async def touch_heartbeat(self, mission_id: str, ts_ms: int) -> None:
        """Bump a mission's liveness heartbeat (header-only, NO event).

        A live orchestrator calls this periodically while a worker is draining
        so startup recovery can distinguish a busy-but-silent worker from a
        genuinely orphaned mission. Deliberately not an event: it must not bloat
        the event log or wake the flight-recorder wildcard subscriber.

        The calling process also stamps itself as the mission's owner, so
        recovery can tell a mission whose owner died in a restart from one a
        live instance is still running (:mod:`jarvis.missions.ownership`).
        """
        owner_pid, owner_start_ms = current_process_identity()
        await self.conn.execute(
            "UPDATE missions SET last_heartbeat_ms = ?, owner_pid = ?, "
            "owner_start_ms = ? WHERE id = ?",
            (ts_ms, owner_pid, owner_start_ms, mission_id),
        )

    async def get_owner(self, mission_id: str) -> tuple[int, int]:
        """``(pid, start_ms)`` of the process that last heartbeat a mission.

        ``(0, 0)`` when no owner was stamped (old row, or no heartbeat yet).
        """
        cur = await self.conn.execute(
            "SELECT owner_pid, owner_start_ms FROM missions WHERE id = ?",
            (mission_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return (0, 0)
        return (int(row[0] or 0), int(row[1] or 0))

    async def get_heartbeat(self, mission_id: str) -> int:
        """Last heartbeat ms for a mission, or 0 if none/unknown."""
        cur = await self.conn.execute(
            "SELECT last_heartbeat_ms FROM missions WHERE id = ?", (mission_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row is not None and row[0] is not None else 0

    async def wal_checkpoint(self) -> None:
        """Manual WAL checkpoint (TRUNCATE mode)."""
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # --- Helpers ---

    @staticmethod
    def _row_to_envelope(row: Any) -> EventEnvelope:
        (
            seq,
            event_id,
            mission_id,
            event_type,
            parent_event_id,
            worker_id,
            source_actor,
            ts_ms,
            schema_version,
            payload_json,
        ) = row
        payload_dict = json.loads(payload_json)
        # Redundantly ensure the discriminator field (model_dump_json already sets it)
        payload_dict.setdefault("event_type", event_type)
        return EventEnvelope.model_validate(
            {
                "event_id": event_id,
                "seq": seq,
                "mission_id": mission_id,
                "parent_event_id": parent_event_id,
                "worker_id": worker_id,
                "source_actor": source_actor,
                "ts_ms": ts_ms,
                "schema_version": schema_version,
                "payload": payload_dict,
            }
        )
