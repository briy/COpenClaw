# ADR-003: Token-Based Session Telemetry

## Status
Accepted

## Context
COpenClaw monitors orchestrator session size to prevent context window overflow. The original implementation measured session size by walking the Copilot CLI session directory on disk (KB), with a 500KB red-zone threshold triggering forced rotation.

Real-world data from `/context` showed this approach is flawed:
- A session at 82k/200k tokens (41% utilization) was experiencing 400 errors — with 77.5K tokens of free space remaining
- The disk-KB metric has no predictable correlation with actual token usage
- System/Tools overhead is constant (~31.4K tokens, 16%) and Buffer is reserved (~40.4K, 20%), leaving only ~64% of the window for conversation
- The 500KB threshold caused frequent unnecessary rotations, which themselves consume tokens (bootstrap prompt, recovery context injection)

## Decision
Collect real token usage data to inform rotation decisions. Originally planned to use the CLI's `/context` and `/usage` slash commands via interactive subprocess, but discovered these commands are TUI-only — piped stdin is treated as user messages by the LLM, not CLI commands.

**Revised approach:** Parse `events.jsonl` from the Copilot CLI session state directory (`~/.copilot/session-state/{id}/events.jsonl`). Each `session.shutdown` event contains exact API-reported token counts:

```json
{
  "type": "session.shutdown",
  "data": {
    "currentModel": "claude-opus-4.6",
    "totalPremiumRequests": 6,
    "totalApiDurationMs": 27884,
    "modelMetrics": {
      "claude-opus-4.6": {
        "usage": { "inputTokens": 138217, "outputTokens": 1137, "cacheReadTokens": 87258 }
      }
    }
  }
}
```

This is faster (file read vs subprocess spawn), more reliable, and produces richer data.

### Phase 1: Instrument (this ADR)
- Add `get_session_metrics(session_id)` to `copilot_cli.py` that spawns a short-lived interactive Copilot CLI process, sends `/context` and `/usage` via stdin, parses the output, and returns structured metrics
- Log telemetry to `.data/context-telemetry.jsonl` on: API errors (400/503), periodic watchdog cycles, yellow zone triggers, pre-rotation
- Raise the disk-KB safety net from 500 to 2000 (emergency guard only)
- Collect data to inform Phase 2 thresholds

### Phase 2: Data-Driven Rotation (future)
- Analyze collected telemetry to determine actual failure thresholds
- Replace KB-based yellow/red zones with token-%-based triggers
- Consider model-aware thresholds (200K context for Opus vs smaller for other models)

## Data Sources

### `/context` output (current window state)
```
claude-opus-4.6 · 82k/200k tokens (41%)
○ System/Tools:  31.4k (16%)
◉ Messages:      50.7k (25%)
· Free Space:    77.5k (39%)
◎ Buffer:        40.4k (20%)
```

### `/usage` output (cumulative session stats)
```
Total usage est:        63 Premium requests
API time spent:         27m 53s
Total session time:     40h 59m 31s
Total code changes:     +247 -139
Breakdown by AI model:
 claude-opus-4.6         9.7m in, 38.1k out, 9.1m cached (Est. 63 Premium requests)
```

### Telemetry record format (`.data/context-telemetry.jsonl`)
```json
{
  "ts": "2026-03-15T18:05:00Z",
  "session_id": "abc-123",
  "trigger": "periodic|error_400|error_503|yellow_zone|pre_rotation",
  "context": {
    "model": "claude-opus-4.6",
    "total_tokens": 82000,
    "max_tokens": 200000,
    "pct_used": 41,
    "system_tokens": 31400,
    "message_tokens": 50700,
    "free_tokens": 77500,
    "buffer_tokens": 40400
  },
  "usage": {
    "premium_requests": 63,
    "api_time_seconds": 1673,
    "models": {
      "claude-opus-4.6": {"input": "9.7m", "output": "38.1k", "cached": "9.1m", "premium_requests": 63}
    }
  },
  "disk_size_kb": 423
}
```

## Implementation

### `get_session_metrics(session_id)` in copilot_cli.py
1. Read `~/.copilot/session-state/{session_id}/events.jsonl`
2. Find the last `session.shutdown` event
3. Extract `modelMetrics.{model}.usage.inputTokens` (= context window usage)
4. Look up model's max tokens from `_MODEL_MAX_TOKENS` table
5. Compute `pct_used = inputTokens / maxTokens * 100`
6. Return structured dict with `context` and `usage` sub-dicts

### `_log_context_telemetry(trigger, cli, data_dir)` in gateway.py
- Module-level function (takes `cli` and `data_dir` explicitly to avoid closure scoping issues)
- Spawns a daemon thread that calls `get_session_metrics()` and appends to `.data/context-telemetry.jsonl`

### Collection triggers
| Trigger | Location | When |
|---------|----------|------|
| `periodic` | gateway.py watchdog | Every 5th watchdog cycle (~5 min) |
| `error_400` | router.py | Before killing session on 400 |
| `error_503` | router.py | On 503 error (new handler) |
| `yellow_zone` | gateway.py | When yellow zone is first entered |
| `pre_rotation` | gateway.py | Before any rotation |

## Consequences
- Zero overhead — reads a local file, no subprocess spawn
- Token counts come directly from the API (exact, not estimated)
- JSONL file grows unboundedly (acceptable for Phase 1; add rotation in Phase 2 if needed)
- Disk-KB guard remains as emergency fallback at 2000KB
- `inputTokens` from `session.shutdown` is cumulative across all API calls in the session, not the instantaneous context window size — but serves as a reliable proxy for context growth
- `/context` TUI command remains the gold standard for point-in-time window usage; may revisit PTY-based approach in Phase 2 if needed
