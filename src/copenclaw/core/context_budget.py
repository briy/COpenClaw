"""Context budget tracking for Copilot CLI sessions.

Monitors estimated context window usage and triggers rotation when
thresholds are exceeded.  All logic is deterministic Python — no LLM calls.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from copenclaw.core.telemetry import SessionMetrics, harvest_session_metrics_fast

logger = logging.getLogger(__name__)

# Default context window size for Copilot CLI models (tokens)
_DEFAULT_WINDOW_SIZE = 200_000

# Thresholds as fraction of window size
_WARNING_THRESHOLD = 0.60
_ROTATION_THRESHOLD = 0.80

# Rough chars-to-tokens ratio for pre-flight estimation
_CHARS_PER_TOKEN = 4


@dataclass
class BudgetStatus:
    """Result of a budget check."""

    session_id: str
    current_tokens: int
    window_size: int
    utilization_pct: float
    needs_rotation: bool
    warning: bool
    overhead_tokens: int
    conversation_tokens: int
    headroom_tokens: int

    @property
    def available_tokens(self) -> int:
        """Tokens remaining before rotation threshold."""
        rotation_limit = int(self.window_size * _ROTATION_THRESHOLD)
        return max(0, rotation_limit - self.current_tokens)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "current_tokens": self.current_tokens,
            "window_size": self.window_size,
            "utilization_pct": round(self.utilization_pct, 1),
            "needs_rotation": self.needs_rotation,
            "warning": self.warning,
            "overhead_tokens": self.overhead_tokens,
            "conversation_tokens": self.conversation_tokens,
            "headroom_tokens": self.headroom_tokens,
            "available_tokens": self.available_tokens,
        }


class ContextBudget:
    """Tracks context window budget for a Copilot CLI session.

    Uses telemetry harvested from Copilot CLI's ``events.jsonl`` — no LLM
    calls, no subprocess launches.  All decisions are arithmetic.
    """

    def __init__(
        self,
        window_size: Optional[int] = None,
        warning_threshold: Optional[float] = None,
        rotation_threshold: Optional[float] = None,
    ) -> None:
        self.window_size = window_size or int(
            os.environ.get("COPENCLAW_CONTEXT_WINDOW_SIZE", _DEFAULT_WINDOW_SIZE)
        )
        self.warning_threshold = warning_threshold or float(
            os.environ.get("COPENCLAW_CONTEXT_WARNING_THRESHOLD", _WARNING_THRESHOLD)
        )
        self.rotation_threshold = rotation_threshold or float(
            os.environ.get("COPENCLAW_CONTEXT_ROTATION_THRESHOLD", _ROTATION_THRESHOLD)
        )

    def check(self, session_id: str) -> Optional[BudgetStatus]:
        """Check context budget for a session.  Returns None if no data available."""
        metrics = harvest_session_metrics_fast(session_id)
        if not metrics or not metrics.current_tokens:
            return None

        utilization = metrics.current_tokens / self.window_size
        rotation_limit = int(self.window_size * self.rotation_threshold)

        status = BudgetStatus(
            session_id=session_id,
            current_tokens=metrics.current_tokens,
            window_size=self.window_size,
            utilization_pct=utilization * 100,
            needs_rotation=utilization >= self.rotation_threshold,
            warning=utilization >= self.warning_threshold,
            overhead_tokens=metrics.overhead_tokens,
            conversation_tokens=metrics.conversation_tokens,
            headroom_tokens=max(0, rotation_limit - metrics.current_tokens),
        )

        if status.needs_rotation:
            logger.warning(
                "CONTEXT BUDGET EXCEEDED for session %s: %.1f%% (%d/%d tokens) — rotation needed",
                session_id, status.utilization_pct, metrics.current_tokens, self.window_size,
            )
        elif status.warning:
            logger.warning(
                "CONTEXT BUDGET WARNING for session %s: %.1f%% (%d/%d tokens)",
                session_id, status.utilization_pct, metrics.current_tokens, self.window_size,
            )

        return status

    def prompt_fits(self, session_id: str, prompt: str) -> bool:
        """Estimate whether a prompt will fit in the remaining budget.

        Uses a rough chars-to-tokens ratio.  Returns True if no budget
        data is available (fail-open).
        """
        status = self.check(session_id)
        if not status:
            return True  # No data — allow the prompt

        estimated_prompt_tokens = len(prompt) // _CHARS_PER_TOKEN
        return (status.current_tokens + estimated_prompt_tokens) < int(
            self.window_size * self.rotation_threshold
        )

    def should_rotate(self, session_id: str) -> bool:
        """Return True if the session should be rotated.  Fail-open (returns False) if no data."""
        status = self.check(session_id)
        if not status:
            return False
        return status.needs_rotation
