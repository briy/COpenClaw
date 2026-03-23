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
import queue as stdlib_queue
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
READY_TIMEOUT_SEC = 90  # max time to wait for Copilot CLI to finish loading
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# OSC sequences: \x1B] ... \x07  (or \x1B\\)
OSC_RE = re.compile(r'\x1B\][^\x07]*\x07|\x1B\].*?\x1B\\')
# CSI sequences missed by the simple regex
CSI_RE = re.compile(r'\x1B\[[0-9;]*[A-Za-z]')
_TG_MAX_CHARS = 4000  # Telegram limit is 4096; leave headroom

# Patterns that indicate Copilot CLI is ready for input
_READY_PATTERNS = [
    'shift+tab',       # appears in the prompt area
    'Type @ to mention',  # prompt text
]

# TUI chrome to strip from output before sending to Telegram
_TUI_CHROME_PATTERNS = [
    re.compile(r'[╭╮╰╯│─┌┐└┘├┤┬┴┼]+'),  # box-drawing
    re.compile(r'[◉◎○●▘▝█▔]+'),           # spinner / block chars
    re.compile(r'\d+;\d*;?\d*;?\d*'),       # OSC numeric params (0;C:\..., 9;4;0;0)
    re.compile(r'shift\+tab switch mode'),  # TUI hint
    re.compile(r'Type @ to mention.*shortcuts', re.DOTALL),  # prompt hint block
    re.compile(r'Loading environment:.*', re.MULTILINE),  # spinner lines
    re.compile(r'Unlimited reqs\..*', re.MULTILINE),  # status bar
    re.compile(r'claude-opus-\S+.*', re.MULTILINE),  # model info line
    re.compile(r'MCP server.*connect\.', re.MULTILINE),  # MCP loading msgs
    re.compile(r'Experimental mode.*future\.', re.MULTILINE),  # experimental warning
    re.compile(r'GitHub Copilot v[\d.]+'),  # version banner
    re.compile(r'Describe a task to get started\.'),  # welcome text
    re.compile(r'Tip:.*\n?'),  # tip lines
    re.compile(r'No copilot instructions found\..*\n?'),  # instructions warning
    re.compile(r'Copilot uses AI.*mistakes\.\s*'),  # disclaimer
]


