# COpenClaw Architectural Improvements

> Written 2026-03-25 as a result of a multi-day resilience audit.
> Goal: provide enough detail for a future agent (or developer) to implement each item independently.

---

## 1. Token-Bound Approval Matching

**Problem:** Approval inputs ("yes", "approve", "ok") are matched by positional regex against whatever pending action comes first (recovery → retry → proposal). If state is stale or multiple actions are pending, the wrong thing gets approved.

**Current code:** `src/copenclaw/core/router.py` lines 316–423 and `protocol.py` lines 1113–1134. Bare regex `r"^(yes|y|ok|approve)$"` matches first bucket.

**Target design:**
- When COpenClaw presents an approval prompt to the user, attach a unique short token (e.g., `[approve-a3f2]`).
- Store a mapping: `{token: (task_id, action_type, action_payload)}` in the task or session state.
- On user reply, extract the token and look up the exact action. If no token, reject with a clarification message listing pending actions with their tokens.
- Timeout stale tokens after 10 minutes.

**Files to change:**
- `router.py`: Replace regex match block (lines 316–423) with token lookup.
- `protocol.py`: When generating approval prompts, inject `[approve-XXXX]` token.
- `tasks.py`: Add `pending_approvals: dict[str, ApprovalAction]` to task state.

**Validation:** After implementation, send "yes" with two pending actions and verify it asks for disambiguation instead of approving the wrong one.

---

## 2. Delivery Confirmation Pipeline

**Problem:** Copilot CLI completes work correctly, but the response never reaches the user because Telegram delivery silently fails. `send_message()` in `telegram.py` (lines 80–97) drops remaining chunks on first failure and does not check Telegram's `{"ok": false}` JSON response. Main send paths in `gateway.py` (lines 1448, 1641) are not wrapped in try/except.

**Target design:**
- `send_message()` returns a `DeliveryResult` dataclass: `(success: bool, chunks_sent: int, chunks_total: int, error: str | None)`.
- All callers in `gateway.py` check the result. On failure:
  1. Log the full error with task context.
  2. Retry once after 2 seconds.
  3. If still failing, store the unsent message in a dead-letter queue (SQLite or JSON file).
  4. On next user interaction, check the dead-letter queue and replay unsent messages.
- Check Telegram's JSON response body for `{"ok": false}` — even when HTTP status is 200.
- Add a `/delivery-status` diagnostic endpoint that shows recent delivery failures.

**Files to change:**
- `telegram.py`: Return `DeliveryResult`, add retry logic, check `ok` field in response JSON.
- `gateway.py`: Wrap all `send_message()` calls, handle `DeliveryResult`, implement dead-letter store.
- New file: `src/copenclaw/core/dead_letter.py` — simple queue backed by SQLite or JSON.

**Validation:** Kill network briefly during a long response, verify message appears in dead-letter queue and replays on next interaction.

---

## 3. SQLite Session Store (Replace sessions.json)

**Problem:** `sessions.json` is an append-only audit log that is never read back for prompt assembly. `/reset` clears it, but the Copilot CLI session ID persists — causing context bleed. There's no queryable session history for diagnostics.

**Current code:** `src/copenclaw/core/session.py` lines 12–156. Hardcoded defaults: `max_turns=20`, `max_msg_chars=2000`, `max_context_chars=8000`. `build_context_prompt()` exists but is unused in production.

**Target design:**
- Replace `sessions.json` with a SQLite database (`sessions.db`) in the data directory.
- Schema:
  ```sql
  CREATE TABLE sessions (
      id TEXT PRIMARY KEY,           -- UUID
      channel TEXT NOT NULL,         -- "telegram", "teams", "terminal"
      channel_user_id TEXT,
      copilot_session_id TEXT,       -- The Copilot CLI --resume ID
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      ended_at TIMESTAMP,
      status TEXT DEFAULT 'active'   -- active, ended, reset
  );

  CREATE TABLE messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT REFERENCES sessions(id),
      role TEXT NOT NULL,            -- "user", "assistant", "system"
      content TEXT NOT NULL,
      token_estimate INTEGER,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
  );
  ```
- On `/reset`: mark session as `ended`, clear `copilot_session_id`, and start a fresh session. **Also** pass `--new-session` (or equivalent) to Copilot CLI on next invocation instead of `--resume`.
- Make `max_turns`, `max_msg_chars`, `max_context_chars` configurable via env vars.
- Use `build_context_prompt()` to assemble context from the last N messages when starting new CLI sessions.

