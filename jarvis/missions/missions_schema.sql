-- Phase-6 Mission-Event-Store-Schema. Idempotent (CREATE IF NOT EXISTS).
-- Lebt unter data/missions.db (separater Lifecycle vs. data/jarvis.db der
-- Phase-5-Memory/Tasks).

CREATE TABLE IF NOT EXISTS missions (
    id              TEXT PRIMARY KEY,           -- UUIDv7
    prompt          TEXT NOT NULL,
    state           TEXT NOT NULL,              -- MissionState.value
    language        TEXT NOT NULL DEFAULT 'de',
    created_ms      INTEGER NOT NULL,
    updated_ms      INTEGER NOT NULL,
    -- Phase-3 (Critic-Loop): pro Mission-Iterations-Counter + Cost-Akkumulator.
    -- Bestehende DBs werden via _apply_migrations() in event_store.py upgegradet
    -- (SQLite hat kein ADD COLUMN IF NOT EXISTS).
    iteration       INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    -- Liveness heartbeat: written by the live orchestrator every ~20 s while a
    -- worker drains. Recovery uses max(last_event_ts, last_heartbeat_ms) as the
    -- freshness timestamp so a busy-but-silent worker (Opus, long tool calls,
    -- Computer-Use) is never swept as orphaned. Not an event: must not bloat
    -- the event log or wake the flight-recorder wildcard subscriber.
    -- Existing DBs are upgraded via _apply_migrations() in event_store.py.
    last_heartbeat_ms INTEGER NOT NULL DEFAULT 0,
    -- Identity of the process that wrote the heartbeat (pid + process start
    -- time in ms). Recovery sweeps a mission at once when this owner is
    -- provably dead instead of waiting out the freshness window
    -- (jarvis/missions/ownership.py). 0 = unknown.
    owner_pid         INTEGER NOT NULL DEFAULT 0,
    owner_start_ms    INTEGER NOT NULL DEFAULT 0,
    subagents_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state);
CREATE INDEX IF NOT EXISTS idx_missions_created ON missions(created_ms);

CREATE TABLE IF NOT EXISTS mission_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,     -- UUIDv7
    mission_id        TEXT NOT NULL,
    event_type        TEXT NOT NULL,            -- Discriminator (MissionDispatched, ...)
    parent_event_id   TEXT,
    worker_id         TEXT,
    source_actor      TEXT NOT NULL,            -- hauptjarvis|kontrollierer|worker|critic|ui|system
    ts_ms             INTEGER NOT NULL,
    schema_version    INTEGER NOT NULL DEFAULT 1,
    payload_json      TEXT NOT NULL             -- Pydantic model_dump_json() output
);