def _clean_pty_output(text: str) -> str:
    """Strip ANSI, OSC, TUI chrome, control chars; truncate to Telegram limit."""
    text = ANSI_ESCAPE_RE.sub('', text)
    text = OSC_RE.sub('', text)
    text = CSI_RE.sub('', text)
    # Remove non-printable control characters (except newline/tab)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    # Strip TUI chrome patterns
    for pattern in _TUI_CHROME_PATTERNS:
        text = pattern.sub('', text)
    # Collapse excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = text.strip()
    if len(text) > _TG_MAX_CHARS:
        text = text[:_TG_MAX_CHARS] + '\n…(truncated)'
    return text

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
        self._reader_thread: Optional[threading.Thread] = None
        self._read_queue: stdlib_queue.Queue = stdlib_queue.Queue()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task] = None

    def spawn(self) -> None:
        """Launch the Copilot CLI process in a ConPTY.

        Sets the working directory to the per-chat directory (so Copilot
        finds ``.github/copilot-instructions.md``) and waits for the CLI
        to finish loading before marking the session as ready.

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

        # Determine CWD: use per-chat directory if instructions exist there,
        # so Copilot CLI picks up .github/copilot-instructions.md natively.
        cwd: Optional[str] = None
        if self.instructions_path is not None and self.instructions_path.exists():
            # instructions_path is e.g. ~/.copenclaw/chats/{id}/.github/copilot-instructions.md
            # CWD should be the chat root (two levels up from .github/copilot-instructions.md)
            chat_dir = self.instructions_path.parent.parent
            cwd = str(chat_dir)
            logger.info(f"[PTY] Using CWD={cwd} for chat_id={self.chat_id}")

        self._process = PtyProcess.spawn(
            [copilot_cmd],
            env=env,
            cwd=cwd,
            dimensions=(50, 220),
        )
        logger.info(f"[PTY] Spawned session for chat_id={self.chat_id}, pid={self._process.pid}")

        # Start the reader thread FIRST so we can drain startup output
        self._start_reader_thread()

        # Wait for Copilot CLI to finish loading (detect ready prompt)
        self._wait_for_ready()

        self._start_monitor_thread()
        self.start_consumer()

    def _wait_for_ready(self) -> None:
        """Block until Copilot CLI has finished loading and shows its prompt.

        Strategy: first wait for a known ready pattern (``shift+tab`` in the
        prompt area), then wait for output to go silent for 3 seconds — meaning
        the loading spinners have stopped and the CLI is truly idle.
        """
        deadline = time.monotonic() + READY_TIMEOUT_SEC
        startup_buf = ""
        found_pattern = False

        while time.monotonic() < deadline:
            try:
                chunk = self._read_queue.get(timeout=1.0)
            except stdlib_queue.Empty:
                if found_pattern:
                    # We saw the prompt pattern AND had 1s of silence — check for 3s total
                    pass
                continue
            if chunk is None:  # EOF
                logger.warning(f"[PTY] EOF during startup wait for {self.chat_id}")
                break
            startup_buf += chunk
            if not found_pattern:
                lower = startup_buf.lower()
                for pattern in _READY_PATTERNS:
                    if pattern.lower() in lower:
                        found_pattern = True
                        logger.debug(f"[PTY] Ready pattern matched for {self.chat_id}: '{pattern}'")
                        break

        # Now wait for silence — no output for 3 seconds means spinners stopped
        silence_start = time.monotonic()
        silence_needed = 3.0
        while time.monotonic() - silence_start < silence_needed and time.monotonic() < deadline:
            try:
                chunk = self._read_queue.get(timeout=0.5)
                if chunk is None:
                    break
                silence_start = time.monotonic()  # reset silence timer on new output
            except stdlib_queue.Empty:
                continue  # no output — silence continues

        # Final flush of anything left
        while not self._read_queue.empty():
            try:
                self._read_queue.get_nowait()
            except stdlib_queue.Empty:
                break

        logger.info(f"[PTY] Ready for chat_id={self.chat_id} (pattern={'found' if found_pattern else 'timeout'})")

    def _start_reader_thread(self) -> None:
        """Start a background thread that continuously drains the PTY into _read_queue."""
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        """Continuously read from the PTY process and push chunks into _read_queue.

        Runs until the process dies or raises EOFError. Uses a sentinel None
        to signal EOF to read_until_eom callers.
        """
        while self.is_alive():
            try:
                chunk = self._process.read(4096)
                if chunk:
                    self._read_queue.put(chunk)
                else:
                    time.sleep(0.02)
            except EOFError:
                break
            except Exception as e:
                logger.warning(f"[PTY] Reader error for {self.chat_id}: {e}")
                break
        self._read_queue.put(None)  # sentinel: EOF
        logger.debug(f"[PTY] Reader thread exiting for {self.chat_id}")

    def _start_monitor_thread(self) -> None:
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def start_consumer(self) -> None:
        """Schedule the async consumer coroutine on the running event loop.

        Safe to call from any context — silently skips if no event loop is
        running (e.g. when spawn() is called from a sync thread; the consumer
        will be started lazily by the first enqueue() call instead).
        """
        if self._consumer_task is not None and not self._consumer_task.done():
            return
        try:
            asyncio.get_running_loop()  # raises RuntimeError if no loop is running
            self._consumer_task = asyncio.ensure_future(self._consume())
            logger.info(f"[PTY] Consumer started for chat_id={self.chat_id}")
        except RuntimeError:
            logger.debug(f"[PTY] start_consumer called outside event loop for chat_id={self.chat_id}; will start on first enqueue")

    async def _consume(self) -> None:
        """Drain the per-chat queue one message at a time."""
        try:
            while True:
                text, reply_fn = await self._queue.get()
                try:
                    # Flush stale output from previous response before writing new message
                    await asyncio.get_running_loop().run_in_executor(None, self._flush_read_queue)
                    # Write the user's message — per-chat instructions already tell Copilot
                    # to end responses with EOM_MARKER. Do NOT include the marker in the
                    # written text or the PTY echo will trigger false EOM detection.
                    await asyncio.get_running_loop().run_in_executor(None, lambda: self.write(text))
                    # Skip the input echo — wait 2s for the PTY to echo back the typed text
                    # then flush it so read_until_eom only sees Copilot's actual response
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self._skip_echo(2.0)
                    )
                    response = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: self.read_until_eom(timeout_sec=120)
                    )
                    # Clean up TUI artifacts and enforce Telegram 4096-char limit
                    response = _clean_pty_output(response)
                    await reply_fn(response if response else "⚠️ No response (timeout). Try again.")
                except Exception as e:
                    logger.error(f"[PTY] Consumer error for {self.chat_id}: {e}")
                    try:
                        await reply_fn(f"⚠️ Session error: {e}"[:500])
                    except Exception as inner:
                        logger.error(f"[PTY] reply_fn also failed for {self.chat_id}: {inner}")
                finally:
                    self._queue.task_done()
        except Exception as e:
            logger.error(f"[PTY] Consumer died unexpectedly for chat_id={self.chat_id}: {e}")

    async def enqueue(self, text: str, reply_fn) -> None:
        """Enqueue a message for serialised processing by the consumer coroutine.

        Starts the consumer if it is not already running (safe to call from
        any async context).
        """
        self.start_consumer()
        await self._queue.put((text, reply_fn))
        logger.debug(f"[PTY] Enqueued message for {self.chat_id}, queue size={self._queue.qsize()}")

    def _monitor_loop(self) -> None:
        while self.is_alive():
            time.sleep(5)
        logger.warning(f"[PTY] Process died for chat_id={self.chat_id}")
        if self._on_error is not None:
            self._on_error(self.chat_id, "⚠️ Copilot CLI session died unexpectedly. Send any message to restart.")

    def _flush_read_queue(self) -> None:
        """Drain any stale output from the read queue before sending a new message."""
        flushed = 0
        while not self._read_queue.empty():
            try:
                self._read_queue.get_nowait()
                flushed += 1
            except stdlib_queue.Empty:
                break
        if flushed:
            logger.debug(f"[PTY] Flushed {flushed} stale chunks for {self.chat_id}")

    def _skip_echo(self, duration: float = 2.0) -> None:
        """Drain PTY output for `duration` seconds to skip the echoed input.

        After writing to a PTY, the terminal echoes the typed text back.
        This echo can contain patterns that confuse read_until_eom.
        We discard output during this window so only Copilot's real response
        is seen by the reader.
        """
        deadline = time.monotonic() + duration
        skipped = 0
        while time.monotonic() < deadline:
            try:
                chunk = self._read_queue.get(timeout=0.2)
                if chunk is None:
                    self._read_queue.put(None)  # re-queue the EOF sentinel
                    break
                skipped += len(chunk)
            except stdlib_queue.Empty:
                continue
        logger.debug(f"[PTY] Skipped {skipped} echo chars for {self.chat_id}")

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

        Consumes from the background _read_queue (populated by _reader_loop)
        using per-chunk timeouts so the overall deadline is always respected —
        even when the PTY has no pending output and read() would block.

        Returns accumulated text with ANSI codes and EOM_MARKER stripped.
        Returns partial output on timeout or EOF.
        """
        if not self.is_alive():
            raise RuntimeError(f"Session {self.chat_id} is not running")

        buffer = ""
        deadline = time.monotonic() + timeout_sec

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(f"[PTY] EOM timeout for chat_id={self.chat_id}")
                break

            try:
                chunk = self._read_queue.get(timeout=min(1.0, remaining))
            except stdlib_queue.Empty:
                continue  # loop back and recheck deadline

            if chunk is None:  # EOF sentinel from reader thread
                logger.warning(f"[PTY] EOF sentinel received for {self.chat_id}")
                break

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
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
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
