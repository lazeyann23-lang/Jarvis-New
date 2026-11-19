"""run_shell tool: runs shell commands (via subprocess).

Risk tier: monitor — the safety layer decides via whitelist/blacklist whether
confirmation is needed. The whitelist/blacklist matching runs on the FULL
command string inside ``ToolExecutor`` before this tool executes, so it is
independent of how the string is executed below.

Shell choice (redesign 2026-08-08): the brain writes commands for the shell it
ASSUMES, so the tool must (a) run a shell the model expects and (b) declare
that shell in its description. Session forensics (2026-08-04/06/07) showed the
model emitting PowerShell (``Add-Type``, ``Start-Process``) and POSIX
(``head``) commands into the previous ``cmd.exe`` backend — roughly half of
all first attempts failed with "command not found" and only succeeded after a
blind reformulation retry.

- Windows: ``powershell.exe -EncodedCommand`` (Windows PowerShell 5.1, present
  on every supported Windows). Base64-encoding the script sidesteps the
  quoting bugs entirely (forensic 2026-07-13: ``shlex.split(posix=False)``
  kept surrounding quotes, so quoted payloads were echoed back as literals
  with exit 0). A wrapper propagates the native exit code, because
  ``powershell -Command``/-EncodedCommand`` otherwise reports 0 even when the
  last native command failed.
- macOS/Linux: ``/bin/sh -c`` so pipes, ``&&`` and redirects work — the
  historical no-shell exec could not run ``ls | grep foo`` at all. Safety is
  unaffected: pattern matching happens upstream on the raw string.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import signal
import sys
import weakref
from typing import Any

from jarvis.core.process_utils import NO_WINDOW_CREATIONFLAGS
from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.safety.command_impact import DESTRUCTIVE, classify_command
from jarvis.safety.explicit_intent import command_confirms_destruction

log = logging.getLogger(__name__)
# Strong references: a cancelled spawn can still complete asynchronously.
# Never abandon its process (or the task that will reap it).
_PENDING_SPAWN_CLEANUPS: set[asyncio.Task[None]] = set()
# A second cancellation must not abandon cleanup of an already-started shell.
_PENDING_ACTIVE_CLEANUPS: set[asyncio.Task[None]] = set()
# Associate a still-running OS cleanup with the *tool task* that spawned it.
# The mission executor uses this to keep its one-shot execution lease open
# after a second cancellation has already ended the Python tool coroutine.
_TOOL_TASK_CLEANUPS: weakref.WeakKeyDictionary[
    asyncio.Task[Any], asyncio.Task[None]
] = weakref.WeakKeyDictionary()
# A completed cleanup can fail before the executor inspects its tool task.
# Retain only the failure bit (not the process or exception) until the owning
# task is collected, so a failed OS cleanup never looks like a clean finish.
_TOOL_TASK_CLEANUP_FAILURES: weakref.WeakKeyDictionary[
    asyncio.Task[Any], bool
] = weakref.WeakKeyDictionary()


def pending_cleanup_for_task(task: asyncio.Task[Any]) -> asyncio.Task[None] | None:
    """Return an unfinished shell cleanup owned by this tool invocation."""
    cleanup = _TOOL_TASK_CLEANUPS.get(task)
    return cleanup if cleanup is not None and not cleanup.done() else None


def cleanup_failed_for_task(task: asyncio.Task[Any]) -> bool:
    """True if any OS cleanup for this tool task failed or was cancelled.

    A completed cleanup may have been removed from the pending map already;
    keep a weakly keyed failure marker so the execution lease fails closed.
    """
    if _TOOL_TASK_CLEANUP_FAILURES.get(task, False):
        return True
    cleanup = _TOOL_TASK_CLEANUPS.get(task)
    if cleanup is None or not cleanup.done():
        return False
    if cleanup.cancelled():
        return True
    return cleanup.exception() is not None


def _associate_cleanup(cleanup: asyncio.Task[None]) -> None:
    owner = asyncio.current_task()
    if owner is None:
        return
    _TOOL_TASK_CLEANUPS[owner] = cleanup

    def forget(done: asyncio.Task[None]) -> None:
        if done.cancelled() or done.exception() is not None:
            _TOOL_TASK_CLEANUP_FAILURES[owner] = True
        if _TOOL_TASK_CLEANUPS.get(owner) is done:
            _TOOL_TASK_CLEANUPS.pop(owner, None)

    cleanup.add_done_callback(forget)

if sys.platform == "win32":
    _SHELL_LABEL = "Windows PowerShell 5.1"
    _SHELL_HINT = (
        "PowerShell cmdlets, aliases (ls, dir, cat) and pipes work. "
        "POSIX tools like head/grep/awk and cmd-only syntax like 'dir /b' "
        "do NOT work unless explicitly installed."
    )
else:
    _SHELL_LABEL = "/bin/sh (POSIX)"
    _SHELL_HINT = (
        "POSIX sh syntax with pipes, && and redirects works. "
        "Write portable sh, not bash-isms; PowerShell cmdlets do NOT exist."
    )


def _windows_subprocess_env() -> dict[str, str]:
    """Return an environment with a deduplicated PATH.

    Desktop integrations commonly append the same native-runtime directories
    repeatedly, inflating PATH towards the per-variable limit. Remove
    duplicates while keeping the original lookup order. Other platforms never
    use this helper.
    """
    env = dict(os.environ)
    path_key = next((key for key in env if key.lower() == "path"), "PATH")
    entries = env.get(path_key, "").split(os.pathsep)
    deduplicated: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        identity_source = os.path.expandvars(entry.strip().strip('"'))
        identity = os.path.normcase(os.path.normpath(identity_source))
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(entry)
    env[path_key] = os.pathsep.join(deduplicated)
    return env


def _powershell_argv(command: str) -> list[str]:
    """Build the ``powershell.exe`` invocation for ``command``.

    The wrapper script exists for three reasons:
    - ``[Console]::OutputEncoding`` makes native tool output decode as UTF-8
      (guarded: under CREATE_NO_WINDOW there may be no console to configure).
    - PowerShell's process exit code is 0 even when the command inside failed
      (both for native commands and for cmdlet errors — verified by test),
      so failures must be surfaced explicitly: cmdlet errors become
      terminating via ``$ErrorActionPreference = 'Stop'`` and are caught,
      native exit codes propagate via ``$LASTEXITCODE``. Otherwise a failing
      command reports success and sends the brain into the false-success
      retry loop documented in the module docstring.
    - Caught errors are written with ``[Console]::Error.WriteLine`` — raw
      text. Letting them reach PowerShell's own error stream would serialize
      them as CLIXML noise on stderr, which the brain cannot read.
      ``$ProgressPreference`` suppresses progress records for the same reason.
    """
    script = (
        "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        "  & { " + command + " }\n"
        "} catch {\n"
        "  [Console]::Error.WriteLine($_.ToString())\n"
        "  exit 1\n"
        "}\n"
        "if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE }\n"
        "exit 0\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


async def _stop_shell_process(proc: asyncio.subprocess.Process) -> None:
    """Reap an interrupted shell and, where possible, its child processes.

    A cancelled ``communicate()`` does NOT stop a subprocess.  Mission
    cancellation reaches this tool via ``Task.cancel()``; without this cleanup
    a command can keep modifying files after the mission is marked cancelled.
    POSIX commands start in their own session so their process group can be
    killed without signalling JARVIS. Windows uses taskkill /T /F (best effort)
    to include children of PowerShell. Neither mechanism can undo effects or
    reliably stop detached/breakaway grandchildren.
    """
    # A successful signal request does not prove that detached descendants
    # stopped.  But a *known* POSIX signalling/reaping failure must not be
    # reported as successful cleanup: V37 uses this exception to keep the
    # durable mission execution lease in its replay-blocking `running` state.
    posix_cleanup_error: BaseException | None = None
    if sys.platform == "win32":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=NO_WINDOW_CREATIONFLAGS,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=3)
            except TimeoutError:
                try:
                    killer.kill()
                except ProcessLookupError:
                    pass
                await killer.wait()
        except OSError:
            # Windows remains best-effort until real Windows process-tree
            # testing is possible. Do not claim verified Windows containment.
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The entire group may have exited before the cancellation.
            pass
        except OSError as exc:
            # Still attempt to stop/reap the direct child below.  Losing the
            # process group is a security-relevant cleanup failure even if
            # killing the direct shell succeeds: its children may survive.
            posix_cleanup_error = exc

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            if sys.platform != "win32" and posix_cleanup_error is None:
                posix_cleanup_error = exc
            elif sys.platform == "win32":
                raise
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError as exc:
        if sys.platform != "win32":
            # A non-reaped process must never clear a mission execution lease.
            raise RuntimeError("POSIX shell cleanup did not reap the process") from exc
        # Preserve the previous Windows best-effort behaviour until tested.
        pass
    if posix_cleanup_error is not None:
        raise RuntimeError("POSIX shell process-tree cleanup failed") from posix_cleanup_error


async def _stop_started_shell_process(proc: asyncio.subprocess.Process) -> None:
    """Keep process-tree cleanup alive even if the caller is cancelled twice.

    Shielding a coroutine protects it from one cancellation, but without a
    durable task reference a second cancellation can let the caller return
    before the subprocess has been reaped. Retain cleanup until completion;
    this is best-effort OS process cleanup, not rollback or isolation.
    """
    cleanup = asyncio.create_task(_stop_shell_process(proc))
    _PENDING_ACTIVE_CLEANUPS.add(cleanup)
    _associate_cleanup(cleanup)

    def finished(task: asyncio.Task[None]) -> None:
        _PENDING_ACTIVE_CLEANUPS.discard(task)
        try:
            task.result()
        except BaseException:
            log.exception("Interrupted shell process cleanup failed")

    cleanup.add_done_callback(finished)
    await asyncio.shield(cleanup)


async def _spawn_shell_process(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """Spawn with a cancellation-safe handoff of the child process.

    ``create_subprocess_exec`` may have created an OS process before its
    awaitable returns. If cancellation lands in that interval, the caller
    has no process handle and cannot stop the child. Shield only the spawn,
    then reap the resulting process on cancellation. The cleanup task stays
    strongly referenced even if the caller receives a second cancellation.
    This is best-effort process-tree containment, not a sandbox.
    """
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError:
        async def cleanup() -> None:
            try:
                proc = await spawning
            except asyncio.CancelledError:
                return
            except Exception:
                # The spawn failed; no process handle was delivered.
                log.debug("Cancelled shell spawn failed before returning a process", exc_info=True)
                return
            try:
                await _stop_shell_process(proc)
            except Exception:
                # Preserve the failure on the reaper task. V37's cleanup
                # status hook must see it, or a cancelled spawn could still
                # finalize its persistent lease after failed OS containment.
                log.exception("Failed to reap shell created during cancellation")
                raise

        reaper = asyncio.create_task(cleanup())
        _PENDING_SPAWN_CLEANUPS.add(reaper)
        _associate_cleanup(reaper)

        def finished(task: asyncio.Task[None]) -> None:
            _PENDING_SPAWN_CLEANUPS.discard(task)
            try:
                task.result()
            except BaseException:
                log.exception("Cancelled shell spawn reaper failed")

        reaper.add_done_callback(finished)
        # Await the reaper when possible, so the execution lease is not
        # finalised while the spawned process is still alive. If the caller
        # cancels again, the strongly referenced reaper still runs.
        try:
            await asyncio.shield(reaper)
        except Exception:
            # Preserve the original cancellation for ToolExecutor's audit.
            # The failed reaper task remains observable through V37's weak
            # cleanup-failure marker, so its execution lease stays reserved.
            log.warning("Cancelled shell spawn cleanup was not successful")
        raise


class RunShellTool:
    # Optional executor hook: do not finalize a cancelled mission lease while
    # its OS subprocess cleanup still runs after a second Task.cancel().
    pending_cleanup_for_task = staticmethod(pending_cleanup_for_task)
    cleanup_failed_for_task = staticmethod(cleanup_failed_for_task)
    name: str = "run_shell"
    read_only: bool = False
    risk_tier: str = "monitor"
    description: str = (
        f"Runs a shell command via {_SHELL_LABEL} on this machine. "
        "Use it for ANY local outcome a command can achieve: create/delete/"
        "rename/move/copy files and folders, list directories, read file "
        "contents, start programs, timers, system queries — the user never "
        "needs to mention a terminal; translate their goal into a command. "
        f"{_SHELL_HINT} "
        "Commands are matched against the whitelist/blacklist. "
        "Default timeout 30s."
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    f"The command (incl. arguments), written for {_SHELL_LABEL}"
                ),
            },
            "timeout_s": {"type": "number", "default": 30},
            "cwd": {"type": "string", "description": "Working directory", "default": ""},
        },
        "required": ["command"],
    }

    def risk_tier_for_args(self, args: dict[str, Any]) -> str | None:
        """Escalate destructive commands to the ``ask`` tier.

        Same per-action pattern as the gmail tool (forensic 2026-06-19): the
        static ``monitor`` tier stays for reading/modifying commands, but a
        command classified as destructive (rm, del, format, Remove-Item, …)
        requires user confirmation. Whitelist and blacklist keep priority in
        ``RiskTierEvaluator`` — a ``run_shell *`` whitelist entry still
        downgrades everything, so confirmation-averse setups are unchanged.
        Returning ``None`` falls back to the static tier.
        """
        command = (args.get("command") or "").strip()
        if not command:
            return None
        if classify_command(command).level == DESTRUCTIVE:
            return "ask"
        return None

    def intent_confirms_args(self, args: dict[str, Any], utterance: str) -> bool:
        """True when the user's own words already authorize this command.

        Only an unambiguous single deletion of the exact absolute path named
        by the user can waive approval. Inferred targets and general shell
        programs keep the ordinary confirmation workflow.
        """
        command = (args.get("command") or "").strip()
        if not command or classify_command(command).level != DESTRUCTIVE:
            return False
        return command_confirms_destruction(command, utterance)

    def describe_args(self, args: dict[str, Any]) -> dict[str, str] | None:
        """Plain-language impact summary for the confirmation question.

        Consumed by ``ToolExecutor`` when this call is deferred for two-turn
        voice/chat confirmation: the spoken question then says WHAT the
        command would do ("would delete something (rm)") instead of a generic
        "run that?". The command words are DATA (English tool names), the
        surrounding phrasing is localized in
        ``jarvis/voice/tool_confirmation.py``.
        """
        command = (args.get("command") or "").strip()
        if not command:
            return None
        impact = classify_command(command)
        commands = ", ".join(dict.fromkeys(impact.commands))
        return {"level": impact.level, "commands": commands}

    async def execute(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        command = (args.get("command") or "").strip()
        timeout_s = float(args.get("timeout_s", 30))
        cwd = args.get("cwd") or None
        if not command:
            return ToolResult(success=False, output=None, error="command is missing")

        try:
            if sys.platform == "win32":
                proc = await _spawn_shell_process(
                    *_powershell_argv(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    creationflags=NO_WINDOW_CREATIONFLAGS,
                    env=_windows_subprocess_env(),
                )
            else:
                proc = await _spawn_shell_process(
                    "/bin/sh",
                    "-c",
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    creationflags=NO_WINDOW_CREATIONFLAGS,
                    start_new_session=True,
                )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except TimeoutError:
                await _stop_started_shell_process(proc)
                return ToolResult(success=False, output=None, error=f"Timeout after {timeout_s}s")
            except asyncio.CancelledError:
                # The mission watchdog cancels this coroutine; the shell must
                # not outlive it. Re-raise so ToolExecutor records cancellation.
                await _stop_started_shell_process(proc)
                raise
            except Exception:
                await _stop_started_shell_process(proc)
                raise
        except FileNotFoundError as exc:
            return ToolResult(success=False, output=None, error=f"Not found: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, output=None, error=str(exc))

        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
        success = proc.returncode == 0
        return ToolResult(
            success=success,
            output={
                "exit_code": proc.returncode,
                "stdout": stdout[:40_000],
                "stderr": stderr[:20_000],
            },
            error=None if success else f"exit {proc.returncode}",
        )
