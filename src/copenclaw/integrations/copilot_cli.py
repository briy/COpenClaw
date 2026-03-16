from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from copenclaw.core.logging_config import (
    append_to_file,
    get_copilot_boot_failure_log_path,
    get_activity_log_path,
    get_orchestrator_log_path,
)
from copenclaw.core.mcp_registry import get_user_servers_for_merge

logger = logging.getLogger("copenclaw.copilot_cli")

DEFAULT_TIMEOUT = 7200  # seconds (2 hours)
_DEFAULT_EXECUTION_BACKEND = "api"


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
    execution_backend: Literal["api", "cli"]
    allow_cli_fallback: bool


def load_launch_defaults() -> CopilotLaunchDefaults:
    backend_raw = (
        _env_get("copenclaw_COPILOT_EXECUTION_BACKEND", "COPILOT_CLAW_COPILOT_EXECUTION_BACKEND")
        or _DEFAULT_EXECUTION_BACKEND
    ).strip().lower()
    backend: Literal["api", "cli"] = "cli" if backend_raw == "cli" else "api"
    return CopilotLaunchDefaults(
        autopilot=_env_bool(
            "copenclaw_COPILOT_AUTOPILOT_DEFAULT",
            "COPILOT_CLAW_COPILOT_AUTOPILOT_DEFAULT",
            default=True,
        ),
        execution_backend=backend,
        allow_cli_fallback=_env_bool(
            "copenclaw_COPILOT_ALLOW_CLI_FALLBACK",
            "COPILOT_CLAW_COPILOT_ALLOW_CLI_FALLBACK",
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
    # so that workers/supervisors have access to the same tools as the brain.
    # Skip if COPENCLAW_SKIP_USER_MCPS is set (reduces API payload size).
    if not os.environ.get("COPENCLAW_SKIP_USER_MCPS"):
        try:
            user_servers = get_user_servers_for_merge()
            if user_servers:
                config["mcpServers"].update(user_servers)
                logger.debug("Merged %d user MCP servers into task config", len(user_servers))
        except Exception:  # noqa: BLE001
            logger.debug("Could not merge user MCP servers (non-fatal)", exc_info=True)
    else:
        logger.info("Skipping user MCP server merge (COPENCLAW_SKIP_USER_MCPS is set)")

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
        execution_backend: Optional[Literal["api", "cli"]] = None,
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
        self.execution_backend: Literal["api", "cli"] = execution_backend or defaults.execution_backend
        self.allow_cli_fallback = defaults.allow_cli_fallback if allow_cli_fallback is None else allow_cli_fallback
        self.yolo = yolo

        self._session_id: Optional[str] = None
        self._resume_session_id: Optional[str] = resume_session_id
        self._mcp_config_path: Optional[str] = mcp_config_path
        self._subcommand: Optional[str] = subcommand or os.getenv("COPILOT_CLI_SUBCOMMAND")
        self._initialized = False

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
            suffix = Path(path).suffix.lower()
            if suffix in {".bat", ".cmd", ".ps1"}:
                exe_name = f"{Path(self.executable).stem}.exe"
                exe_path = shutil.which(exe_name)
                if exe_path:
                    return exe_path
        return path

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
        cmd = [exe]
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
        flags = [
            "--no-ask-user",
            "-s",  # silent (clean output only)
        ]
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
        """Build a launch command, using explicit CLI fallback when required."""
        if require_subprocess and self.execution_backend == "api":
            if not self.allow_cli_fallback:
                raise CopilotCliError(
                    "Copilot API backend selected, but subprocess launch requires explicit CLI fallback"
                )
            logger.warning(
                "Copilot API backend selected; using explicit CLI fallback for subprocess launch"
            )
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
    def _should_retry_without_autopilot(cls, output: str) -> bool:
        lowered = output.lower()
        return "autopilot" in lowered and cls._is_unknown_option_error(lowered)

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

    def _discover_latest_non_task_session_id(self) -> Optional[str]:
        """Discover latest session, excluding worker/supervisor task sessions."""
        config_dir = os.path.expanduser("~/.copilot")
        sessions_dir = os.path.join(config_dir, "session-state")
        if not os.path.isdir(sessions_dir):
            sessions_dir = os.path.join(config_dir, "sessions")
        if not os.path.isdir(sessions_dir):
            return None
        try:
            entries = sorted(
                [e for e in os.scandir(sessions_dir) if e.is_dir()],
                key=lambda e: e.stat().st_mtime,
                reverse=True,
            )
            for entry in entries:
                summary = self._session_summary(entry.path)
                if summary.startswith("you are worker for task") or summary.startswith("you are supervisor for task"):
                    continue
                logger.info("Discovered latest non-task Copilot CLI session: %s", entry.name)
                return entry.name
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to discover non-task session ID: %s", exc)
        return None

    def get_session_size_kb(self, session_id: Optional[str] = None) -> int:
        """Return the size in KB of a CLI session directory on disk."""
        sid = session_id or self._resume_session_id
        if not sid:
            return 0
        config_dir = os.path.expanduser("~/.copilot")
        session_dir = os.path.join(config_dir, "session-state", sid)
        if not os.path.isdir(session_dir):
            return 0
        total = 0
        for dirpath, _dirnames, filenames in os.walk(session_dir):
            for f in filenames:
                total += os.path.getsize(os.path.join(dirpath, f))
        return total // 1024

    # ── Token-based telemetry via /context and /usage ──────────────

    @staticmethod
    def _parse_token_value(text: str) -> int:
        """Convert '31.4k' or '9.7m' to integer token count."""
        m = re.match(r"(\d+\.?\d*)\s*(k|m)", text.strip(), re.IGNORECASE)
        if not m:
            return 0
        value = float(m.group(1))
        suffix = m.group(2).lower()
        if suffix == "k":
            return int(value * 1_000)
        return int(value * 1_000_000)

    def get_session_metrics(
        self,
        session_id: Optional[str] = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Probe a session with /context and /usage, return structured metrics.

        Spawns a short-lived interactive Copilot CLI process that resumes the
        given session, sends ``/context`` and ``/usage`` slash commands via
        stdin, parses the text output, and terminates.  The slash commands are
        read-only and do not modify session history.

        Returns a dict with ``context`` and ``usage`` sub-dicts (either may be
        empty if parsing fails).  Always includes ``disk_size_kb``.
        """
        sid = session_id or self._resume_session_id
        result: dict[str, Any] = {
            "session_id": sid or "",
            "disk_size_kb": self.get_session_size_kb(sid),
            "context": {},
            "usage": {},
        }
        if not sid:
            return result

        exe = self._resolve_executable()
        cmd = [exe, "--resume", sid, "--no-alt-screen"]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workspace_dir or os.getcwd(),
                env=self._make_env(),
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except Exception as exc:
            logger.warning("get_session_metrics: failed to spawn CLI: %s", exc)
            return result

        try:
            # Wait for interactive session to initialize
            time.sleep(4)

            assert proc.stdin is not None
            proc.stdin.write("/context\n")
            proc.stdin.flush()
            time.sleep(3)

            proc.stdin.write("/usage\n")
            proc.stdin.flush()
            time.sleep(3)

            proc.stdin.write("/exit\n")
            proc.stdin.flush()

            # Read all available output
            try:
                raw_output, _ = proc.communicate(timeout=timeout - 10)
            except subprocess.TimeoutExpired:
                proc.kill()
                raw_output, _ = proc.communicate(timeout=5)

            result["context"] = self._parse_context_output(raw_output)
            result["usage"] = self._parse_usage_output(raw_output)

        except Exception as exc:
            logger.warning("get_session_metrics: error during probe: %s", exc)
        finally:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        return result

    def _parse_context_output(self, raw: str) -> dict[str, Any]:
        """Extract token window metrics from /context output."""
        ctx: dict[str, Any] = {}

        # Summary line: "claude-opus-4.6 · 82k/200k tokens (41%)"
        m = re.search(
            r"([\w.-]+)\s+·\s+(\d+\.?\d*)(k|m)/(\d+\.?\d*)(k|m)\s+tokens\s+\((\d+)%\)",
            raw,
        )
        if m:
            ctx["model"] = m.group(1)
            ctx["total_tokens"] = self._parse_token_value(m.group(2) + m.group(3))
            ctx["max_tokens"] = self._parse_token_value(m.group(4) + m.group(5))
            ctx["pct_used"] = int(m.group(6))

        # Breakdown lines
        breakdown_pattern = r"(System/Tools|Messages|Free Space|Buffer):\s+(\d+\.?\d*)(k|m)\s+\((\d+)%\)"
        field_map = {
            "System/Tools": "system_tokens",
            "Messages": "message_tokens",
            "Free Space": "free_tokens",
            "Buffer": "buffer_tokens",
        }
        for match in re.finditer(breakdown_pattern, raw):
            key = field_map.get(match.group(1))
            if key:
                ctx[key] = self._parse_token_value(match.group(2) + match.group(3))
                ctx[key + "_pct"] = int(match.group(4))

        return ctx

    def _parse_usage_output(self, raw: str) -> dict[str, Any]:
        """Extract cumulative usage metrics from /usage output."""
        usage: dict[str, Any] = {}

        # "Total usage est:        63 Premium requests"
        m = re.search(r"Total usage est:\s+(\d+)\s+Premium", raw)
        if m:
            usage["premium_requests"] = int(m.group(1))

        # "API time spent:         27m 53s"
        m = re.search(r"API time spent:\s+(.+)", raw)
        if m:
            usage["api_time"] = m.group(1).strip()

        # "Total session time:     40h 59m 31s"
        m = re.search(r"Total session time:\s+(.+)", raw)
        if m:
            usage["session_time"] = m.group(1).strip()

        # "Total code changes:     +247 -139"
        m = re.search(r"Total code changes:\s+\+(\d+)\s+-(\d+)", raw)
        if m:
            usage["code_additions"] = int(m.group(1))
            usage["code_deletions"] = int(m.group(2))

        # Per-model breakdown: "claude-opus-4.6  9.7m in, 38.1k out, 9.1m cached (Est. 63 Premium requests)"
        models: dict[str, dict] = {}
        model_pattern = (
            r"([\w.-]+)\s+(\d+\.?\d*)(k|m)\s+in,\s+(\d+\.?\d*)(k|m)\s+out,"
            r"\s+(\d+\.?\d*)(k|m)\s+cached\s+\(Est\.\s+(\d+)\s+Premium"
        )
        for match in re.finditer(model_pattern, raw):
            models[match.group(1)] = {
                "input_tokens": self._parse_token_value(match.group(2) + match.group(3)),
                "output_tokens": self._parse_token_value(match.group(4) + match.group(5)),
                "cached_tokens": self._parse_token_value(match.group(6) + match.group(7)),
                "premium_requests": int(match.group(8)),
            }
        if models:
            usage["models"] = models

        return usage

    def kill_session(self, session_id: Optional[str] = None) -> bool:
        """Delete a CLI session directory from disk. Returns True if deleted."""
        import shutil

        sid = session_id or self._resume_session_id
        if not sid:
            return False
        config_dir = os.path.expanduser("~/.copilot")
        session_dir = os.path.join(config_dir, "session-state", sid)
        if not os.path.isdir(session_dir):
            return False
        try:
            shutil.rmtree(session_dir)
            logger.info("Killed CLI session %s (%s)", sid, session_dir)
            if self._resume_session_id == sid:
                self._resume_session_id = None
            if self._session_id == sid:
                self._session_id = None
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to kill session %s: %s", sid, exc)
            return False

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
    def _await_if_needed(value: Any) -> Any:
        if inspect.isawaitable(value):
            return asyncio.run(value)
        return value

    @staticmethod
    def _extract_sdk_text(response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        for attr in ("content", "text", "message"):
            val = getattr(response, attr, None)
            if isinstance(val, str):
                return val
        data = getattr(response, "data", None)
        if data is not None:
            for attr in ("content", "text", "message"):
                val = getattr(data, attr, None)
                if isinstance(val, str):
                    return val
                if isinstance(data, dict) and isinstance(data.get(attr), str):
                    return data[attr]
        if isinstance(response, dict):
            for key in ("content", "text", "message"):
                val = response.get(key)
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    nested = val.get("content") or val.get("text")
                    if isinstance(nested, str):
                        return nested
        return str(response)

    def _load_sdk_client_type(self) -> type | None:
        candidates = (
            ("github_copilot_sdk", "CopilotClient"),
            ("copilot_sdk", "CopilotClient"),
        )
        for module_name, class_name in candidates:
            try:
                module = __import__(module_name, fromlist=[class_name])
            except ImportError:
                continue
            client_type = getattr(module, class_name, None)
            if isinstance(client_type, type):
                return client_type
        return None

    def _run_prompt_api(
        self,
        prompt: str,
        *,
        model: Optional[str],
        log_prefix: str,
    ) -> str:
        client_type = self._load_sdk_client_type()
        if client_type is None:
            raise CopilotCliError("Copilot SDK backend unavailable (missing github_copilot_sdk/copilot_sdk)")
        try:
            client = client_type()
            create_session = getattr(client, "create_session", None) or getattr(client, "createSession", None)
            if not callable(create_session):
                raise CopilotCliError("Copilot SDK backend missing create_session/createSession")
            session = self._await_if_needed(create_session(model=model) if model else create_session())
            send = getattr(session, "send_and_wait", None) or getattr(session, "sendAndWait", None) or getattr(session, "send", None)
            if not callable(send):
                raise CopilotCliError("Copilot SDK session missing send_and_wait/sendAndWait/send")
            try:
                response = self._await_if_needed(send(prompt=prompt))
            except TypeError:
                response = self._await_if_needed(send(prompt))
            output = self._extract_sdk_text(response).strip()
            if output:
                self._log_line(output, prefix=log_prefix)
            stop = getattr(client, "stop", None)
            if callable(stop):
                self._await_if_needed(stop())
            self._initialized = True
            return output
        except CopilotCliError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CopilotCliError(f"copilot API backend error: {exc}") from exc

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
        cmd = self._base_cmd(resume_id=resume_id, autopilot=autopilot)
        cmd.extend(["-p", prompt])
        if model:
            cmd.extend(["--model", model])

        effective_cwd = cwd or self.workspace_dir
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
        timed_out = False
        timeout_timer: Optional[threading.Timer] = None
        if self.timeout and self.timeout > 0:
            def _on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass

            timeout_timer = threading.Timer(self.timeout, _on_timeout)
            timeout_timer.daemon = True
            timeout_timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                output_lines.append(line)
                self._log_line(line, prefix=log_prefix)
                if on_line:
                    should_stop = on_line(line.rstrip("\n\r"))
                    if should_stop:
                        early_stopped = True
                        logger.info("%s | Early stop requested; terminating Copilot CLI process", log_prefix)
                        process.terminate()
                        break
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except CopilotCliError:
            raise
        except Exception as exc:
            process.kill()
            raise CopilotCliError(f"copilot CLI error: {exc}") from exc
        finally:
            if timeout_timer:
                timeout_timer.cancel()

        output = "".join(output_lines).strip()
        if timed_out:
            raise CopilotCliError(f"copilot CLI timed out after {self.timeout}s")
        if process.returncode != 0 and not early_stopped:
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
            effective_autopilot = self.autopilot if autopilot is None else autopilot
            if allow_retry and effective_autopilot and self._should_retry_without_autopilot(output):
                logger.warning("copilot CLI rejected --autopilot; retrying without it")
                self.autopilot = False
                return self._run_prompt_cli(
                    prompt,
                    model=model,
                    cwd=cwd,
                    log_prefix=log_prefix,
                    resume_id=resume_id,
                    allow_retry=False,
                    autopilot=False,
                    on_line=on_line,
                )
            if not output:
                raise CopilotCliError(f"copilot CLI failed with exit code {process.returncode}")

        logger.info("%s → complete (%d chars)", log_prefix, len(output))
        self._initialized = True
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
        execution_backend: Optional[Literal["api", "cli"]] = None,
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
        backend = execution_backend or self.execution_backend
        if backend == "api":
            try:
                output = self._run_prompt_api(prompt, model=model, log_prefix=log_prefix)
                logger.info("%s → complete (%d chars) [backend=api]", log_prefix, len(output))
                return output
            except CopilotCliError as api_exc:
                if not self.allow_cli_fallback:
                    raise
                logger.warning("Copilot API backend failed; using explicit CLI fallback: %s", api_exc)
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
