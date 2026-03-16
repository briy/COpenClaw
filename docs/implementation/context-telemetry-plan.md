# Context Telemetry Implementation Plan

## Overview
Add token-based session telemetry to replace disk-KB-based rotation logic.
See ADR-003 for full rationale.

## Tasks

### 1. `get_session_metrics()` in copilot_cli.py
- [ ] Add method to CopilotCli class
- [ ] Spawn interactive process with `--resume=SID --no-alt-screen`
- [ ] Implement stdin write sequence: wait → /context → /usage → /exit
- [ ] Parse /context output with regex (summary + breakdown)
- [ ] Parse /usage output with regex (premium requests + model breakdown)
- [ ] Handle timeouts and parse failures gracefully (return partial data)
- [ ] Add helper `_parse_token_count(value, suffix)` for "31.4k" → 31400

### 2. `_log_context_telemetry()` in gateway.py
- [ ] Add function that calls get_session_metrics() and writes JSONL
- [ ] Target file: `{data_dir}/context-telemetry.jsonl`
- [ ] Include trigger reason and disk_size_kb in each record
- [ ] Run in background thread to avoid blocking watchdog/router
- [ ] Add periodic trigger in watchdog loop (every 5th cycle)

### 3. Error handler hooks in router.py + gateway.py
- [ ] router.py: Call telemetry BEFORE killing session on 400 (line ~456)
- [ ] router.py: Add 503 error detection alongside existing 400 detection
- [ ] gateway.py: Call telemetry on yellow zone entry
- [ ] gateway.py: Call telemetry before rotation in _rotate_orchestrator_session()

### 4. Raise KB safety net
- [ ] config.py: Change default from 500 to 2000
- [ ] .env: Update COPENCLAW_SESSION_MAX_SIZE_KB=2000

### 5. Validation
- [ ] Restart COpenClaw
- [ ] Verify periodic telemetry populates JSONL
- [ ] Verify 400 error triggers telemetry collection
- [ ] Check JSONL format is correct and parseable

## File Changes Summary
| File | Changes |
|------|---------|
| `src/copenclaw/integrations/copilot_cli.py` | Add `get_session_metrics()`, regex helpers |
| `src/copenclaw/core/gateway.py` | Add `_log_context_telemetry()`, wire into watchdog + rotation |
| `src/copenclaw/core/router.py` | Wire telemetry into 400 handler, add 503 detection |
| `src/copenclaw/core/config.py` | Raise session_max_size_kb default to 2000 |
| `.env` | Update COPENCLAW_SESSION_MAX_SIZE_KB |