CREATE INDEX IF NOT EXISTS idx_events_mission ON mission_events(mission_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON mission_events(event_type, seq);

-- V22: durable first-writer-wins tool decisions.  Keep minimal metadata only;
-- trace IDs must not be reused, including after process restarts.
CREATE TABLE IF NOT EXISTS mission_tool_approval_decisions (
    trace_id     TEXT PRIMARY KEY,
    mission_id   TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK(decision IN ('approved', 'denied', 'auto_approved')),
    claimed_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_tool_approval_decisions_mission
    ON mission_tool_approval_decisions(mission_id);

-- V36: opt-in retention for decisions belonging to terminal missions.
-- Approval/denial decisions are one-shot even after their detailed audit row
-- has been compacted. Only the opaque trace identifier survives indefinitely.
CREATE TABLE IF NOT EXISTS mission_tool_approval_tombstones (
    trace_id TEXT PRIMARY KEY,
    compacted_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_approval_retention
    ON mission_tool_approval_decisions(claimed_ms, trace_id);

-- Any direct deletion of a decision atomically leaves a non-replayable marker.
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_tombstone_on_delete
AFTER DELETE ON mission_tool_approval_decisions
BEGIN
    INSERT OR IGNORE INTO mission_tool_approval_tombstones
        (trace_id, compacted_ms)
    VALUES (OLD.trace_id, CAST(strftime('%s','now') AS INTEGER) * 1000);
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_tombstone_no_reclaim
BEFORE INSERT ON mission_tool_approval_decisions
WHEN EXISTS (
    SELECT 1 FROM mission_tool_approval_tombstones
    WHERE trace_id = NEW.trace_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;
-- V46: a duplicate trace must never be rewritten by INSERT OR REPLACE
-- (or REPLACE INTO). SQLite's default recursive_triggers=OFF can otherwise
-- bypass the AFTER DELETE tombstone trigger while replacing an existing
-- denial with an approval. Ignore the duplicate BEFORE conflict resolution;
-- the normal ON CONFLICT DO NOTHING ... RETURNING claim stays non-throwing.
-- This does not protect against privileged DDL or arbitrary UPDATE of a
-- decision: it is specifically a SQL REPLACE/replay guard.
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_no_duplicate_replace
BEFORE INSERT ON mission_tool_approval_decisions
WHEN EXISTS (
    SELECT 1 FROM mission_tool_approval_decisions
    WHERE trace_id = NEW.trace_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;
-- V47: approval decisions are one-shot audit records. A direct UPDATE must
-- not change a denial to approval (or alter its mission/tool/claim metadata).
-- Idempotent UPDATEs are allowed for compatibility; privileged schema edits
-- remain outside this ordinary-DML protection.
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_decision_immutable
BEFORE UPDATE ON mission_tool_approval_decisions
WHEN NEW.decision IS NOT OLD.decision
  OR NEW.mission_id IS NOT OLD.mission_id
  OR NEW.tool_name IS NOT OLD.tool_name
  OR NEW.claimed_ms IS NOT OLD.claimed_ms
BEGIN
    SELECT RAISE(ABORT, 'approval decision record is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_trace_immutable
BEFORE UPDATE OF trace_id ON mission_tool_approval_decisions
WHEN NEW.trace_id != OLD.trace_id
BEGIN
    SELECT RAISE(ABORT, 'approval trace id is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_tombstone_no_delete
BEFORE DELETE ON mission_tool_approval_tombstones
BEGIN
    SELECT RAISE(ABORT, 'approval tombstones cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_tombstone_no_update
BEFORE UPDATE ON mission_tool_approval_tombstones
BEGIN
    SELECT RAISE(ABORT, 'approval tombstones cannot be modified');
END;

-- V25: a persistent, one-shot execution reservation. Never reuse a trace ID
-- after a crash; stale "running" entries are deliberately NOT auto-reclaimed.
-- This is an audit/duplicate-start barrier, not an OS process lock or an
-- assurance that a tool can be undone after cancellation.
CREATE TABLE IF NOT EXISTS mission_tool_execution_leases (
    trace_id     TEXT PRIMARY KEY,
    mission_id   TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    started_ms   INTEGER NOT NULL,
    -- V26: activation is a separate SQLite writer fence after reservation.
    -- NULL means the trace was claimed but never authorised to start.
    activated_ms INTEGER,
    -- V29: durable cancel/terminal-event latch, written by the event INSERT
    -- trigger in the SAME SQLite transaction as the lifecycle event.
    cancel_requested_ms INTEGER,
    finished_ms  INTEGER,
    outcome      TEXT NOT NULL DEFAULT 'running'
                 CHECK(outcome IN ('running', 'completed', 'failed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_mission_tool_execution_leases_mission
    ON mission_tool_execution_leases(mission_id, outcome);

-- V35: opt-in bounded retention of finished execution rows. A compacted
-- trace must remain permanently non-replayable even after its detailed row
-- has been removed. Tombstones deliberately retain only the opaque trace ID;
-- no tool arguments, mission prompts, or secrets are copied here.
CREATE TABLE IF NOT EXISTS mission_tool_execution_tombstones (
    trace_id TEXT PRIMARY KEY,
    compacted_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_execution_finished_retention
    ON mission_tool_execution_leases(finished_ms, trace_id)
    WHERE outcome != 'running' AND finished_ms IS NOT NULL;

-- Fail closed: neither a cleanup query nor an accidental direct DELETE may
-- discard a potentially executing (or crash-orphaned) running reservation.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_running_delete
BEFORE DELETE ON mission_tool_execution_leases
WHEN OLD.outcome = 'running'
BEGIN
    SELECT RAISE(ABORT, 'cannot delete a running execution reservation');
END;

-- Deletion and tombstone creation happen in ONE SQLite write transaction.
-- Rollback restores the detailed row and removes the tombstone together.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_tombstone_on_delete
AFTER DELETE ON mission_tool_execution_leases
BEGIN
    INSERT OR IGNORE INTO mission_tool_execution_tombstones
        (trace_id, compacted_ms)
    VALUES (OLD.trace_id, CAST(strftime('%s','now') AS INTEGER) * 1000);
END;

-- The normal first-writer-wins claim must return no row for retired traces,
-- including when another process tries to claim the same trace after restart.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_tombstone_no_reclaim
BEFORE INSERT ON mission_tool_execution_leases
WHEN EXISTS (
    SELECT 1 FROM mission_tool_execution_tombstones
    WHERE trace_id = NEW.trace_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- V45: SQLite's default recursive_triggers=OFF lets INSERT OR REPLACE
-- silently delete an existing PRIMARY KEY row without running the V35
-- BEFORE DELETE safety trigger. That can reset a running/cancelled lease or
-- reopen a completed trace. Guard duplicate IDs BEFORE conflict handling.
-- RAISE(IGNORE) keeps ordinary first-writer-wins claims (ON CONFLICT DO
-- NOTHING ... RETURNING) non-throwing and leaves the existing row untouched.
-- New IDs remain insertable; tombstoned IDs are covered by the V35 trigger.
-- This is a defense against accidental SQL REPLACE, not privileged DDL.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_duplicate_replace
BEFORE INSERT ON mission_tool_execution_leases
WHEN EXISTS (
    SELECT 1 FROM mission_tool_execution_leases
    WHERE trace_id = NEW.trace_id
)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- Trace IDs and tombstones are immutable through ordinary SQL operations.
-- Explicit schema administration can still override triggers, as with any
-- SQLite database; this is not a defence against a privileged attacker.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_trace_id_immutable
BEFORE UPDATE OF trace_id ON mission_tool_execution_leases
WHEN NEW.trace_id != OLD.trace_id
BEGIN
    SELECT RAISE(ABORT, 'execution trace id is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_tombstone_no_delete
BEFORE DELETE ON mission_tool_execution_tombstones
BEGIN
    SELECT RAISE(ABORT, 'execution tombstones cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_tombstone_no_update
BEFORE UPDATE ON mission_tool_execution_tombstones
BEGIN
    SELECT RAISE(ABORT, 'execution tombstones cannot be modified');
END;

-- V48: a reservation is bound to its original mission, tool and start time.
-- Without this guard a second SQL writer could reassign a running lease to a
-- different mission, evading mission-specific cancel fences and audit trails.
-- Lifecycle fields remain writable. This is ordinary-DML hardening only.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_identity_immutable
BEFORE UPDATE ON mission_tool_execution_leases
WHEN NEW.mission_id IS NOT OLD.mission_id
  OR NEW.tool_name IS NOT OLD.tool_name
  OR NEW.started_ms IS NOT OLD.started_ms
BEGIN
    SELECT RAISE(ABORT, 'execution reservation identity is immutable');
END;

-- V44: once a lifecycle/header cancellation has latched a trace, ordinary
-- SQL updates must not erase or rewrite that durable stop signal. Otherwise a
-- buggy second connection could clear it after pause/resume and let the V42/
-- V43 dispatch fences authorize a cancelled tool. Idempotent trigger updates
-- (COALESCE to the original value) and first-time NULL -> timestamp are valid.
-- This protects against accidental SQL mutations, not privileged schema edits,
-- INSERT OR REPLACE, or a cancellation that has not yet committed.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_cancel_latch_immutable
BEFORE UPDATE OF cancel_requested_ms ON mission_tool_execution_leases
WHEN OLD.cancel_requested_ms IS NOT NULL
     AND NEW.cancel_requested_ms IS NOT OLD.cancel_requested_ms
BEGIN
    SELECT RAISE(ABORT, 'execution cancel latch is immutable');
END;

-- V29: any transition out of execution-active states (including a pause)
-- latches outstanding leases in the SAME transaction as the event append.
-- A later resume must not re-enable a previously paused execution trace.
-- This is a durable audit/activation fence, NOT a lock held while a tool runs.
-- Fail closed for invalid MissionStateChanged payloads as well.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_terminal_latch
AFTER INSERT ON mission_events
WHEN NEW.event_type IN (
    'MissionStateChanged', 'MissionApproved', 'MissionFailed',
    'MissionCancelled', 'MissionTimedOut'
) AND (
    NEW.event_type != 'MissionStateChanged'
    OR NOT json_valid(NEW.payload_json)
    OR COALESCE(
        CASE WHEN json_valid(NEW.payload_json)
             THEN json_extract(NEW.payload_json, '$.to_state')
        END, '__invalid__'
    ) NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
)
BEGIN
    UPDATE mission_tool_execution_leases
    SET cancel_requested_ms = COALESCE(
        cancel_requested_ms, CAST(strftime('%s','now') AS INTEGER) * 1000
    )
    WHERE mission_id = NEW.mission_id AND outcome = 'running';
END;

-- V30: a direct header transition is also a durable execution-stop signal.
-- Normal MissionManager transitions append a lifecycle event first (covered
-- by the V29 trigger), but recovery/administrative/legacy writers may update
-- or remove the header without appending an event.  Latch in the SAME SQLite
-- transaction so a separate process cannot activate an old reservation after
-- observing a transient inactive header followed by a resume.  The latch is
-- intentionally irreversible for a given trace; no automatic replay.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_header_state_latch
AFTER UPDATE OF state ON missions
WHEN NEW.state NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
BEGIN
    UPDATE mission_tool_execution_leases
    SET cancel_requested_ms = COALESCE(
        cancel_requested_ms, CAST(strftime('%s','now') AS INTEGER) * 1000
    )
    WHERE mission_id = NEW.id AND outcome = 'running';
END;

-- Deleting a mission header must not leave a previously claimed trace
-- eligible for activation if a header with the same id is later recreated.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_header_delete_latch
AFTER DELETE ON missions
BEGIN
    UPDATE mission_tool_execution_leases
    SET cancel_requested_ms = COALESCE(
        cancel_requested_ms, CAST(strftime('%s','now') AS INTEGER) * 1000
    )
    WHERE mission_id = OLD.id AND outcome = 'running';
END;

-- Also cover an inactive header inserted while a previously reserved trace
-- survives from an interrupted migration/recovery. A new active header never
-- clears a previously set latch.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_header_insert_latch
AFTER INSERT ON missions
WHEN NEW.state NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
BEGIN
    UPDATE mission_tool_execution_leases
    SET cancel_requested_ms = COALESCE(
        cancel_requested_ms, CAST(strftime('%s','now') AS INTEGER) * 1000
    )
    WHERE mission_id = NEW.id AND outcome = 'running';
END;

-- V49: retained trace tombstones are permanent replay barriers. SQLite's
-- INSERT OR REPLACE can bypass ordinary UPDATE/DELETE guards when recursive
-- triggers are disabled. Ignore duplicate tombstone inserts before conflict
-- resolution, preserving the original compacted timestamp and replay barrier.
-- This is ordinary-DML hardening, not protection against privileged DDL.
CREATE TRIGGER IF NOT EXISTS trg_mission_approval_tombstone_no_duplicate_replace
BEFORE INSERT ON mission_tool_approval_tombstones
WHEN EXISTS (SELECT 1 FROM mission_tool_approval_tombstones WHERE trace_id = NEW.trace_id)
BEGIN
    SELECT RAISE(IGNORE);
END;
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_tombstone_no_duplicate_replace
BEFORE INSERT ON mission_tool_execution_tombstones
WHEN EXISTS (SELECT 1 FROM mission_tool_execution_tombstones WHERE trace_id = NEW.trace_id)
BEGIN
    SELECT RAISE(IGNORE);
END;

-- V50: a finalized execution is a one-shot audit record. Ordinary SQL UPDATE
-- must not reopen it or rewrite its terminal outcome/finish timestamp.
-- The legitimate running -> terminal transition remains allowed.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_final_outcome_immutable
BEFORE UPDATE ON mission_tool_execution_leases
WHEN OLD.outcome != 'running' AND (
    NEW.outcome IS NOT OLD.outcome OR NEW.finished_ms IS NOT OLD.finished_ms
)
BEGIN
    SELECT RAISE(ABORT, 'finalized execution outcome is immutable');
END;

-- V51: activation is a one-shot writer-fence milestone. Once the durable
-- activation timestamp is set, ordinary SQL UPDATE must not clear or rewrite
-- it, which could falsify audit state after dispatch. The first NULL -> value
-- transition and idempotent updates remain permitted. This does not make
-- cancel-vs-start atomic or prevent privileged schema manipulation.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_activation_latch_immutable
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NOT NULL
 AND NEW.activated_ms IS NOT OLD.activated_ms
BEGIN
    SELECT RAISE(ABORT, 'execution activation latch is immutable');
END;

-- V52: prevent a second SQL writer from activating a reservation whose
-- cancellation was already durably latched. The normal event-store writer
-- checks this in its WHERE clause; enforce it for accidental direct UPDATEs
-- too. This is NOT atomic with a later external tool start.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_after_cancel
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND NEW.cancel_requested_ms IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'cannot activate cancelled execution');
END;

-- V53: finalized leases cannot be activated for the first time. A terminal
-- outcome is an irreversible stop barrier even when no cancel latch exists.
-- This guards ordinary SQL writers, not an OS-level tool launch race.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_after_finish
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND (OLD.outcome != 'running' OR NEW.outcome != 'running')
BEGIN
    SELECT RAISE(ABORT, 'cannot activate finalized execution');
END;

-- V54: direct SQL INSERT must not create an already activated lease with
-- a cancellation latch or a terminal outcome. UPDATE activation fences
-- cannot inspect a row that is born activated. Legitimate reservation
-- claims start unactivated and remain compatible. Ordinary DML only.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_preactivated_stopped_insert
BEFORE INSERT ON mission_tool_execution_leases
WHEN NEW.activated_ms IS NOT NULL
 AND (NEW.cancel_requested_ms IS NOT NULL OR NEW.outcome != 'running')
BEGIN
    SELECT RAISE(ABORT, 'cannot insert preactivated stopped execution');
END;

-- V57: once a finish timestamp is recorded, ordinary SQL writers cannot
-- erase or rewrite it, even if a malformed running row was given a timestamp.
-- First NULL -> timestamp and idempotent updates remain valid. This does not
-- guarantee external tool launch atomicity or protect against privileged DDL.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_finish_timestamp_latch_immutable
BEFORE UPDATE OF finished_ms ON mission_tool_execution_leases
WHEN OLD.finished_ms IS NOT NULL
 AND NEW.finished_ms IS NOT OLD.finished_ms
BEGIN
    SELECT RAISE(ABORT, 'execution finish timestamp latch is immutable');
END;

-- V58: a finish timestamp is an independent stop signal. Even if a legacy
-- writer left outcome='running', a finished trace must not be activated for
-- the first time. Ordinary SQL UPDATE hardening, not an OS launch lock.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_after_finish_timestamp
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND (OLD.finished_ms IS NOT NULL OR NEW.finished_ms IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'cannot activate finished execution timestamp');
END;

-- V59: a finish timestamp is an independent stop barrier even on INSERT.
-- A malformed legacy row cannot be born both activated and finished while
-- outcome remains running. Ordinary SQL DML protection only.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_preactivated_finished_insert
BEFORE INSERT ON mission_tool_execution_leases
WHEN NEW.activated_ms IS NOT NULL AND NEW.finished_ms IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'cannot insert preactivated finished execution');
END;

-- V60: a surviving but inactive mission header is an independent durable
-- activation stop signal. Reject the first activation even if a legacy writer
-- failed to latch cancellation. Missing headers remain handled by the existing
-- event-store claim path; this is ordinary SQL DML hardening, not an atomic
-- guarantee for an external process launch.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_inactive_header
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM missions
    WHERE id = NEW.mission_id
      AND state NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot activate execution for inactive mission');
END;

-- V61: reject a directly inserted preactivated lease when its existing
-- mission header is inactive. V60 covered UPDATE activation, not INSERT.
-- This is an ordinary SQLite DML fence, not atomic external dispatch.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_preactivated_inactive_header_insert
BEFORE INSERT ON mission_tool_execution_leases
WHEN NEW.activated_ms IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM missions
    WHERE id = NEW.mission_id
      AND state NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot insert activated execution for inactive mission');
END;

-- V62: a missing mission header must not authorize first activation through
-- a direct SQL UPDATE. Reservations may survive header deletion or a failed
-- recovery; absence of an active header is not proof of permission to launch.
-- Existing activated traces are unaffected. This is a SQLite DML fence,
-- not an atomic transaction with an external process start.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_missing_header
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM missions WHERE id = NEW.mission_id)
BEGIN
    SELECT RAISE(ABORT, 'cannot activate execution without mission header');
END;

-- V63: reject a direct preactivated INSERT without an existing mission header.
-- V62 covered the first activation through UPDATE; INSERT must fail closed
-- independently. Ordinary SQLite DML only; no external-launch atomicity.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_preactivated_missing_header_insert
BEFORE INSERT ON mission_tool_execution_leases
WHEN NEW.activated_ms IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM missions WHERE id = NEW.mission_id)
BEGIN
    SELECT RAISE(ABORT, 'cannot insert activated execution without mission header');
END;

-- V64: fail closed when a terminal lifecycle event exists but a stale or
-- manually restored mission header still appears active. A committed terminal
-- event is a durable stop signal for first activation. This is an ordinary
-- SQL writer fence, not an atomic external process-launch transaction.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_activation_terminal_event
BEFORE UPDATE OF activated_ms ON mission_tool_execution_leases
WHEN OLD.activated_ms IS NULL AND NEW.activated_ms IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM mission_events
    WHERE mission_id = NEW.mission_id
      AND (event_type IN ('MissionApproved', 'MissionFailed', 'MissionCancelled', 'MissionTimedOut')
        OR (event_type = 'MissionStateChanged' AND (
            NOT json_valid(payload_json)
            OR COALESCE(CASE WHEN json_valid(payload_json)
                 THEN json_extract(payload_json, '$.to_state') END, '__invalid__')
               NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
        )))
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot activate execution after terminal mission event');
END;

-- V65: a preactivated INSERT must obey the same committed terminal-event
-- barrier as V64 UPDATE activation. This protects ordinary SQL DML only;
-- it does not make an external tool launch atomic with cancellation.
CREATE TRIGGER IF NOT EXISTS trg_mission_execution_no_preactivated_terminal_event_insert
BEFORE INSERT ON mission_tool_execution_leases
WHEN NEW.activated_ms IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM mission_events
    WHERE mission_id = NEW.mission_id
      AND (event_type IN ('MissionApproved', 'MissionFailed', 'MissionCancelled', 'MissionTimedOut')
        OR (event_type = 'MissionStateChanged' AND (
            NOT json_valid(payload_json)
            OR COALESCE(CASE WHEN json_valid(payload_json)
                 THEN json_extract(payload_json, '$.to_state') END, '__invalid__')
               NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
        )))
 )
BEGIN
    SELECT RAISE(ABORT, 'cannot insert activated execution after terminal mission event');
END;

-- V66: a committed terminal lifecycle event is a durable activation barrier.
-- Ordinary DELETE must not erase that evidence and allow a stale RUNNING
-- header plus an unlatched legacy reservation to authorize a later launch.
-- This is DML hardening only, not an atomic OS tool-launch guarantee.
CREATE TRIGGER IF NOT EXISTS trg_mission_terminal_event_no_delete
BEFORE DELETE ON mission_events
WHEN OLD.event_type IN ('MissionApproved', 'MissionFailed', 'MissionCancelled', 'MissionTimedOut')
 OR (OLD.event_type = 'MissionStateChanged' AND (
    NOT json_valid(OLD.payload_json)
    OR COALESCE(CASE WHEN json_valid(OLD.payload_json)
         THEN json_extract(OLD.payload_json, '$.to_state') END, '__invalid__')
       NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
 ))
BEGIN
    SELECT RAISE(ABORT, 'terminal mission event cannot be deleted');
END;

-- V67: terminal lifecycle events are immutable under ordinary SQL UPDATE.
-- V66 blocked DELETE, but rewriting event_type, payload or mission_id could
-- otherwise erase the committed activation barrier. Preserve the entire
-- terminal audit row. Ordinary DML only; privileged DDL remains out of scope.
CREATE TRIGGER IF NOT EXISTS trg_mission_terminal_event_no_update
BEFORE UPDATE ON mission_events
WHEN OLD.event_type IN ('MissionApproved', 'MissionFailed', 'MissionCancelled', 'MissionTimedOut')
 OR (OLD.event_type = 'MissionStateChanged' AND (
    NOT json_valid(OLD.payload_json)
    OR COALESCE(CASE WHEN json_valid(OLD.payload_json)
         THEN json_extract(OLD.payload_json, '$.to_state') END, '__invalid__')
       NOT IN ('RUNNING', 'CRITIQUING', 'LOOPING')
 ))
BEGIN
    SELECT RAISE(ABORT, 'terminal mission event cannot be updated');
END;
