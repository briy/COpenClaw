"""
PTY session routing — thin façade over ChatSessionMap.

Provides per-chat session lookup / creation and per-chat
``copilot-instructions.md`` discovery.  All callers share the same
module-level singleton so session state is preserved across requests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from copenclaw.core.pty_session import ChatSessionMap, PtySession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants and singleton
# ---------------------------------------------------------------------------

CHATS_DIR: Path = Path.home() / ".copenclaw" / "chats"
CHATS_DIR.mkdir(parents=True, exist_ok=True)

_session_map: ChatSessionMap = ChatSessionMap()


# ---------------------------------------------------------------------------
# Per-chat instructions discovery
# ---------------------------------------------------------------------------

def get_instructions_path(chat_id: str) -> Optional[Path]:
    """Return path to per-chat copilot-instructions.md if it exists, else None.

    Convention: ``~/.copenclaw/chats/<chat_id>/.github/copilot-instructions.md``
    (placed under ``.github/`` so Copilot CLI finds it natively when the chat
    directory is used as the working directory).
    """
    candidate = CHATS_DIR / str(chat_id) / ".github" / "copilot-instructions.md"
    return candidate if candidate.exists() else None


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def get_session(chat_id: str, on_error: Optional[Callable] = None) -> PtySession:
    """Return the live PtySession for *chat_id*, creating and spawning one if needed.

    Args:
        chat_id: Telegram (or other channel) chat identifier.
        on_error: Optional ``callback(chat_id, error_text)`` forwarded to the
            session so fatal errors can be surfaced back to the user.

    Returns:
        The live ``PtySession`` for this chat.
    """
    return _session_map.get_or_create(
        chat_id,
        get_instructions_path(chat_id),
        on_error,
    )


def remove_session(chat_id: str) -> None:
    """Close and remove the session for *chat_id* if one exists."""
    _session_map.remove(chat_id)


def active_sessions() -> Dict[str, PtySession]:
    """Return a snapshot of all currently tracked ``{chat_id: PtySession}`` pairs."""
    return _session_map.active_sessions()


def shutdown_all_sessions() -> None:
    """Close every active session. Call on application shutdown."""
    for chat_id in list(active_sessions()):
        logger.info("[PTY] shutdown_all_sessions: closing %s", chat_id)
        remove_session(chat_id)
