"""Task worker — runs Copilot CLI sessions in background threads.

Each worker gets its own CopilotCli instance, working directory, and
MCP config that points back to copenclaw with task-scoped tools.

Directory layout per task::

    .data/.tasks/task-xxxx/
    ├── workspace/                 # Clean dir for actual task work
    │   ├── .github/
    │   │   └── copilot-instructions.md   # Worker system prompt
    │   └── copilot-mcp-config.json       # MCP config for worker
    ├── supervisor/                # Supervisor's own clean dir
    │   ├── .github/
    │   │   └── copilot-instructions.md   # Supervisor system prompt
    │   └── copilot-mcp-config.json       # MCP config for supervisor
    ├── worker.log                 # Streamed worker output
    ├── supervisor.log             # Streamed supervisor output
    └── raw.log                    # Legacy / combined log
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from copenclaw.core.logging_config import (
    append_to_file,
    get_activity_log_path,
    get_worker_log_dir,
)
from copenclaw.core.templates import (
    worker_session_start_prompt,
    worker_resume_session_prompt,
    worker_template,
    supervisor_template,
)
from copenclaw.integrations.copilot_cli import (
    CopilotCli,
    CopilotCliError,
    write_mcp_config,
)

logger = logging.getLogger("copenclaw.worker")
_UNKNOWN_OPTION_STARTUP_WINDOW_SECONDS = 45.0
_UNKNOWN_OPTION_BURST_LIMIT = 3


def _collect_child_processes(root_pid: int) -> list[int]:
    """Best-effort recursive child-process discovery for a worker PID."""
    if root_pid <= 0:
        return []
    parent_map: dict[int, list[int]] = {}
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                return []
            parsed = json.loads(raw)
            rows = parsed if isinstance(parsed, list) else [parsed]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                pid = row.get("ProcessId")
                ppid = row.get("ParentProcessId")
                if isinstance(pid, int) and isinstance(ppid, int):
                    parent_map.setdefault(ppid, []).append(pid)
        else:
            proc = subprocess.run(
                ["ps", "-eo", "pid=,ppid="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                parts = line.strip().split()
                if len(parts) != 2:
                    continue
                try:
                    pid = int(parts[0])
                    ppid = int(parts[1])
                except ValueError:
                    continue
                parent_map.setdefault(ppid, []).append(pid)
    except Exception:  # noqa: BLE001
        return []

    descendants: list[int] = []
    stack = list(parent_map.get(root_pid, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        stack.extend(parent_map.get(pid, []))
    descendants.sort()
    return descendants

# ── System prompt templates ──────────────────────────────────
# Source-of-truth system prompts live in templates/system/*.md and are
# loaded via copenclaw.core.templates.

WORKER_INSTRUCTIONS_TEMPLATE_REMOVED = "Deprecated: use templates/system/worker.md"
SUPERVISOR_INSTRUCTIONS_TEMPLATE = "Deprecated: use templates/system/supervisor.md"


# ── Workspace linking helpers ────────────────────────────────
# Workers/supervisors get their own directory, but we link entries
# from the root workspace so they can access README.md, project
# folders, etc.  On Windows we use hard links for files and
# directory junctions for folders (neither requires admin).

SKIP_LINK_ENTRIES = frozenset({".github", ".data", ".tasks", "copilot-mcp-config.json"})


def _link_entry(src: str, dst: str) -> None:
    """Link a single file or directory from *src* to *dst*.

    Files   → ``os.link()`` (hard link, no admin on Windows).
    Dirs    → ``junction`` on Windows, ``os.symlink`` elsewhere.
    """
    if os.path.isfile(src):
        try:
            os.link(src, dst)
        except OSError:
            # Fallback: copy the file if hard link fails (cross-device, etc.)
            shutil.copy2(src, dst)
    elif os.path.isdir(src):
        if sys.platform == "win32":
            # Directory junction — no admin required
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", dst, src],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(src, dst, target_is_directory=True)


def _link_workspace(source_dir: str, target_dir: str) -> None:
    """Link all top-level entries from *source_dir* into *target_dir*.

    Skips ``.github``, ``.data``, and ``tasks`` so each tier keeps
    its own instructions / data.
    """
    if not source_dir or not os.path.isdir(source_dir):
        return
    os.makedirs(target_dir, exist_ok=True)
    for entry in os.listdir(source_dir):
        if entry in SKIP_LINK_ENTRIES:
            continue
        src = os.path.join(source_dir, entry)
        dst = os.path.join(target_dir, entry)
        if os.path.exists(dst) or os.path.islink(dst):
            continue  # already linked / present
        try:
            _link_entry(src, dst)
        except Exception:  # noqa: BLE001
            logger.debug("Could not link %s → %s", src, dst)


def _sync_workspace(source_dir: str, target_dir: str) -> None:
    """Bi-directional sync between root workspace and a task workspace.

    * Forward: new entries in *source_dir* → link into *target_dir*
    * Reverse: new **real** (non-link) entries in *target_dir* → move
      to *source_dir*, then replace with a link.
    """
    if not source_dir or not os.path.isdir(source_dir):
        return
    if not os.path.isdir(target_dir):
        return

    source_entries = set(os.listdir(source_dir))
    target_entries = set(os.listdir(target_dir))

    # Forward: source → target
    for entry in source_entries:
        if entry in SKIP_LINK_ENTRIES:
            continue
        dst = os.path.join(target_dir, entry)
        if os.path.exists(dst) or os.path.islink(dst):
            continue
        src = os.path.join(source_dir, entry)
        try:
            _link_entry(src, dst)
        except Exception:  # noqa: BLE001
            pass

    # Reverse: target → source (new real entries only)
    for entry in target_entries:
        if entry in SKIP_LINK_ENTRIES:
            continue
        if entry in source_entries:
            continue  # already in root
        dst_path = os.path.join(target_dir, entry)
        # Skip links / junctions — they point elsewhere already
        if os.path.islink(dst_path):
            continue
        if sys.platform == "win32" and os.path.isdir(dst_path):
            # Check for junction
            try:
                import ctypes.wintypes  # noqa: F401
                attrs = ctypes.windll.kernel32.GetFileAttributesW(dst_path)
                if attrs != -1 and (attrs & 0x400):  # FILE_ATTRIBUTE_REPARSE_POINT
                    continue
            except Exception:  # noqa: BLE001
                pass
        src_path = os.path.join(source_dir, entry)
        try:
            shutil.move(dst_path, src_path)
            _link_entry(src_path, dst_path)
            logger.info("Reverse-synced %s → root workspace", entry)
        except Exception:  # noqa: BLE001
            logger.debug("Could not reverse-sync %s", entry)


_MAX_INSTRUCTIONS_CHARS = 50_000


def _write_instructions_file(working_dir: str, content: str) -> str:
    """Write .github/copilot-instructions.md into the working directory."""
    if len(content) > _MAX_INSTRUCTIONS_CHARS:
        raise ValueError(
            f"Instructions too large ({len(content):,} chars, limit {_MAX_INSTRUCTIONS_CHARS:,}). "
            "Reduce the task prompt or context size."
        )
    github_dir = os.path.join(working_dir, ".github")
    os.makedirs(github_dir, exist_ok=True)
    instructions_path = os.path.join(github_dir, "copilot-instructions.md")
    with open(instructions_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Wrote instructions file: %s (%d chars)", instructions_path, len(content))
    return instructions_path


def _activity_log_path() -> str:
    return get_activity_log_path()


def _log_to_file(path: str, line: str) -> None:
    """Append a timestamped line to a log file, flushing immediately."""
    append_to_file(path, line)


class WorkerThread:
    """Manages a background Copilot CLI session for a task.

    Writes task-specific instructions to .github/copilot-instructions.md
    in a clean ``workspace/`` subdirectory, writes MCP config there too,
    then launches Copilot CLI with a short trigger prompt via -p.

    Output is streamed to:
    - ``on_output`` callback (for real-time forwarding)
    - ``<task_dir>/worker.log`` (per-task persistent log)
    - ``.data/activity.log`` (unified activity stream)
    - Python logger at INFO level
    """

    def __init__(
        self,
        task_id: str,
        prompt: str,
        working_dir: str,
        mcp_server_url: str,
        mcp_token: Optional[str] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_complete: Optional[Callable[[str, str], None]] = None,
        timeout: int = 600,
        root_workspace_dir: Optional[str] = None,
        resume_session_id: Optional[str] = None,
    ) -> None:
        self.task_id = task_id
        self.prompt = prompt
        self.working_dir = working_dir          # The task directory
        self.mcp_server_url = mcp_server_url
        self.mcp_token = mcp_token
        self.on_output = on_output      # callback(task_id, output_text)
        self.on_complete = on_complete   # callback(task_id, final_output)
        self.timeout = timeout
        self.root_workspace_dir = root_workspace_dir  # Main workspace (e.g. ~/.copenclaw)
        self.resume_session_id = resume_session_id  # Resume previous worker session

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._session_id: Optional[str] = None
        self._accumulated_output: list[str] = []
        self._last_pid: Optional[int] = None
        self._last_child_pids: list[int] = []

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def pid(self) -> Optional[int]:
        if self._process is not None:
            return self._process.pid
        return self._last_pid

    @property
    def exit_code(self) -> Optional[int]:
        if self._process is None or self._process.poll() is None:
            return None
        return self._process.returncode

    def process_snapshot(self) -> dict[str, Any]:
        root_pid = self.pid
        root_running = bool(self._process is not None and self._process.poll() is None)
        child_pids = _collect_child_processes(root_pid) if root_pid else []
        if child_pids:
            self._last_child_pids = child_pids
        elif root_running and self._last_child_pids:
            child_pids = list(self._last_child_pids)
        active_pids = ([root_pid] if root_pid and root_running else []) + child_pids
        return {
            "pid": root_pid,
            "child_pids": child_pids,
            "active_pids": list(dict.fromkeys(active_pids)),
            "running": bool(active_pids),
            "observed_at": datetime.now(timezone.utc),
        }

    @property
    def workspace_dir(self) -> str:
        """The clean workspace subdirectory where the worker actually runs."""
        return os.path.join(self.working_dir, "workspace")

    @property
    def worker_log_path(self) -> str:
        return os.path.join(self.working_dir, "worker.log")

    def _build_cli(self, mcp_config_path: Optional[str] = None) -> CopilotCli:
        """Create a CopilotCli instance pointing at the workspace subdirectory.

        Adds the repo root directory and the main workspace root via
        ``--add-dir`` so Copilot CLI can use its built-in file tools
        (read/write/edit) on project files including README.md.
        """
        # Grant access to the repo root so Copilot can read/write files directly
        repo_root = os.path.abspath(os.getenv("copenclaw_REPO_ROOT", "."))
        add_dirs = [repo_root]
        # Grant access to the main workspace root (where README.md lives)
        if self.root_workspace_dir:
            abs_root = os.path.abspath(self.root_workspace_dir)
            if abs_root != repo_root:
                add_dirs.append(abs_root)
        # Also grant access to the task workspace itself
        ws = self.workspace_dir
        if os.path.isdir(ws) and os.path.abspath(ws) not in {os.path.abspath(d) for d in add_dirs}:
            add_dirs.append(os.path.abspath(ws))
        return CopilotCli(
            workspace_dir=self.workspace_dir,
            timeout=0,  # Not used — we manage the process ourselves
            mcp_server_url=None,
            mcp_token=self.mcp_token,
            add_dirs=add_dirs,
            mcp_config_path=mcp_config_path,
        )

    @property
    def _central_worker_log(self) -> str:
        return os.path.join(get_worker_log_dir(self.task_id), "worker.log")

    def _log(self, line: str) -> None:
        """Log a worker output line to all destinations."""
        tag = f"WORKER {self.task_id[:12]}"
        logger.info("[%s] %s", tag, line[:500])
        _log_to_file(self.worker_log_path, line)
        _log_to_file(self._central_worker_log, line)
        _log_to_file(_activity_log_path(), f"[{tag}] {line}")

    def _log_worker_telemetry(self, cli: "CopilotCli", output: str) -> None:
        """Log structured telemetry for this worker turn.  Pure Python."""
        try:
            from copenclaw.core.telemetry import harvest_session_metrics_fast
            from copenclaw.core.context_budget import ContextBudget

            sid = self._session_id
            if not sid:
                return

            metrics = harvest_session_metrics_fast(sid)
            if not metrics:
                return

            # Check budget (pure arithmetic)
            budget = ContextBudget()
            budget_status = budget.check(sid)

            record = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "role": "worker",
                "task_id": self.task_id,
                "session_id": sid,
                "model": metrics.model,
                "current_tokens": metrics.current_tokens,
                "conversation_tokens": metrics.conversation_tokens,
                "overhead_tokens": metrics.overhead_tokens,
                "context_utilization_pct": round(metrics.context_utilization_pct, 1),
                "output_chars": len(output),
                "budget_warning": budget_status.warning if budget_status else False,
                "budget_needs_rotation": budget_status.needs_rotation if budget_status else False,
            }

            data_dir = os.path.dirname(os.path.dirname(self.worker_log_path))
            telemetry_path = os.path.join(data_dir, "context-telemetry.jsonl")
            with open(telemetry_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

            self._log(
                f"TELEMETRY | context: {record['context_utilization_pct']}% "
                f"| tokens: {metrics.current_tokens} "
                f"| conversation: {metrics.conversation_tokens} "
                f"| overhead: {metrics.overhead_tokens}"
                + (" | ⚠️ BUDGET WARNING" if record.get("budget_warning") else "")
                + (" | 🔴 ROTATION NEEDED" if record.get("budget_needs_rotation") else "")
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Worker telemetry harvest failed for %s: %s", self.task_id, exc)

    def start(self) -> None:
        """Start the worker thread."""
        if self.is_running:
            logger.warning("Worker %s already running", self.task_id)
            return

        self._stop_event.clear()
        self._accumulated_output = []
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Worker thread started for task %s", self.task_id)

    def stop(self) -> None:
        """Signal the worker to stop and terminate the subprocess."""
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            pid = self._process.pid
            logger.info("Terminating worker process for task %s (pid=%s)", self.task_id, pid)
            try:
                if sys.platform == "win32":
                    # On Windows, use taskkill /F /T to kill the entire process
                    # tree (Copilot CLI spawns child processes like node).
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=15,
                    )
                    logger.info("taskkill /F /T sent for worker %s (pid=%s)", self.task_id, pid)
                else:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        logger.warning("Force-killed worker process for task %s", self.task_id)
            except OSError as exc:
                logger.error("Error terminating worker %s: %s", self.task_id, exc)
            except subprocess.TimeoutExpired:
                logger.warning("taskkill timed out for worker %s, falling back to kill", self.task_id)
                try:
                    self._process.kill()
                except OSError:
                    pass
        logger.info("Worker %s stop requested", self.task_id)

    def _run(self) -> None:
        """Main worker execution — writes instructions + MCP config, then launches CLI."""
        sync_stop: Optional[threading.Event] = None
        try:
            # Create a CLEAN workspace subdirectory for the actual task work
            ws = self.workspace_dir
            os.makedirs(ws, exist_ok=True)

            # Resolve workspace root for the instructions template
            ws_root = os.path.abspath(self.root_workspace_dir) if self.root_workspace_dir else os.path.abspath(ws)

            # Link root workspace entries into the worker workspace
            if self.root_workspace_dir and os.path.isdir(self.root_workspace_dir):
                _link_workspace(self.root_workspace_dir, ws)
                self._log(f"Linked workspace from {self.root_workspace_dir}")

            # Write task-specific instructions into the workspace
            instructions = worker_template(
                task_id=self.task_id,
                prompt=self.prompt,
                workspace_root=ws_root,
            )
            _write_instructions_file(ws, instructions)

            # Write MCP config INTO the workspace with task routing params
            mcp_config_path = write_mcp_config(
                target_dir=ws,
                mcp_server_url=self.mcp_server_url,
                mcp_token=self.mcp_token,
                task_id=self.task_id,
                role="worker",
            )

            cli = self._build_cli(mcp_config_path=mcp_config_path)

            # If resuming a previous session, set the resume ID on the CLI
            if self.resume_session_id:
                cli.resume_session_id = self.resume_session_id
                self._log(f"Resuming previous worker session: {self.resume_session_id}")

            # Build the command — short trigger prompt since instructions are in the file
            cmd = cli.build_launch_command(require_subprocess=True)
            if self.resume_session_id:
                cmd.extend(["-p", worker_resume_session_prompt(task_id=self.task_id)])
            else:
                cmd.extend(["-p", worker_session_start_prompt(task_id=self.task_id)])

            env = os.environ.copy()
            env.setdefault("TERM", "dumb")
            env["PYTHONIOENCODING"] = "utf-8"

            # Start periodic workspace sync thread
            if self.root_workspace_dir and os.path.isdir(self.root_workspace_dir):
                sync_stop = threading.Event()
                def _sync_loop(stop_evt: threading.Event) -> None:
                    while not stop_evt.wait(30):
                        try:
                            _sync_workspace(self.root_workspace_dir, ws)
                        except Exception:  # noqa: BLE001
                            pass
                sync_thread = threading.Thread(target=_sync_loop, args=(sync_stop,), daemon=True)
                sync_thread.start()

            self._log(f"Launching Copilot CLI (cwd={ws})")
            self._log(f"MCP config: {mcp_config_path}")
            self._log(f"Prompt: {self.prompt[:200]}")

            logger.info(
                "Worker %s launching Copilot CLI (cwd=%s, cmd_len=%d)",
                self.task_id, ws, len(cmd),
            )

            # Launch with Popen for streaming
            self._process = subprocess.Popen(
                cmd,
                cwd=ws,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                text=True,
                env=env,
                encoding="utf-8",
                errors="replace",
                # On Windows, CREATE_NEW_PROCESS_GROUP lets us terminate cleanly
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            self._last_pid = self._process.pid
            self._last_child_pids = []

            self._log(f"Process started (pid={self._process.pid})")

            # Timeout enforcement
            timed_out = False
            timeout_timer: Optional[threading.Timer] = None
            if self.timeout and self.timeout > 0:
                def _on_timeout() -> None:
                    nonlocal timed_out
                    timed_out = True
                    if self._process and self._process.poll() is None:
                        try:
                            self._process.kill()
                        except OSError:
                            pass

                timeout_timer = threading.Timer(self.timeout, _on_timeout)
                timeout_timer.daemon = True
                timeout_timer.start()

            # Stream stdout line-by-line
            assert self._process.stdout is not None
            startup_deadline = time.monotonic() + _UNKNOWN_OPTION_STARTUP_WINDOW_SECONDS
            unknown_option_hits = 0
            for line in self._process.stdout:
                if self._stop_event.is_set():
                    self._log("Stop event received, breaking stream")
                    break

                line = line.rstrip("\n\r")
                if line:
                    self._accumulated_output.append(line)
                    self._log(line)
                    if "unknown option '--no-warnings'" in line.lower():
                        if time.monotonic() <= startup_deadline:
                            unknown_option_hits += 1
                        if unknown_option_hits >= _UNKNOWN_OPTION_BURST_LIMIT:
                            self._log(
                                "Detected repeated '--no-warnings' unknown-option failures during startup; terminating worker process."
                            )
                            if self._process and self._process.poll() is None:
                                self._process.terminate()
                            break
                    if self.on_output:
                        self.on_output(self.task_id, line)

            # Wait for process to finish
            if self._process.poll() is None:
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.terminate()

            if timeout_timer:
                timeout_timer.cancel()

            exit_code = self._process.returncode
            final_output = "\n".join(self._accumulated_output)

            self._log(f"Process exited (code={exit_code}, output_lines={len(self._accumulated_output)})")

            # Discover and store session ID for potential future resume.
            # The CLI adapter captures this via directory snapshot automatically.
            discovered = cli._last_discovered_session_id
            if discovered:
                self._session_id = discovered
                self._log(f"Captured worker session: {discovered}")

            # Harvest and log worker telemetry (pure Python, no LLM calls)
            self._log_worker_telemetry(cli, final_output)

            # Final sync before completing
            if self.root_workspace_dir:
                try:
                    _sync_workspace(self.root_workspace_dir, ws)
                except Exception:  # noqa: BLE001
                    pass

            if self.on_complete:
                if timed_out:
                    self.on_complete(self.task_id, f"ERROR: worker timed out after {self.timeout}s")
                elif exit_code != 0:
                    self.on_complete(self.task_id, f"ERROR (exit {exit_code}): {final_output[-500:]}")
                else:
                    self.on_complete(self.task_id, final_output)

        except Exception as exc:  # noqa: BLE001
            logger.error("Worker %s unexpected error: %s", self.task_id, exc, exc_info=True)
            self._log(f"UNEXPECTED ERROR: {exc}")
            if self.on_complete:
                self.on_complete(self.task_id, f"UNEXPECTED ERROR: {exc}")
        finally:
            if sync_stop:
                sync_stop.set()


class SupervisorThread:
    """Periodically checks on a worker and provides guidance.

    The supervisor runs its own Copilot CLI session in a ``supervisor/``
    subdirectory of the task directory. It writes its own instructions
    and MCP config there, keeping it fully isolated from the worker's
    workspace.
    """

    def __init__(
        self,
        task_id: str,
        prompt: str,
        worker_session_id: Optional[str],
        mcp_server_url: str,
        mcp_token: Optional[str] = None,
        check_interval: int = 600,
        on_output: Optional[Callable[[str, str], None]] = None,
        timeout: int = 120,
        working_dir: Optional[str] = None,
        root_workspace_dir: Optional[str] = None,
        task_manager: Optional[Any] = None,
        worker_pool: Optional[Any] = None,
    ) -> None:
        self.task_id = task_id
        self.prompt = prompt
        self.worker_session_id = worker_session_id
        self.mcp_server_url = mcp_server_url
        self.mcp_token = mcp_token
        self.check_interval = check_interval
        self.on_output = on_output
        self.timeout = timeout
        self.working_dir = working_dir   # The task directory (parent)
        self.root_workspace_dir = root_workspace_dir  # Main workspace (e.g. ~/.copenclaw)
        self._task_manager = task_manager  # For contextual trigger prompts
        self._worker_pool = worker_pool    # For checking worker state

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._kick_event = threading.Event()
        self._session_id: Optional[str] = None
        self._sup_dir: Optional[str] = None
        self.last_check_requested_at: Optional[float] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def supervisor_log_path(self) -> str:
        if self.working_dir:
            return os.path.join(self.working_dir, "supervisor.log")
        data_dir = os.getenv("copenclaw_DATA_DIR", ".data")
        return os.path.join(data_dir, "supervisors", self.task_id, "supervisor.log")

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"supervisor-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("Supervisor thread started for task %s (scheduled checks)", self.task_id)

    def stop(self) -> None:
        self._stop_event.set()
        self._kick_event.set()

    def request_check(self) -> None:
        """Request an immediate supervisor check."""
        self.last_check_requested_at = time.time()
        self._kick_event.set()

    def update_worker_session(self, session_id: str) -> None:
        """Update the worker session ID (may be set after worker starts)."""
        self.worker_session_id = session_id

    def _get_supervisor_dir(self) -> str:
        """Get or create a dedicated directory for the supervisor.

        This is a ``supervisor/`` subdirectory of the task directory
        (NOT inside the worker's workspace/ directory).
        """
        if self._sup_dir:
            return self._sup_dir

        if self.working_dir:
            self._sup_dir = os.path.join(self.working_dir, "supervisor")
        else:
            data_dir = os.getenv("copenclaw_DATA_DIR", ".data")
            self._sup_dir = os.path.join(data_dir, "supervisors", self.task_id)

        os.makedirs(self._sup_dir, exist_ok=True)
        return self._sup_dir

    @property
    def _central_supervisor_log(self) -> str:
        return os.path.join(get_worker_log_dir(self.task_id), "supervisor.log")

    def _log(self, line: str) -> None:
        """Log a supervisor output line to all destinations."""
        tag = f"SUPERVISOR {self.task_id[:12]}"
        logger.info("[%s] %s", tag, line[:500])
        _log_to_file(self.supervisor_log_path, line)
        _log_to_file(self._central_supervisor_log, line)
        _log_to_file(_activity_log_path(), f"[{tag}] {line}")

    def _build_trigger_prompt(self, check_count: int) -> str:
        """Build a contextual trigger prompt based on current task/worker state.

        Instead of always saying "check on the worker", this adapts the
        prompt to tell the supervisor exactly what's happening and what
        action is needed — especially when the worker has exited or
        completion is deferred.
        """
        base = f"You are supervisor for task {self.task_id}. "

        # Try to get task state from task_manager
        task = None
        if self._task_manager:
            task = self._task_manager.get(self.task_id)

        # Check if worker is still running
        worker_running = True
        worker_pid: Optional[int] = None
        child_pids: list[int] = []
        if self._worker_pool:
            worker = self._worker_pool.get_worker(self.task_id)
            if worker:
                snapshot = worker.process_snapshot()
                worker_pid = snapshot.get("pid")
                child_pids = list(snapshot.get("child_pids", []))
                worker_running = bool(snapshot.get("running")) or worker.is_running
            else:
                worker_running = False

        if task and task.completion_deferred and not worker_running:
            # CRITICAL: Worker exited + completion deferred = must finalize NOW
            return (
                base +
                f"URGENT: The worker has EXITED and reported completion (deferred for your verification). "
                f"Worker said: \"{task.completion_deferred_summary[:200]}\". "
                f"You have already assessed {task.supervisor_assessment_count} time(s) without finalizing. "
                f"You MUST make a final decision NOW. Use task_read_peer to review, then: "
                f"report type='completed' if the work looks good, or type='failed' if it does not. "
                f"If you keep reporting type='assessment', the system will auto-finalize after repeated checks."
            )
        elif task and task.completion_deferred and worker_running:
            # Worker still running but reported completion — verify
            return (
                base +
                f"The worker has reported completion but is still running (waiting for your verification). "
                f"Worker said: \"{task.completion_deferred_summary[:200]}\". "
                f"Use task_read_peer to review the worker's output, inspect files in workers-workspace/, "
                f"and report type='completed' if satisfied or use task_send_input to request fixes."
            )
        elif not worker_running and task and task.status == "running":
            # Worker died without reporting completion
            pid_hint = f"Last known PID: {worker_pid}. " if worker_pid else ""
            return (
                base +
                f"WARNING: The worker process has EXITED but the task is still marked as running. "
                + pid_hint +
                f"The worker may have crashed or gotten stuck. Use task_read_peer to review what happened. "
                f"If the work was completed, report type='completed'. If it failed, report type='failed'. "
                f"If the worker needs to be re-dispatched, report type='escalation'."
            )
        elif task and task.last_worker_activity_at:
            # Check for idle worker
            now = datetime.now(timezone.utc)
            idle_secs = int((now - task.last_worker_activity_at).total_seconds())
            stall_threshold = max(900, self.check_interval * 3)
            process_tree_active = len(child_pids) > 0
            if idle_secs > stall_threshold and worker_running:
                child_hint = f"Observed child processes: {len(child_pids)}. " if process_tree_active else ""
                return (
                    base +
                    f"The worker has had no MCP activity for {idle_secs // 60}m {idle_secs % 60}s. "
                    f"It may be STUCK on a blocking command or in an infinite loop. "
                    + child_hint +
                    f"Use task_read_peer to check its latest activity, and if stuck, "
                    f"use task_send_input to give guidance or report type='intervention'."
                )
            if idle_secs > 300 and worker_running and process_tree_active:
                return (
                    base +
                    f"The worker has no recent MCP activity ({idle_secs // 60}m {idle_secs % 60}s), "
                    f"but its process tree is still active ({len(child_pids)} child process(es)). "
                    f"Monitor passively and do NOT send assessment/intervention unless clear failure signals appear."
                )

        # Normal check
        return (
            base +
            "The worker appears healthy. Monitor passively; do not send routine assessments or nudges. "
            "Only use task_report when you detect clear failure indicators (worker exited unexpectedly, "
            "repeated hard errors, or sustained stall beyond threshold)."
        )

    def _run(self) -> None:
        """Supervisor loop — wait for external triggers to check on the worker."""
        # Create supervisor directory and write instructions + MCP config
        sup_dir = self._get_supervisor_dir()

        # Resolve workspace root for the instructions template
        ws_root = os.path.abspath(self.root_workspace_dir) if self.root_workspace_dir else os.path.abspath(sup_dir)

        # Link the worker's workspace into the supervisor directory as "workers-workspace"
        worker_ws = os.path.join(self.working_dir, "workspace") if self.working_dir else None
        if worker_ws and os.path.isdir(worker_ws):
            dst = os.path.join(sup_dir, "workers-workspace")
            if not os.path.exists(dst) and not os.path.islink(dst):
                try:
                    _link_entry(worker_ws, dst)
                    self._log(f"Linked worker workspace as workers-workspace")
                except Exception:  # noqa: BLE001
                    logger.debug("Could not link worker workspace into supervisor dir")

        instructions = supervisor_template(
            task_id=self.task_id,
            prompt=self.prompt,
            worker_session_id=self.worker_session_id or "(unknown)",
            workspace_root=ws_root,
        )
        _write_instructions_file(sup_dir, instructions)

        # Write MCP config INTO the supervisor directory with task routing params
        mcp_config_path = write_mcp_config(
            target_dir=sup_dir,
            mcp_server_url=self.mcp_server_url,
            mcp_token=self.mcp_token,
            task_id=self.task_id,
            role="supervisor",
        )

        self._log(f"Supervisor directory: {sup_dir}")
        self._log(f"MCP config: {mcp_config_path}")

        # Build add_dirs list — grant access to workspace root + worker workspace
        add_dirs: list[str] = []
        if self.root_workspace_dir:
            add_dirs.append(os.path.abspath(self.root_workspace_dir))
        if worker_ws and os.path.isdir(worker_ws):
            abs_worker_ws = os.path.abspath(worker_ws)
            if abs_worker_ws not in {os.path.abspath(d) for d in add_dirs}:
                add_dirs.append(abs_worker_ws)

        cli = CopilotCli(
            workspace_dir=sup_dir,
            timeout=self.timeout,
            mcp_server_url=None,
            mcp_token=self.mcp_token,
            mcp_config_path=mcp_config_path,
            add_dirs=add_dirs,
        )

        check_count = 0
        while not self._stop_event.is_set():
            self._log("Waiting for check trigger...")
            self._kick_event.wait()
            self._kick_event.clear()
            if self._stop_event.is_set():
                break

            check_count += 1
            self._log(f"--- Check #{check_count} ---")
            try:
                # Build contextual trigger prompt based on task state
                trigger = self._build_trigger_prompt(check_count)

                # Resume previous session so MCP tools remain available
                output = cli.run_prompt(
                    trigger,
                    log_prefix=f"SUPERVISOR {self.task_id[:12]}",
                    resume_id=self._session_id,
                    autopilot=False,
                )

                # Always re-discover session ID (may change between checks).
                # The CLI adapter captures this via directory snapshot automatically.
                discovered = cli._last_discovered_session_id
                if discovered and discovered != self._session_id:
                    self._session_id = discovered
                    self._log(f"Captured supervisor session: {discovered}")
                elif not self._session_id and cli.session_id:
                    self._session_id = cli.session_id

                self._log(f"Check result: {output[:300]}")

                if self.on_output:
                    self.on_output(self.task_id, output)

            except CopilotCliError as exc:
                self._log(f"Check failed: {exc}")
                logger.error("Supervisor %s check failed: %s", self.task_id, exc)
            except Exception as exc:  # noqa: BLE001
                self._log(f"Unexpected error: {exc}")
                logger.error("Supervisor %s unexpected error: %s", self.task_id, exc)

        self._log("Supervisor loop ended")


class WorkerPool:
    """Manages all active worker and supervisor threads."""

    def __init__(
        self,
        mcp_server_url: str,
        mcp_token: Optional[str] = None,
        supervisor_timeout: int = 120,
        worker_timeout: int = 600,
        root_workspace_dir: Optional[str] = None,
    ) -> None:
        self.mcp_server_url = mcp_server_url
        self.mcp_token = mcp_token
        self.supervisor_timeout = supervisor_timeout
        self.worker_timeout = worker_timeout
        self.root_workspace_dir = root_workspace_dir

        self._workers: Dict[str, WorkerThread] = {}
        self._supervisors: Dict[str, SupervisorThread] = {}
        self._lock = threading.Lock()

    def start_worker(
        self,
        task_id: str,
        prompt: str,
        working_dir: str,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_complete: Optional[Callable[[str, str], None]] = None,
        resume_session_id: Optional[str] = None,
    ) -> WorkerThread:
        """Start a new worker thread for a task.

        If *resume_session_id* is provided, the worker will resume the
        previous Copilot CLI session via ``--resume``, giving it full
        context of earlier work on this task.
        """
        with self._lock:
            # If re-dispatching, grab the previous worker's session ID
            if not resume_session_id and task_id in self._workers:
                prev = self._workers[task_id]
                if prev.session_id:
                    resume_session_id = prev.session_id
                    logger.info(
                        "Re-dispatch worker %s: inheriting session %s",
                        task_id, resume_session_id,
                    )

            if task_id in self._workers and self._workers[task_id].is_running:
                raise RuntimeError(f"Worker already running for task {task_id}")

            worker = WorkerThread(
                task_id=task_id,
                prompt=prompt,
                working_dir=working_dir,
                mcp_server_url=self.mcp_server_url,
                mcp_token=self.mcp_token,
                on_output=on_output,
                on_complete=on_complete,
                timeout=self.worker_timeout,
                root_workspace_dir=self.root_workspace_dir,
                resume_session_id=resume_session_id,
            )
            self._workers[task_id] = worker
            worker.start()
            return worker

    def start_supervisor(
        self,
        task_id: str,
        prompt: str,
        worker_session_id: Optional[str] = None,
        check_interval: int = 600,
        on_output: Optional[Callable[[str, str], None]] = None,
        working_dir: Optional[str] = None,
        task_manager: Optional[Any] = None,
    ) -> SupervisorThread:
        """Start a supervisor thread for a task."""
        with self._lock:
            if task_id in self._supervisors and self._supervisors[task_id].is_running:
                raise RuntimeError(f"Supervisor already running for task {task_id}")

            effective_timeout = self.supervisor_timeout
            if effective_timeout > 0 and check_interval > 0:
                # Keep each supervisor check bounded so periodic monitoring
                # can continue at the configured cadence.
                effective_timeout = min(effective_timeout, max(5, check_interval))

            supervisor = SupervisorThread(
                task_id=task_id,
                prompt=prompt,
                worker_session_id=worker_session_id,
                mcp_server_url=self.mcp_server_url,
                mcp_token=self.mcp_token,
                check_interval=check_interval,
                on_output=on_output,
                timeout=effective_timeout,
                working_dir=working_dir,
                root_workspace_dir=self.root_workspace_dir,
                task_manager=task_manager,
                worker_pool=self,
            )
            self._supervisors[task_id] = supervisor
            supervisor.start()
            return supervisor

    def stop_task(self, task_id: str) -> None:
        """Stop both worker and supervisor for a task."""
        with self._lock:
            if task_id in self._workers:
                self._workers[task_id].stop()
            if task_id in self._supervisors:
                self._supervisors[task_id].stop()

    def stop_worker(self, task_id: str, wait_seconds: float = 5.0) -> None:
        """Stop only the worker for a task and wait briefly for thread exit."""
        worker: Optional[WorkerThread] = None
        with self._lock:
            worker = self._workers.get(task_id)
            if worker:
                worker.stop()
        if not worker:
            return
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while worker.is_running and time.monotonic() < deadline:
            time.sleep(0.05)

    def stop_supervisor(self, task_id: str) -> None:
        """Stop only the supervisor for a task."""
        with self._lock:
            supervisor = self._supervisors.get(task_id)
            if supervisor:
                supervisor.stop()

    def stop_all(self) -> None:
        """Stop all workers and supervisors."""
        with self._lock:
            for w in self._workers.values():
                w.stop()
            for s in self._supervisors.values():
                s.stop()

    def get_worker(self, task_id: str) -> Optional[WorkerThread]:
        return self._workers.get(task_id)

    def get_supervisor(self, task_id: str) -> Optional[SupervisorThread]:
        return self._supervisors.get(task_id)

    def request_supervisor_check(self, task_id: str) -> bool:
        """Ask a supervisor to run a check immediately."""
        sup = self._supervisors.get(task_id)
        if sup:
            sup.request_check()
            return True
        return False

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for w in self._workers.values() if w.is_running) + \
                   sum(1 for s in self._supervisors.values() if s.is_running)

    def status(self) -> dict:
        with self._lock:
            return {
                "workers": {
                    tid: {"running": w.is_running, "session_id": w.session_id}
                    for tid, w in self._workers.items()
                },
                "supervisors": {
                    tid: {"running": s.is_running, "session_id": s.session_id}
                    for tid, s in self._supervisors.items()
                },
            }
