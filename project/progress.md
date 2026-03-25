# Progress

## Log

### 2026-03-24

**Summary:** Fixed missing `channel_type` field on `ChatRequest` dataclass causing Telegram handler crash.

**Details:**
- Added `channel_type: Optional[str] = None` field to `ChatRequest` in `src/copenclaw/core/router.py`
- Fixed `AttributeError` that occurred when Telegram messages were processed
- All 349 tests pass

**Commits/PRs:**
- `5e42b64` — fix: add missing channel_type field to ChatRequest dataclass
- PR #3: https://github.com/briy/COpenClaw/pull/3

---

### 2026-03-23

**Summary:** Multiple stability and reliability improvements: auto-recovery from CAPIError, /reset command, Windows command-line limit fix, stale file references cleanup, timeout separation, and test fixture fix.

**Details:**
- Separated orchestrator timeout from worker timeout for independent tuning
- Fixed `tasks_approve` tool description to prevent orchestrator from calling it directly
- Added auto-recovery from CAPIError 400 tool_use/tool_result corruption
- Added `/reset` command to clear session context without restarting
- Fixed Windows 8191-char command-line limit for long prompts (switched to temp file injection)
- Removed stale `files.read` references from `server.py`

**Commits/PRs:**
- `497ea95` — feat: separate orchestrator timeout from worker timeout
- `1289d3c` — Fix tasks_approve tool description and broken test fixture
- `5649c8b` — fix: auto-recover from CAPIError 400 tool_use/tool_result corruption
- `94815f3` — feat: add /reset command to clear session context without restarting
- `65148bf` — fix: avoid Windows 8191-char command-line limit for long prompts
- `8c7816a` — Remove stale files.read references from server.py

---

### 2026-03-16

**Summary:** Added `--model auto` to worker CLI launch command.

**Details:**
- Modified `worker.py` to pass `--model auto` when spawning Copilot CLI worker sessions
- Enables automatic model selection by Copilot CLI

**Commits/PRs:**
- `90e3293` (via task-520d79ccef61)

---

### 2026-03-15

**Summary:** Major reliability hardening: explicit user approval for task proposals, CLI-only Copilot enforcement, startup resilience, and runtime UX improvements.

**Details:**
- Implemented explicit user approval flow for `tasks_propose` with approval tokens
- Enforced CLI-only Copilot execution path (no API fallback)
- Hardened starter marker and shutdown edge cases
- Auto-dispatch startup recovery tasks
- Fixed startup console prompt collisions
- Improved runtime UX and recovery flow
- Hardened startup probe log and health checks

**Commits/PRs:**
- `ee5736e` — Merge PR #22 (startup-starter-serve-resilience)
- `dab579c` — Merge PR #23 (proposal-explicit-user-approval)
- `17da64c` — Enforce CLI-only Copilot execution path
- `df90828` — Require explicit user approval for proposed tasks
- `abe1fb2` — Auto-dispatch startup recovery tasks
- `f38e434` — Improve runtime UX and recovery flow

---

### 2026-03-14

**Summary:** Major architecture overhaul: template system, worker messaging redesign, continuous task protocol, and Windows auto-heal.

**Details:**
- Split system prompts into templates subfolders (orchestrator, worker, supervisor)
- Redesigned worker messaging to relaunch resumed sessions with injected context
- Added continuous task (continuous_improvement) protocol and templates
- Added Windows auto-heal flow and runtime response safeguards
- Renamed jobs MCP tools to `scheduled_tasks`
- Clarified orchestrator stop/wait behavior
- Revised `tasks_propose` proposal UX

**Commits/PRs:**
- `13ebcfd` — Split prompts into templates subfolders
- `931f4b7` — Redesign worker messaging to relaunch resumed sessions
- `81c5464` — Add continuous task protocol and templates
- `932ae65` — Add Windows auto-heal flow and runtime response safeguards
- `734d8b6` — Remove continuous_task task type and templateize launch prompts
- `f0440a0` — Rename jobs MCP tools to scheduled_tasks

---

### 2026-02-22–23

**Summary:** Continuous mission chaining, supervisor observability, and API-first session defaults.

**Details:**
- Added autonomous continuous-task mission chaining
- Improved watchdog progress summaries for supervisor updates
- Reduced supervisor interventions and added process observability
- Defaulted Copilot sessions to API-first autopilot mode
- Fixed task-session resume for orchestrator

**Commits/PRs:**
- `e2ca37e` — Add autonomous continuous-task mission chaining
- `0db9f80` — Reduce supervisor interventions and add process observability
- `3e92d65` — feat: default Copilot sessions to API-first autopilot
- `2c5afb8` — Merge PR #20 (continuous-mission-autochain)
- `91b5e1b` — Merge PR #18 (supervisor-passive-observability)

---

### 2026-02-15–16

**Summary:** Continuous improvement task type, Windows installer fixes, updater lock handling.

**Details:**
- Designed and implemented continuous improvement task type with budget/iteration tracking
- Fixed Windows installer PATH parse crash during winget flow
- Fixed Windows updater lock handling and installer updates
- Improved docstrings and logging for atomic write failures

**Commits/PRs:**
- `f9ec20d` — Merge PR #12 (continuous-improvement-task-type-design)
- `2efae6a` — Merge PR #16 (windows-installer-winget-path-parser)
- `1767c78` — Merge PR #15 (windows-updater-lock-deferred-install)

---

<!-- Add new entries above this line -->
