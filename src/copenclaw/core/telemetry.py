"""Harvest telemetry from Copilot CLI session events.

Parses ``events.jsonl`` produced by Copilot CLI to extract token counts,
turn metrics, and model information.  All logic is deterministic Python —
no LLM calls.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionMetrics:
    """Telemetry snapshot for a single Copilot CLI session."""

    session_id: str = ""

    # Turn counts
    total_turns: int = 0
    user_messages: int = 0

    # Token counts (from session.shutdown or accumulated from assistant.message)
    total_output_tokens: int = 0
    current_tokens: int = 0          # total context window usage at shutdown
    system_tokens: int = 0           # system prompt / instructions overhead
    conversation_tokens: int = 0     # conversation history tokens
    tool_definition_tokens: int = 0  # MCP tool definitions overhead

    # Per-model breakdown
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0

    # Size metrics
    total_input_chars: int = 0
    total_output_chars: int = 0

    # Timing
    total_api_duration_ms: int = 0
    harvest_timestamp: float = field(default_factory=time.time)

    @property
    def overhead_tokens(self) -> int:
        """Fixed overhead: system prompt + tool definitions."""
        return self.system_tokens + self.tool_definition_tokens

    @property
    def context_utilization_pct(self) -> float:
        """Estimated context window utilization as a percentage.

        Uses a 200K token window as the default for Opus/Sonnet models.
        Returns 0.0 if no token data is available.
        """
        if not self.current_tokens:
            return 0.0
        # Copilot CLI models are typically 200K context
        window_size = 200_000
        return min(100.0, (self.current_tokens / window_size) * 100)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "total_turns": self.total_turns,
            "user_messages": self.user_messages,
            "total_output_tokens": self.total_output_tokens,
            "current_tokens": self.current_tokens,
            "system_tokens": self.system_tokens,
            "conversation_tokens": self.conversation_tokens,
            "tool_definition_tokens": self.tool_definition_tokens,
            "overhead_tokens": self.overhead_tokens,
            "context_utilization_pct": round(self.context_utilization_pct, 1),
            "model": self.model,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_input_chars": self.total_input_chars,
            "total_output_chars": self.total_output_chars,
            "total_api_duration_ms": self.total_api_duration_ms,
            "harvest_timestamp": self.harvest_timestamp,
        }


def _sessions_dir() -> Optional[str]:
    """Return the Copilot CLI session-state directory, or None."""
    config_dir = os.path.expanduser("~/.copilot")
    for subdir in ("session-state", "sessions"):
        candidate = os.path.join(config_dir, subdir)
        if os.path.isdir(candidate):
            return candidate
    return None


def harvest_session_metrics(session_id: str) -> Optional[SessionMetrics]:
    """Parse events.jsonl for *session_id* and return metrics.

    Returns None if the session directory or events file doesn't exist.
    Reads only the events file — no LLM calls, no subprocess launches.
    """
    base = _sessions_dir()
    if not base:
        return None
    events_path = os.path.join(base, session_id, "events.jsonl")
    if not os.path.isfile(events_path):
        logger.debug("No events.jsonl for session %s", session_id)
        return None

    metrics = SessionMetrics(session_id=session_id)

    # Track the latest shutdown event (most complete token data)
    latest_shutdown: Optional[dict] = None

    try:
        with open(events_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")
                data = event.get("data", {})

                if etype == "assistant.turn_start":
                    metrics.total_turns += 1

                elif etype == "user.message":
                    metrics.user_messages += 1
                    content = data.get("content", "") or ""
                    metrics.total_input_chars += len(content)

                elif etype == "assistant.message":
                    output_tokens = data.get("outputTokens", 0) or 0
                    metrics.total_output_tokens += output_tokens
                    content = data.get("content", "") or ""
                    metrics.total_output_chars += len(content)

                elif etype == "session.model_change":
                    metrics.model = data.get("newModel", "") or ""

                elif etype == "session.shutdown":
                    latest_shutdown = data

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse events.jsonl for session %s: %s", session_id, exc)
        return metrics  # return partial data

    # Extract the richest token data from the latest shutdown event
    if latest_shutdown:
        metrics.current_tokens = latest_shutdown.get("currentTokens", 0) or 0
        metrics.system_tokens = latest_shutdown.get("systemTokens", 0) or 0
        metrics.conversation_tokens = latest_shutdown.get("conversationTokens", 0) or 0
        metrics.tool_definition_tokens = latest_shutdown.get("toolDefinitionsTokens", 0) or 0
        metrics.total_api_duration_ms = latest_shutdown.get("totalApiDurationMs", 0) or 0

        # Extract per-model usage from modelMetrics
        model_metrics = latest_shutdown.get("modelMetrics", {})
        if model_metrics:
            # Use the current model's metrics, or fall back to first available
            model_name = latest_shutdown.get("currentModel", "")
            if model_name:
                metrics.model = model_name
            model_data = model_metrics.get(metrics.model, {})
            if not model_data and model_metrics:
                # Fall back to first model entry
                first_model = next(iter(model_metrics))
                model_data = model_metrics[first_model]
                if not metrics.model:
                    metrics.model = first_model
            usage = model_data.get("usage", {})
            metrics.input_tokens = usage.get("inputTokens", 0) or 0
            metrics.cache_read_tokens = usage.get("cacheReadTokens", 0) or 0

    logger.info(
        "Harvested session %s: %d turns, %d output tokens, %.1f%% context",
        session_id,
        metrics.total_turns,
        metrics.total_output_tokens,
        metrics.context_utilization_pct,
    )
    return metrics


def harvest_session_metrics_fast(session_id: str) -> Optional[SessionMetrics]:
    """Fast harvest that only reads the last shutdown event.

    Much faster for large sessions — reads the file in reverse to find the
    last ``session.shutdown`` line.  Use this for real-time budget checks;
    use :func:`harvest_session_metrics` for full historical analysis.
    """
    base = _sessions_dir()
    if not base:
        return None
    events_path = os.path.join(base, session_id, "events.jsonl")
    if not os.path.isfile(events_path):
        return None

    metrics = SessionMetrics(session_id=session_id)

    # Read the file in reverse to find the latest session.shutdown
    try:
        file_size = os.path.getsize(events_path)
        # For small files, just scan forward
        if file_size < 100_000:
            return harvest_session_metrics(session_id)

        # For large files, read the tail (last 50KB should contain shutdown)
        with open(events_path, "rb") as fh:
            tail_size = min(file_size, 50_000)
            fh.seek(file_size - tail_size)
            tail = fh.read().decode("utf-8", errors="replace")

        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line or "session.shutdown" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "session.shutdown":
                continue
            data = event.get("data", {})
            metrics.current_tokens = data.get("currentTokens", 0) or 0
            metrics.system_tokens = data.get("systemTokens", 0) or 0
            metrics.conversation_tokens = data.get("conversationTokens", 0) or 0
            metrics.tool_definition_tokens = data.get("toolDefinitionsTokens", 0) or 0
            metrics.total_api_duration_ms = data.get("totalApiDurationMs", 0) or 0
            model_metrics = data.get("modelMetrics", {})
            if model_metrics:
                model_name = data.get("currentModel", "")
                if model_name:
                    metrics.model = model_name
                model_data = model_metrics.get(metrics.model, {})
                if not model_data and model_metrics:
                    first_model = next(iter(model_metrics))
                    model_data = model_metrics[first_model]
                    if not metrics.model:
                        metrics.model = first_model
                usage = model_data.get("usage", {})
                metrics.input_tokens = usage.get("inputTokens", 0) or 0
                metrics.cache_read_tokens = usage.get("cacheReadTokens", 0) or 0
            return metrics

    except Exception as exc:  # noqa: BLE001
        logger.warning("Fast harvest failed for session %s: %s", session_id, exc)

    return None
