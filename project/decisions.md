# Decisions

## Decision Log

### 2026-03-14 — Template-Based System Prompts

**Context:** System prompts for orchestrator, worker, and supervisor sessions were previously inline strings in Python code. As prompts grew complex (especially with continuous improvement and inter-tier communication protocols), they became difficult to maintain and test.

**Options Considered:**
1. Inline strings in Python — Simple but hard to read, edit, and version separately
2. Markdown template files in a `templates/` directory — Easy to edit, diff, and test independently
3. Jinja2 templates — Maximum flexibility but adds a dependency

**Decision:** Markdown template files loaded by `src/copenclaw/core/templates.py`

**Rationale:** Templates are pure markdown, editable by both humans and AI agents. No additional dependency. The templates module provides simple string formatting for variable injection. Splitting by role (orchestrator, worker, supervisor) and session state (start, resume) keeps each template focused.

**Consequences:** System prompts are now first-class artifacts that can be reviewed in PRs. Workers and supervisors can be given different instructions without modifying Python code.

---

### 2026-03-15 — Explicit User Approval for Task Proposals

**Context:** The orchestrator could inadvertently approve its own task proposals when `tasks_propose` output was fed back into the same session. This violated the principle that only human users should authorize task execution.

**Options Considered:**
1. Sender ID allowlist — Simple but fragile; internal IDs could change
2. Approval tokens — Cryptographic proof that approval came through the user-facing chat flow
3. Separate approval API endpoint — Overengineered for the single-user model

**Decision:** Approval tokens issued by `tasks_propose`, required by `tasks_approve`

**Rationale:** Tokens ensure that only the chat router (triggered by a real user reply) can approve proposals. The orchestrator Copilot CLI session never receives the token, so it cannot self-approve. This was implemented in PR #23.

**Consequences:** `tasks_approve` now requires an `_approval_token` parameter. The orchestrator brain's tool description explicitly states it must not call `tasks_approve` directly.

---

### 2026-02-22 — Copilot CLI as Sole AI Backend

**Context:** Early design considered supporting multiple AI backends (OpenAI API, Anthropic API, local models). Copilot CLI was initially just one option.

**Options Considered:**
1. Multi-backend with provider abstraction — Maximum flexibility but massive complexity
2. OpenAI API direct — Full control but requires reimplementing tool calling, context management, code execution
3. Copilot CLI only — Delegates all AI complexity to GitHub's maintained toolchain

**Decision:** Copilot CLI as the sole AI backend, invoked as a subprocess

**Rationale:** Copilot CLI already implements model selection, context compression, tool calling (file read/write, shell execution, git operations), and multi-turn conversation management. Reimplementing any of this would balloon the codebase from ~3,000 lines to 10,000+. Copilot CLI's `--model auto` flag provides automatic model routing. The trade-off is complete dependency on GitHub's CLI availability and feature set.

**Consequences:** COpenClaw is non-functional without a working `gh copilot` installation. Model selection, token limits, and context management are opaque — COpenClaw cannot tune these directly.

---

### 2026-02-15 — JSON File Persistence (Interim)

**Context:** The system needed persistence for sessions, tasks, scheduled jobs, and audit logs from day one. The question was what storage backend to use.

**Options Considered:**
1. SQLite — Transactional, queryable, single-file, well-supported
2. JSON files — Zero dependencies, human-readable, easy to debug
3. Redis — Fast, good for sessions, but adds an external dependency
4. PostgreSQL — Overkill for a single-user local tool

**Decision:** JSON files for all persistence

**Rationale:** For the initial build, JSON files were the fastest path to working persistence with zero additional dependencies. Each store (sessions, tasks, scheduler) manages its own JSON file with atomic writes. This was explicitly chosen as an interim solution with SQLite migration planned as the first infrastructure milestone (M1).

**Consequences:** No concurrent read/write safety (mitigated by single-process architecture). Limited query capability — finding tasks by status requires loading all tasks. Planned migration to SQLite (see implementation-plan.md M1).

---

### 2026-02-22 — Python over TypeScript

**Context:** OpenClaw (the inspiration project) is written in TypeScript. COpenClaw needed to choose its implementation language.

**Options Considered:**
1. TypeScript/Node.js — Match OpenClaw's stack, good async support, large npm ecosystem
2. Python — FastAPI for HTTP, rich ecosystem, native subprocess management, simpler scripting
3. Go — Fast, good subprocess management, but less ecosystem for web frameworks

**Decision:** Python with FastAPI

**Rationale:** Python's subprocess module provides direct control over Copilot CLI process lifecycle. FastAPI delivers async HTTP with minimal boilerplate. The data science / AI ecosystem (even though COpenClaw doesn't do its own AI) means rich libraries for any future integration. Python's dynamic nature makes it easy for AI agents to modify the codebase — important for the self-improving design goal.

**Consequences:** Single-process, multi-threaded architecture (GIL present but acceptable since most work is I/O-bound subprocess management). Packaging via hatchling/pip.

---

### 2026-03-14 — MCP for Inter-Tier Communication

**Context:** Workers and supervisors need to communicate with the orchestrator (report progress, request input, send artifacts). The question was how to implement this inter-tier communication.

**Options Considered:**
1. File-based IPC — Write status files that the orchestrator polls
2. HTTP API — Workers call a REST API on the orchestrator
3. MCP (Model Context Protocol) — Workers call MCP tools, which are natively supported by Copilot CLI

**Decision:** MCP over HTTP (Streamable HTTP transport)

**Rationale:** Copilot CLI already supports MCP tool calling. By exposing task management operations as MCP tools (`task_report`, `task_get_context`, `task_check_inbox`, `task_send_input`), workers can communicate with the orchestrator using the same mechanism they use for all other tool calls. No additional client code needed in the worker — Copilot CLI handles the JSON-RPC protocol natively.

**Consequences:** All inter-tier communication goes through the MCP server on localhost. The MCP config for each worker/supervisor must include the correct task ID and endpoint URL. Tool schemas must be precise since Copilot CLI interprets them for function calling.

---

### 2026-03-14 — Worker/Supervisor Process Model

**Context:** Background tasks need to run autonomously for extended periods. The question was how to isolate and manage these long-running sessions.

**Options Considered:**
1. In-process async tasks — Share the main event loop; risk blocking the gateway
2. Thread-per-worker with subprocess — Each worker is a thread managing a Copilot CLI subprocess
3. Process-per-worker — Each worker is a separate Python process

**Decision:** Thread-per-worker with subprocess

**Rationale:** Each worker thread spawns and monitors a Copilot CLI subprocess. This provides process-level isolation for the AI (separate Copilot CLI sessions) while keeping worker management simple (thread join, log streaming, watchdog timers). The WorkerPool class manages the lifecycle. Supervisors are similarly thread+subprocess pairs that periodically review worker logs.

**Consequences:** Thread pool size limits concurrent tasks. A hung subprocess ties up a thread until the watchdog kills it. Worker logs are streamed to separate files for isolation.

---

<!-- Add new decisions above this line -->
