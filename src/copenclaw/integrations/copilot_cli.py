from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from copenclaw.core.logging_config import (
    append_to_file,
    get_copilot_boot_failure_log_path,
    get_activity_log_path,
    get_orchestrator_log_path,
)
from copenclaw.core.mcp_registry import get_user_servers_for_merge

logger = logging.getLogger("copenclaw.copilot_cli")

DEFAULT_TIMEOUT = 7200  # seconds (2 hours)
_UNKNOWN_OPTION_STARTUP_WINDOW_SECONDS = 45.0
_UNKNOWN_OPTION_BURST_LIMIT = 3
_MIN_NO_WARNINGS_FIXED_VERSION = (0, 0, 410)


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants (best-effort).

    On Windows uses taskkill /T (tree kill).  On Unix walks /proc or
    uses os.killpg.  Falls back to killing just the root PID.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            # Try killing the process group first
            try:
                os.killpg(os.getpgid(pid), 9)
            except (OSError, ProcessLookupError):
                os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        # Last resort — kill just the root
        try:
            os.kill(pid, 9)
        except Exception:  # noqa: BLE001
            pass


def _env_get(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return None


def _env_bool(*names: str, default: bool) -> bool:
    value = _env_get(*names)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CopilotLaunchDefaults:
    """Centralized defaults for all Copilot session launches."""

    autopilot: bool


def load_launch_defaults() -> CopilotLaunchDefaults:
    return CopilotLaunchDefaults(
        autopilot=_env_bool(
            "copenclaw_COPILOT_AUTOPILOT_DEFAULT",
            "COPILOT_CLAW_COPILOT_AUTOPILOT_DEFAULT",
            default=True,
        ),
    )


class CopilotCliError(RuntimeError):
    pass


def write_mcp_config(
    target_dir: str,
    mcp_server_url: str,
    mcp_token: Optional[str] = None,
    filename: str = "copilot-mcp-config.json",
    task_id: Optional[str] = None,
    role: Optional[str] = None,
) -> str:
    """Write an MCP config JSON file into *target_dir* and return the absolute path.

    This is a module-level helper so both CopilotCli and worker/supervisor
    code can write a config into any directory they control.

    If *task_id* and *role* are provided, they are appended as query
    parameters to the URL so the server can identify which task a
    tool call belongs to and log it to the per-task event stream.
    """
    os.makedirs(target_dir, exist_ok=True)

    # Build URL with optional task routing query params
    url = mcp_server_url
    if task_id:
        sep = "&" if "?" in url else "?"
        url += f"{sep}task_id={task_id}"
        if role:
            url += f"&role={role}"

    config: dict = {
        "mcpServers": {
            "copenclaw": {
                "type": "http",
                "url": url,
                "tools": ["*"],
            }
        }
    }
    if mcp_token:
        config["mcpServers"]["copenclaw"]["headers"] = {
            "x-mcp-token": mcp_token,
        }

    # Merge user-installed MCP servers from ~/.copilot/mcp-config.json
    # so that workers/supervisors have access to the same tools as the brain
    try:
        user_servers = get_user_servers_for_merge()
        if user_servers:
            config["mcpServers"].update(user_servers)
            logger.debug("Merged %d user MCP servers into task config", len(user_servers))
    except Exception:  # noqa: BLE001
        logger.debug("Could not merge user MCP servers (non-fatal)", exc_info=True)

    config_path = os.path.join(target_dir, filename)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    abs_path = os.path.abspath(config_path)
    logger.debug("Wrote MCP config: %s (task_id=%s, role=%s)", abs_path, task_id, role)
    return abs_path


class CopilotCli:
    """Adapter that invokes Copilot CLI for each prompt.

    System instructions live in .github/copilot-instructions.md in the
    workspace directory — Copilot CLI reads that file automatically.
    Each ``run_prompt`` call passes only the user's message via ``-p``,
    keeping prompt and instructions cleanly separated.

    Output is streamed line-by-line to the logger and to per-task log
    files so you can watch in real-time.
    """
    def __init__(
        self,
        executable: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        mcp_server_url: Optional[str] = None,
        mcp_token: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        mcp_config_path: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        subcommand: Optional[str] = None,
        autopilot: Optional[bool] = None,
        execution_backend: Optional[str] = None,
        allow_cli_fallback: Optional[bool] = None,
        yolo: bool = True,
    ) -> None:
        defaults = load_launch_defaults()
        self.executable = executable or os.getenv("COPILOT_CLI_PATH", "copilot")
        self.workspace_dir = workspace_dir or os.getenv("copenclaw_WORKSPACE_DIR")
        self.timeout = timeout
        self.mcp_server_url = mcp_server_url
        self.mcp_token = mcp_token
        self.add_dirs: list[str] = add_dirs or []
        self.autopilot = defaults.autopilot if autopilot is None else autopilot
        if execution_backend and execution_backend.strip().lower() != "cli":
            logger.info(
                "Ignoring deprecated execution_backend=%r; CopilotCli runtime is CLI-only.",
                execution_backend,
            )
        if allow_cli_fallback is False:
            logger.info(
                "Ignoring deprecated allow_cli_fallback=False; CopilotCli runtime is already CLI-only."
            )
        self.yolo = yolo
        self._silent_mode = True

        self._session_id: Optional[str] = None
        self._resume_session_id: Optional[str] = resume_session_id
        self._mcp_config_path: Optional[str] = mcp_config_path
        raw_subcommand = subcommand or os.getenv("COPILOT_CLI_SUBCOMMAND")
        self._subcommand: Optional[str] = self._normalize_subcommand(raw_subcommand)
        self._initialized = False
        self._version_logged = False
        self._cached_version: Optional[str] = None
        self._last_discovered_session_id: Optional[str] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def resume_session_id(self) -> Optional[str]:
        return self._resume_session_id

    @resume_session_id.setter
    def resume_session_id(self, value: Optional[str]) -> None:
        self._resume_session_id = value

    # ── internal helpers ──────────────────────────────────────

    def _resolve_executable(self) -> str:
        path = shutil.which(self.executable)
        if not path:
            raise CopilotCliError("copilot CLI not found on PATH")
        if sys.platform == "win32":
            normalized = os.path.normcase(path)
            root, ext = os.path.splitext(normalized)
            if ext in {".cmd", ".bat", ".ps1"}:
                exe_candidate = f"{root}.exe"
                if os.path.exists(exe_candidate):
                    return os.path.normcase(exe_candidate)
            return normalized
        return path

    @staticmethod
    def _build_executable_cmd(executable: str) -> list[str]:
        if sys.platform == "win32" and executable.lower().endswith(".ps1"):
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            return [os.path.normcase(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", executable]
        return [executable]

    @staticmethod
    def _normalize_subcommand(raw: Optional[str]) -> Optional[str]:
        if raw is None:
            return None
        candidate = raw.strip()
        if not candidate:
            return None
        if candidate.startswith("-") or any(ch.isspace() for ch in candidate):
            logger.warning("Ignoring invalid Copilot subcommand override: %r", raw)
            return None
        return candidate

    def _ensure_mcp_config(self) -> str:
        """Write MCP config into the workspace directory (or .data/) and return abs path.

        The config is written into the workspace directory itself so that
        the ``@path`` reference always resolves correctly regardless of cwd.
        """
        if self._mcp_config_path and os.path.exists(self._mcp_config_path):
            return self._mcp_config_path
        if self._mcp_config_path and not self.mcp_server_url:
            return self._mcp_config_path

        url = self.mcp_server_url or "http://127.0.0.1:18790/mcp"
        target_dir = self.workspace_dir or os.getenv("copenclaw_DATA_DIR", ".data")
        self._mcp_config_path = write_mcp_config(
            target_dir=target_dir,
            mcp_server_url=url,
            mcp_token=self.mcp_token,
        )
        logger.info("MCP config ready: %s", self._mcp_config_path)
        return self._mcp_config_path

    def _base_cmd(
        self,
        resume_id: Optional[str] = None,
        autopilot: Optional[bool] = None,
    ) -> list[str]:
        """Build the base command with non-interactive flags.

        If *resume_id* is provided, ``--resume <id>`` is added so Copilot CLI
        restores the conversation from a previous session instead of starting
        fresh.  When no *resume_id* is given but ``self._resume_session_id``
        is set, that value is used automatically.
        """
        exe = self._resolve_executable()
        cmd = self._build_executable_cmd(exe)
        if self._subcommand:
            cmd.append(self._subcommand)

        # Resume a previous session if we have a session ID
        effective_resume = resume_id or self._resume_session_id
        if effective_resume:
            cmd.extend(["--resume", effective_resume])

        # MCP config — written into the workspace directory so @path resolves
        if self.mcp_server_url or self._mcp_config_path:
            mcp_path = self._ensure_mcp_config()
            cmd.extend(["--additional-mcp-config", f"@{mcp_path}"])

        # Grant access to additional directories so Copilot can use
        # its built-in file tools (read/write/edit) instead of shell commands
        for d in self.add_dirs:
            abs_d = os.path.abspath(d)
            if os.path.isdir(abs_d):
                cmd.extend(["--add-dir", abs_d])

        # Non-interactive autonomous flags
        flags = ["--no-ask-user"]
        if self._silent_mode:
            flags.append("-s")  # silent (clean output only)
        if self.yolo:
            # --yolo enables all permissions (tools, paths, URLs) at once
            flags.insert(0, "--yolo")
        effective_autopilot = self.autopilot if autopilot is None else autopilot
        if effective_autopilot:
            flags.insert(0, "--autopilot")
        cmd.extend(flags)

        return cmd

    def build_launch_command(
        self,
        resume_id: Optional[str] = None,
        require_subprocess: bool = False,
    ) -> list[str]:
        """Build a launch command for CLI subprocess execution."""
        return self._base_cmd(resume_id=resume_id)

    @staticmethod
    def _should_retry_with_chat(output: str) -> bool:
        """Detect CLI errors that indicate a missing 'chat' subcommand."""
        lowered = output.lower()
        return (
            "too many arguments" in lowered
            or "expected 0 arguments" in lowered
            or "unexpected extra argument" in lowered
            or "no such option" in lowered
            or "unknown option" in lowered
        )

    @staticmethod
    def _is_unknown_option_error(output: str) -> bool:
        lowered = output.lower()
        return (
            "no such option" in lowered
            or "unknown option" in lowered
            or "unexpected argument" in lowered
            or "unrecognized option" in lowered
        )

    @classmethod
    def _should_retry_without_silent(cls, output: str) -> bool:
        return cls._is_no_warnings_unknown_option(output)

    @staticmethod
    def _is_no_warnings_unknown_option(text: str) -> bool:
        lowered = text.lower()
        return "--no-warnings" in lowered and "unknown option" in lowered

    @classmethod
    def _should_retry_with_clean_session(cls, output: str, *, burst_detected: bool) -> bool:
        lowered = output.lower()
        if not cls._is_no_warnings_unknown_option(lowered):
            return False
        if burst_detected:
            return True
        return "try 'copilot --help'" in lowered or "unknown option" in lowered

    @staticmethod
    def _sanitize_cmd_for_log(cmd: list[str]) -> list[str]:
        sanitized: list[str] = []
        mask_next = False
        for part in cmd:
            if mask_next:
                sanitized.append("<prompt>")
                mask_next = False
                continue
            sanitized.append(part)
            if part in {"-p", "--prompt"}:
                mask_next = True
        return sanitized

    @staticmethod
    def _extract_semver(raw: str) -> Optional[tuple[int, int, int]]:
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    def _log_cli_runtime_metadata(self, log_prefix: str) -> None:
        if self._version_logged:
            return
        self._version_logged = True
        try:
            self._cached_version = self.version()
            logger.info("%s | Copilot CLI version: %s", log_prefix, self._cached_version)
            parsed = self._extract_semver(self._cached_version)
            if parsed is not None and parsed < _MIN_NO_WARNINGS_FIXED_VERSION:
                logger.warning(
                    "%s | Copilot CLI version %s may include known '--no-warnings' issues",
                    log_prefix,
                    self._cached_version,
                )
        except CopilotCliError as exc:
            logger.warning("%s | Unable to determine Copilot CLI version: %s", log_prefix, exc)

    def _orchestrator_log_path(self) -> str:
        return get_orchestrator_log_path()

    def _activity_log_path(self) -> str:
        return get_activity_log_path()

    def _log_line(self, line: str, prefix: str = "ORCHESTRATOR") -> None:
        """Log a single line to both the Python logger and disk log files."""
        clean = line.rstrip()
        logger.info("%s | %s", prefix, clean)
        # Write to centralized orchestrator log
        try:
            with open(self._orchestrator_log_path(), "a", encoding="utf-8") as f:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
        except Exception:  # noqa: BLE001
            pass
        # Also append to unified activity log
        append_to_file(self._activity_log_path(), f"[{prefix}] {clean}")

    def _make_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _discover_latest_session_id(self) -> Optional[str]:
        """Try to discover the most-recently-modified session from Copilot CLI's data dir.

        Copilot CLI stores sessions under ``~/.copilot/session-state/``.
        Each session is a directory whose name is the session ID.  We pick
        the one with the most recent modification time.
        """
        config_dir = os.path.expanduser("~/.copilot")
        # Copilot CLI uses "session-state" for session storage
        sessions_dir = os.path.join(config_dir, "session-state")
        if not os.path.isdir(sessions_dir):
            # Fallback: older versions may use "sessions"
            sessions_dir = os.path.join(config_dir, "sessions")
        if not os.path.isdir(sessions_dir):
            logger.debug("No Copilot sessions dir found at %s", sessions_dir)
            return None
        try:
            entries = [e for e in os.scandir(sessions_dir) if e.is_dir()]
            if not entries:
                return None
            latest = max(entries, key=lambda e: e.stat().st_mtime)
            session_id = latest.name
            logger.info("Discovered latest Copilot CLI session: %s", session_id)
            return session_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to discover session ID: %s", exc)
            return None

    @staticmethod
    def _session_summary(session_dir: str) -> str:
        """Read session summary text from workspace.yaml (best-effort)."""
        workspace_yaml = os.path.join(session_dir, "workspace.yaml")
        if not os.path.isfile(workspace_yaml):
            return ""
        try:
            with open(workspace_yaml, "r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.strip()
                    if line.startswith("summary:"):
                        return line.split(":", 1)[1].strip().lower()
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def session_is_task_role(self, session_id: str) -> bool:
        """Return True if session summary indicates worker/supervisor task role."""
        config_dir = os.path.expanduser("~/.copilot")
        session_dir = os.path.join(config_dir, "session-state", session_id)
        if not os.path.isdir(session_dir):
            session_dir = os.path.join(config_dir, "sessions", session_id)
        summary = self._session_summary(session_dir)
        return summary.startswith("you are worker for task") or summary.startswith("you are supervisor for task")

    # ── Session ownership ────────────────────────────────────
    _OWNERSHIP_MARKER = ".copenclaw-owned"

    @staticmethod
    def _session_dir_for_id(session_id: str) -> Optional[str]:
        """Return the on-disk path for a Copilot CLI session, or None."""
        config_dir = os.path.expanduser("~/.copilot")
        for subdir in ("session-state", "sessions"):
            candidate = os.path.join(config_dir, subdir, session_id)
            if os.path.isdir(candidate):
                return candidate
        return None

    def mark_session_owned(self, session_id: str) -> None:
        """Write an ownership marker into a session dir so we can identify it later."""
        session_dir = self._session_dir_for_id(session_id)
        if not session_dir:
            logger.debug("Cannot mark session %s — dir not found", session_id)
            return
        marker = os.path.join(session_dir, self._OWNERSHIP_MARKER)
        try:
            Path(marker).write_text("copenclaw\n", encoding="utf-8")
            logger.info("Marked session %s as copenclaw-owned", session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to mark session %s: %s", session_id, exc)

    @classmethod
    def _is_owned_session(cls, session_dir: str) -> bool:
        """Return True if the session dir contains our ownership marker."""
        return os.path.isfile(os.path.join(session_dir, cls._OWNERSHIP_MARKER))

    @staticmethod
    def _sessions_dir() -> Optional[str]:
        """Return the path to the Copilot CLI sessions directory, or None."""
        config_dir = os.path.expanduser("~/.copilot")
        for subdir in ("session-state", "sessions"):
            candidate = os.path.join(config_dir, subdir)
            if os.path.isdir(candidate):
                return candidate
        return None

    def snapshot_session_dirs(self) -> set[str]:
        """Return the current set of session directory names.

        Call this *before* launching a CLI process.  After the process
        completes, call :meth:`discover_new_session` with this snapshot
        to identify the session the process created.
        """
        sessions_dir = self._sessions_dir()
        if not sessions_dir:
            return set()
        try:
            return {e.name for e in os.scandir(sessions_dir) if e.is_dir()}
        except OSError:
            return set()

    def discover_new_session(self, before_snapshot: set[str]) -> Optional[str]:
        """Find a session created since *before_snapshot* was taken.

        Compares the current session directories against the snapshot and
        returns the newest NEW directory (one that didn't exist before).
        This is immune to mtime races with the user's terminal sessions.
        If no new session is found, falls back to the newest owned session.
        """
        sessions_dir = self._sessions_dir()
        if not sessions_dir:
            return None
        try:
            current = {
                e.name: e.stat().st_mtime
                for e in os.scandir(sessions_dir)
                if e.is_dir()
            }
        except OSError:
            return None

        new_ids = set(current.keys()) - before_snapshot
        if new_ids:
            # Pick the newest of the new sessions (usually just one)
            best = max(new_ids, key=lambda sid: current[sid])
            logger.info("Discovered NEW session (snapshot diff): %s", best)
            self.mark_session_owned(best)
            return best

        # Fallback: no new directory found (e.g. CLI reused an existing
        # session).  Fall back to newest owned, non-task session.
        logger.debug("No new session dirs found; falling back to newest owned session")
        return self._discover_latest_owned_session()

    def _discover_latest_owned_session(self) -> Optional[str]:
        """Discover the newest COpenClaw-owned, non-task session."""
        sessions_dir = self._sessions_dir()
        if not sessions_dir:
            return None
        try:
            entries = sorted(
                [e for e in os.scandir(sessions_dir) if e.is_dir()],
                key=lambda e: e.stat().st_mtime,
                reverse=True,
            )
            for entry in entries:
                if not self._is_owned_session(entry.path):
                    continue
                summary = self._session_summary(entry.path)
                if summary.startswith("you are worker for task") or summary.startswith("you are supervisor for task"):
                    continue
                logger.info("Discovered latest owned session: %s", entry.name)
                return entry.name
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to discover owned session: %s", exc)
        return None

    # ── public API ────────────────────────────────────────────

    def _log_prompt_header(self, prompt: str, log_prefix: str) -> None:
        logger.info("%s ← %s", log_prefix, prompt[:300])
        try:
            with open(self._orchestrator_log_path(), "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"USER: {prompt}\n")
                f.write(f"{'='*60}\n")
        except Exception:  # noqa: BLE001
            pass

    def _record_boot_failure(self, error_text: str) -> None:
        if not error_text:
            return
        append_to_file(
            get_copilot_boot_failure_log_path(),
            f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {error_text}",
        )

    @staticmethod
    def _prompt_needs_spill(
        prompt: str,
        base_cmd_len: int,
        headroom: int = 7000,
    ) -> bool:
        """Decide whether to spill the prompt to a temp file instead of ``-p``.

        Two cases require spilling:

        1. **Length** — a prompt long enough to push the command line over the
           Windows CreateProcessW limit (~8 191 chars).

        2. **Multi-line** — any prompt containing a newline.  On Windows the
           Copilot CLI is launched via a ``.cmd``/``.bat`` shim (or ``.ps1``
           through PowerShell), and the batch/PowerShell arg parser truncates
           a multi-line ``-p`` value at the first newline.  This silently
           dropped everything after line 1 of scheduled-task prompts.  Reading
           the prompt from a file preserves the full multi-line content.
        """
        if "\n" in prompt or "\r" in prompt:
            return True
        return base_cmd_len + len(prompt) + 10 > headroom

    def _run_prompt_cli(
        self,
        prompt: str,
        *,
        model: Optional[str],
        cwd: Optional[str],
        log_prefix: str,
        resume_id: Optional[str],
        allow_retry: bool,
        autopilot: Optional[bool],
        on_line: Optional[Callable[[str], Optional[bool]]],
    ) -> str:
        self._log_cli_runtime_metadata(log_prefix)
        cmd = self._base_cmd(resume_id=resume_id, autopilot=autopilot)
        # On Windows, CreateProcessW enforces a hard 8 191-character limit
        # on the entire command-line string.  When the prompt is short
        # (and single-line) we pass it inline via `-p` (simplest, most
        # reliable).  When it would push the command line over ~7 000 chars
        # OR contains a newline we write the prompt to a temp file and pass
        # `-p` with a short file-read instruction so Copilot CLI still enters
        # prompt-mode and exits cleanly after responding.
        #
        # Multi-line prompts MUST be spilled: the Windows `.cmd`/`.bat`/`.ps1`
        # launcher truncates a multi-line `-p` argument at the first newline,
        # which silently dropped everything after line 1 of scheduled prompts.
        #
        # We do NOT use bare stdin piping (no `-p`) because Copilot CLI
        # requires `-p` to enter non-interactive prompt-mode; without it
        # the process enters the TUI and never exits.
        #
        # Ref: Windows CreateProcessW limit
        #   https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
        _CMDLINE_HEADROOM = 7000  # conservative; leaves room for base cmd + flags
        prompt_file: Optional[str] = None
        base_cmd_len = sum(len(a) + 3 for a in cmd)  # rough: arg + quotes + space
        if self._prompt_needs_spill(prompt, base_cmd_len, _CMDLINE_HEADROOM):
            # Prompt is too long or multi-line for the command line — spill to temp file.
            prompt_file = os.path.join(
                tempfile.gettempdir(),
                f"copenclaw-prompt-{os.getpid()}-{id(prompt):x}.txt",
            )
            with open(prompt_file, "w", encoding="utf-8") as pf:
                pf.write(prompt)
            # Tell the model to read from the file.  The `-p` value is
            # kept short so the command line stays well under the limit.
            cmd.extend(["-p", f"Read and respond to the user prompt in: {prompt_file}"])
            logger.info(
                "%s | Prompt spilled to file (%d chars, multiline=%s); path=%s",
                log_prefix, len(prompt), "\n" in prompt or "\r" in prompt, prompt_file,
            )
        else:
            cmd.extend(["-p", prompt])
        if model:
            cmd.extend(["--model", model])

        effective_cwd = cwd or self.workspace_dir

        # Snapshot session dirs BEFORE launching so we can detect the new
        # session created by this CLI invocation (immune to mtime races).
        is_new_session = not (resume_id or self._resume_session_id)
        pre_launch_snapshot = self.snapshot_session_dirs() if is_new_session else set()

        logger.info(
            "%s | Launching Copilot CLI (cwd=%s, resume=%s, prompt_len=%d, cmd=%s)",
            log_prefix,
            effective_cwd,
            bool(resume_id or self._resume_session_id),
            len(prompt),
            self._sanitize_cmd_for_log(cmd),
        )
        try:
            process = subprocess.Popen(
                cmd,
                cwd=effective_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._make_env(),
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except FileNotFoundError as exc:
            raise CopilotCliError(f"copilot CLI not found: {exc}") from exc

        output_lines: list[str] = []
        early_stopped = False
        burst_detected = False
        timed_out = False
        startup_deadline = time.monotonic() + _UNKNOWN_OPTION_STARTUP_WINDOW_SECONDS
        unknown_option_hits = 0
        timeout_timer: Optional[threading.Timer] = None
        if self.timeout and self.timeout > 0:
            def _on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                logger.warning("%s | Timeout after %ds — killing process tree", log_prefix, self.timeout)
                _kill_process_tree(process.pid)
                # Close stdout to unblock the read loop — process.kill()
                # alone doesn't help if child processes hold the pipe open.
                try:
                    if process.stdout and not process.stdout.closed:
                        process.stdout.close()
                except Exception:  # noqa: BLE001
                    pass

            timeout_timer = threading.Timer(self.timeout, _on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if timed_out:
                    break
                output_lines.append(line)
                self._log_line(line, prefix=log_prefix)
                clean_line = line.rstrip("\n\r")
                if self._is_no_warnings_unknown_option(clean_line):
                    if time.monotonic() <= startup_deadline:
                        unknown_option_hits += 1
                    if unknown_option_hits >= _UNKNOWN_OPTION_BURST_LIMIT:
                        burst_detected = True
                        early_stopped = True
                        logger.warning(
                            "%s | Repeated '--no-warnings' unknown-option failures detected; terminating process",
                            log_prefix,
                        )
                        process.terminate()
                        break
                if on_line:
                    should_stop = on_line(clean_line)
                    if should_stop:
                        early_stopped = True
                        logger.info("%s | Early stop requested; terminating Copilot CLI process", log_prefix)
                        process.terminate()
                        break
        except ValueError:
            # stdout was closed by the timeout handler — expected
            pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("%s | Copilot CLI process did not exit after kill()", log_prefix)
        except CopilotCliError:
            raise
        except Exception as exc:
            process.kill()
            raise CopilotCliError(f"copilot CLI error: {exc}") from exc
        finally:
            if timeout_timer:
                timeout_timer.cancel()
            # Clean up temp prompt file if we spilled to disk
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

        # Ensure returncode is populated — without wait(), it stays None
        # even after stdout is fully consumed.
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            logger.warning("%s | Process did not exit after stdout closed; killing", log_prefix)
            try:
                _kill_process_tree(process.pid)
            except (AttributeError, OSError):
                process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        output = "".join(output_lines).strip()
        if timed_out:
            raise CopilotCliError(f"copilot CLI timed out after {self.timeout}s")
        if allow_retry and self._should_retry_without_silent(output):
            if self._silent_mode:
                logger.warning("copilot CLI rejected silent mode; retrying without '-s'")
                self._silent_mode = False
                return self._run_prompt_cli(
                    prompt,
                    model=model,
                    cwd=cwd,
                    log_prefix=log_prefix,
                    resume_id=resume_id,
                    allow_retry=True,
                    autopilot=autopilot,
                    on_line=on_line,
                )
            if self._should_retry_with_clean_session(output, burst_detected=burst_detected):
                logger.warning(
                    "%s | Retrying with clean session after '--no-warnings' unknown-option failure",
                    log_prefix,
                )
                self._resume_session_id = None
                self._session_id = None
                return self._run_prompt_cli(
                    prompt,
                    model=model,
                    cwd=cwd,
                    log_prefix=log_prefix,
                    resume_id=None,
                    allow_retry=False,
                    autopilot=autopilot,
                    on_line=on_line,
                )
        if process.returncode != 0 and (not early_stopped or burst_detected):
            if allow_retry and not self._subcommand and self._should_retry_with_chat(output):
                logger.warning("copilot CLI rejected args; retrying with 'chat' subcommand")
                self._subcommand = "chat"
                return self._run_prompt_cli(
                    prompt,
                    model=model,
                    cwd=cwd,
                    log_prefix=log_prefix,
                    resume_id=resume_id,
                    allow_retry=False,
                    autopilot=autopilot,
                    on_line=on_line,
                )
            if not output:
                raise CopilotCliError(f"copilot CLI failed with exit code {process.returncode}")

        logger.info("%s → complete (%d chars)", log_prefix, len(output))
        self._initialized = True

        # If this was a new session (no --resume), discover which session
        # was created by comparing against the pre-launch snapshot.
        if is_new_session and pre_launch_snapshot:
            new_sid = self.discover_new_session(pre_launch_snapshot)
            if new_sid:
                self._resume_session_id = new_sid
                self._session_id = new_sid
                self._last_discovered_session_id = new_sid
                logger.info("%s | Captured new session ID via snapshot: %s", log_prefix, new_sid)

        return output

    def create_session(self, context: str = "", allow_retry: bool = True) -> str:
        """Bootstrap a brain session (validates CLI works). Returns the response.

        Sends a greeting with optional context (e.g. README.md contents);
        system instructions come from .github/copilot-instructions.md in
        the workspace directory.
        """
        logger.info("Creating Copilot CLI brain session...")
        if context:
            boot_prompt = (
                "Hello! You are coming online. Here is the current workspace README.md "
                "so you understand what projects and tasks have been done:\n\n"
                f"{context}\n\n"
                "Please confirm you are online and ready."
            )
        else:
            boot_prompt = "Hello! Please confirm you are online and ready."

        try:
            output = self.run_prompt(
                boot_prompt,
                cwd=self.workspace_dir,
                log_prefix="BOOTSTRAP",
                allow_retry=allow_retry,
            )
            logger.info("Brain session created. Response: %s", output[:200])
            return output
        except CopilotCliError as exc:
            self._record_boot_failure(str(exc))
            logger.error("Session creation failed: %s", exc)
            raise

    def run_prompt(
        self,
        prompt: str,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        log_prefix: str = "ORCHESTRATOR",
        resume_id: Optional[str] = None,
        allow_retry: bool = True,
        execution_backend: Optional[str] = None,
        autopilot: Optional[bool] = None,
        on_line: Optional[Callable[[str], Optional[bool]]] = None,
    ) -> str:
        """Send a user prompt to Copilot CLI with streaming output.

        If *resume_id* is given (or ``self._resume_session_id`` is set),
        the session is resumed via ``--resume`` so Copilot CLI maintains
        its own conversation context natively — no need to prepend history.

        System instructions come from .github/copilot-instructions.md in
        the workspace dir.  Only the user's actual message is passed
        via ``-p``.  Output is streamed line-by-line.
        """
        self._log_prompt_header(prompt, log_prefix)
        if execution_backend and execution_backend.strip().lower() != "cli":
            logger.info(
                "%s | Ignoring deprecated execution_backend=%r; CopilotCli runtime is CLI-only.",
                log_prefix,
                execution_backend,
            )
        return self._run_prompt_cli(
            prompt,
            model=model,
            cwd=cwd,
            log_prefix=log_prefix,
            resume_id=resume_id,
            allow_retry=allow_retry,
            autopilot=autopilot,
            on_line=on_line,
        )

    def harvest_current_metrics(self) -> "Optional[SessionMetrics]":
        """Harvest telemetry from the current session's events.jsonl.

        Uses the fast path (reads only the tail of the file) for real-time
        budget checks.  Returns None if no session ID is known.
        """
        from copenclaw.core.telemetry import harvest_session_metrics_fast

        sid = self._session_id or self._resume_session_id
        if not sid:
            return None
        return harvest_session_metrics_fast(sid)

    def version(self) -> str:
        """Return copilot CLI version string."""
        exe = self._resolve_executable()
        try:
            result = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except Exception as exc:
            raise CopilotCliError(f"failed to get version: {exc}") from exc