**Files to change:**
- `session.py`: Replace JSON file I/O with SQLite via `sqlite3` stdlib.
- `router.py`: On `/reset`, call `session_store.end_session()` and clear the CLI resume ID.
- `copilot_cli.py`: Respect session end — use `--new-session` flag when session was reset.
- `config.py`: Add env vars for `max_turns`, `max_msg_chars`, `max_context_chars`.

**Validation:** `/reset` should produce a truly clean session with no context from prior conversation.

---

## 4. Context Injection (Summary Preamble)

**Problem:** COpenClaw cannot see or control the Copilot CLI context window. It relies entirely on `--resume` for continuity, which means it has no way to recover from context bloat, compaction failures, or session loss. OpenClaw solves this by controlling prompt assembly end-to-end.

**Target design:**
- Before each CLI invocation, COpenClaw assembles a **summary preamble** from recent session history (last 5–10 messages + any active task state).
- This preamble is written into the `copilot-instructions.md` file alongside the worker/orchestrator prompt.
- Format: a `## Session Context` section with bullet points summarizing what's been discussed.
- If the total preamble + instructions exceed a size threshold (e.g., 50K chars), truncate the preamble (oldest messages first) rather than the instructions.
- This gives Copilot CLI enough context to be useful even without `--resume`, and makes `/reset` + fresh session viable without losing conversational continuity.

**Dependencies:** Requires SQLite session store (#3) to efficiently query recent messages.

**Files to change:**
- `worker.py`: In `worker_template()`, add a `session_context` parameter and inject it into the instructions file.
- `copilot_cli.py`: In `_run_prompt_cli()`, build context preamble from session store before writing instructions.
- `session.py`: Add `get_recent_messages(session_id, limit=10)` method.

**Validation:** Start a conversation, `/reset`, start again — the bot should retain awareness of the prior topic through the preamble.

---

## 5. Worker Concurrency Cap

**Problem:** `WorkerPool` (in `worker.py`) enforces "one worker per task" but has no global `max_workers` cap. A user dispatching many tasks can exhaust system resources (each worker spawns a Copilot CLI subprocess with its own context window).

**Current code:** `WorkerPool.start_worker()` at lines 949–1031. No pool-level concurrency limit.

**Target design:**
- Add `COPENCLAW_MAX_WORKERS` env var (default: 5).
- `WorkerPool.start_worker()` checks `len(self._active_workers) >= max_workers` before spawning.
- If at capacity, return a clear error: "Worker limit reached (5/5). Complete or cancel a task first."
- Add a `/workers` diagnostic endpoint showing active worker count, task IDs, and uptime.
- Consider a priority queue: if at capacity, queue the request and start it when a slot opens (stretch goal).

**Files to change:**
- `worker.py`: Add `max_workers` to `WorkerPool.__init__()`, enforce in `start_worker()`.
- `config.py`: Add `COPENCLAW_MAX_WORKERS` env var.
- `gateway.py`: Add `/workers` endpoint.

**Validation:** Start 6 tasks with `max_workers=5`, verify the 6th gets a clear rejection message.

---

## Priority Order

| # | Improvement | Impact | Effort | Dependencies |
|---|------------|--------|--------|--------------|
| 2 | Delivery confirmation | Critical — fixes silent failures | Medium | None |
| 1 | Token-bound approvals | High — prevents wrong-action bugs | Medium | None |
| 5 | Worker concurrency cap | High — prevents resource exhaustion | Low | None |
| 3 | SQLite session store | Medium — enables other improvements | Medium | None |
| 4 | Context injection | Medium — resilience to context loss | Medium | #3 |

Items 1, 2, and 5 can be implemented independently and in parallel.
Item 4 depends on item 3.

---

## Quick Reference: Key File Locations

| File | Purpose |
|------|---------|
| `src/copenclaw/core/gateway.py` | Main FastAPI app, webhook handlers, watchdog |
| `src/copenclaw/core/router.py` | Message routing, approval matching |
| `src/copenclaw/core/worker.py` | Worker dispatch, WorkerPool |
| `src/copenclaw/core/session.py` | Session store (currently JSON) |
| `src/copenclaw/core/config.py` | Settings with env var mappings |
| `src/copenclaw/core/tasks.py` | Task lifecycle, progress reporting |
| `src/copenclaw/core/protocol.py` | Protocol messages, approval prompts |
| `src/copenclaw/integrations/telegram.py` | Telegram send/receive adapter |
| `src/copenclaw/integrations/copilot_cli.py` | Copilot CLI subprocess wrapper |
