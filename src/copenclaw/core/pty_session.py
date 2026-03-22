"""
PTY bridge mode — persistent Copilot CLI session per Telegram chat_id,
using Windows ConPTY via pywinpty.

Each Telegram chat gets its own long-lived PtySession that wraps a
``gh copilot`` (or similar) subprocess running inside a ConPTY.
Responses are delimited by EOM_MARKER so callers can block until a
complete reply is ready without polling the rest of the application.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

if sys.platform == "win32":
    from winpty import PtyProcess

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EOM_MARKER = "***EOM***"
DEFAULT_EOM_TIMEOUT_SEC = 120
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PtySession
# ---------------------------------------------------------------------------

class PtySession:
    """Manages a single persistent Copilot CLI PTY process for one Telegram chat."""

    def __init__(
        self,
        chat_id: str,
        instructions_path: Optional[Path] = None,
        on_error: Optional[Callable] = None,
    ) -> None:
        """Initialize session state. Does NOT spawn the process — call spawn() explicitly.

        Args:
            chat_id: Telegram chat_id this session belongs to.
            instructions_path: Path to per-chat copilot-instructions.md, or None to
                use the global default.
            on_error: Optional callback with signature ``callback(chat_id, error_text)``
                used to surface fatal errors back to the Telegram chat.
        """
        self.chat_id: str = chat_id
        self.instructions_path: Optional[Path] = instructions_path
        self._process: Optional[PtyProcess] = None  # type: ignore[name-defined]
        self._lock: threading.Lock = threading.Lock()
        self._on_error: Optional[Callable[[str, str], None]] = on_error
        self._restart_count: int = 0
        self._last_restart_time: float = 0.0
        self._monitor_thread: Optional[threading.Thread] = None

    def spawn(self) -> None:
        """Launch the Copilot CLI process in a ConPTY.

        Sets the ``COPILOT_INSTRUCTIONS`` environment variable to
        ``instructions_path`` when provided.

        Raises:
            RuntimeError: If the process is already running or the host OS is
                not Windows.
        """
        if sys.platform != "win32":
            raise RuntimeError("PTY bridge requires Windows (ConPTY)")
        if self.is_alive():
            raise RuntimeError(f"Session {self.chat_id} is already running")

        copilot_cmd = shutil.which("copilot")
        if copilot_cmd is None:
            username = os.environ.get("USERNAME", os.environ.get("USER", ""))
            candidates = [
                rf"C:\Users\{username}\AppData\Roaming\npm\copilot.cmd",
                r"C:\Program Files\GitHub Copilot CLI\copilot.cmd",
            ]
            for path in candidates:
                if os.path.isfile(path):
                    copilot_cmd = path
                    break
        if copilot_cmd is None:
            raise RuntimeError("Could not find the copilot executable")

        env = dict(os.environ)
        if self.instructions_path is not None and self.instructions_path.exists():
            env["COPILOT_INSTRUCTIONS_FILE"] = str(self.instructions_path)

        self._process = PtyProcess.spawn(
            [copilot_cmd],
            env=env,
            dimensions=(50, 220),
        )
        logger.info(f"[PTY] Spawned session for chat_id={self.chat_id}, pid={self._process.pid}")
        time.sleep(2)
        self._start_monitor_thread()

    def _start_monitor_thread(self) -> None:
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while self.is_alive():
            time.sleep(5)
        logger.warning(f"[PTY] Process died for chat_id={self.chat_id}")
        if self._on_error is not None:
            self._on_error(self.chat_id, "⚠️ Copilot CLI session died unexpectedly. Send any message to restart.")

    def write(self, text: str) -> None:
        """Send text to the PTY stdin. Appends newline if not present.

        Args:
            text: The text to write to the process.

        Raises:
            RuntimeError: If the process is not currently alive.
        """
        if not self.is_alive():
            raise RuntimeError(f"Session {self.chat_id} is not running")
        with self._lock:
            if not text.endswith("\n"):
                text += "\n"
            self._process.write(text)
            logger.debug(f"[PTY] write to {self.chat_id}: {text[:80]!r}")

    def read_until_eom(self, timeout_sec: float = DEFAULT_EOM_TIMEOUT_SEC) -> str:
        """Read PTY output until EOM_MARKER is seen or timeout expires.

        Strips ANSI escape codes from the accumulated output before returning.
        The EOM_MARKER itself is removed from the returned string.

        Args:
            timeout_sec: Maximum seconds to wait for the marker before giving
                up and returning whatever has been collected so far.

        Returns:
            Accumulated PTY output with ANSI codes and EOM_MARKER removed.
            On timeout, returns partial output collected up to that point.
        """
        if not self.is_alive():
            raise RuntimeError(f"Session {self.chat_id} is not running")

        buffer = ""
        deadline = time.monotonic() + timeout_sec

        while True:
            if time.monotonic() > deadline:
                logger.warning(f"[PTY] EOM timeout for chat_id={self.chat_id}")
                break

            try:
                chunk = self._process.read(4096)
            except EOFError:
                logger.warning(f"[PTY] EOF on read for {self.chat_id}")
                break
            except Exception as e:
                logger.warning(f"[PTY] Read error for {self.chat_id}: {e}")
                break

            if not chunk:
                time.sleep(0.05)
                continue

            chunk = self._strip_ansi(chunk)
            buffer += chunk

            if EOM_MARKER in buffer:
                pre_eom = buffer.split(EOM_MARKER, 1)[0]
                logger.debug(f"[PTY] EOM received for {self.chat_id}, {len(buffer)} chars")
                return pre_eom.strip()

        return buffer.strip()

    def _strip_ansi(self, text: str) -> str:
        """Remove ANSI escape sequences from text using ANSI_ESCAPE_RE.

        Args:
            text: Raw text that may contain ANSI escape codes.

        Returns:
            Text with all ANSI escape sequences removed.
        """
        return ANSI_ESCAPE_RE.sub('', text)

    def is_alive(self) -> bool:
        """Return True if the PTY process exists and has not exited."""
        return self._process is not None and self._process.isalive()

    def close(self) -> None:
        """Terminate the PTY process gracefully. Logs if process was not running."""
        if not self.is_alive():
            logger.debug(f"[PTY] close() called but session {self.chat_id} is not running")
            return
        try:
            self._process.terminate()
        except Exception as e:
            logger.warning(f"[PTY] Error terminating {self.chat_id}: {e}")
        finally:
            self._process = None
        logger.info(f"[PTY] Closed session for chat_id={self.chat_id}")

    def restart(self) -> None:
        """Close existing process if alive, then call spawn().

        Increments ``_restart_count`` and records ``_last_restart_time``.
        Logs the restart attempt including ``chat_id`` and the new count.
        """
        self.close()
        self._restart_count += 1
        self._last_restart_time = time.time()
        logger.info(f"[PTY] Restarting session {self.chat_id} (attempt #{self._restart_count})")
        self.spawn()


# ---------------------------------------------------------------------------
# ChatSessionMap
# ---------------------------------------------------------------------------

class ChatSessionMap:
    """Thread-safe registry mapping Telegram chat_id strings to PtySession instances."""

    def __init__(self) -> None:
        """Initialize empty session map and lock."""
        self._sessions: Dict[str, PtySession] = {}
        self._lock: threading.Lock = threading.Lock()

    def get_or_create(
        self,
        chat_id: str,
        instructions_path: Optional[Path] = None,
        on_error: Optional[Callable] = None,
    ) -> PtySession:
        """Return existing PtySession for chat_id, or create and spawn a new one.

        Thread-safe — multiple callers with the same ``chat_id`` will always
        receive the same session object.

        Args:
            chat_id: Telegram chat identifier.
            instructions_path: Forwarded to ``PtySession.__init__`` when creating
                a new session.
            on_error: Forwarded to ``PtySession.__init__`` when creating a new
                session.

        Returns:
            The live ``PtySession`` for this chat.
        """
        with self._lock:
            if chat_id in self._sessions and self._sessions[chat_id].is_alive():
                return self._sessions[chat_id]
            session = PtySession(chat_id, instructions_path, on_error)
            session.spawn()
            self._sessions[chat_id] = session
            return session

    def get(self, chat_id: str) -> Optional[PtySession]:
        """Return session for chat_id if it exists, else None.

        Args:
            chat_id: Telegram chat identifier.

        Returns:
            Existing ``PtySession`` or ``None``.
        """
        with self._lock:
            return self._sessions.get(chat_id)

    def remove(self, chat_id: str) -> None:
        """Close and remove session for chat_id if present.

        Args:
            chat_id: Telegram chat identifier.
        """
        with self._lock:
            if chat_id in self._sessions:
                self._sessions[chat_id].close()
                del self._sessions[chat_id]
                logger.info(f"[PTY] Removed session {chat_id}")

    def active_sessions(self) -> Dict[str, PtySession]:
        """Return a snapshot dict of all currently tracked sessions.

        Returns:
            A shallow copy of the internal ``{chat_id: PtySession}`` mapping.
        """
        with self._lock:
            return dict(self._sessions)
