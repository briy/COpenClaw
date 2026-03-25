# Architecture

## System Overview

COpenClaw is a Python/FastAPI application that acts as a bidirectional bridge between chat messaging platforms and GitHub Copilot CLI. Inbound messages arrive via webhooks from five chat channels, get routed through a normalized command dispatcher, and are forwarded to Copilot CLI processes running on the host machine. Results stream back to the originating chat thread.

For long-running work, the system dispatches autonomous worker sessions (each a separate Copilot CLI process) with optional supervisor processes that monitor progress and intervene when needed. An MCP (Model Context Protocol) server exposes task management, scheduling, and messaging tools that Copilot CLI sessions call back into.

```
Chat Apps ──webhook──▶ FastAPI Gateway ──▶ Router ──▶ Copilot CLI (orchestrator)
                            │                              │
                            │                     ┌────────┴────────┐
                            │                     ▼                 ▼
                            │               WorkerPool         Supervisor
                            │              (Copilot CLI)      (Copilot CLI)
                            │                     │                 │
                            ◀──────── MCP Server ◀┘─────────────────┘
                            │         (JSON-RPC over HTTP)
                            ▼
                     Chat Apps (responses)
```

## Major Components

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| `src/copenclaw/core/gateway.py` | FastAPI application factory; webhook endpoints for all channels; lifespan management (startup/shutdown); health checks | FastAPI, Uvicorn |
| `src/copenclaw/core/router.py` | Normalizes inbound messages into `ChatRequest`; dispatches slash-commands (`/status`, `/tasks`, `/reset`, `/update`); handles task proposal approval/rejection; falls through to Copilot CLI for free-text | Dataclasses, regex |
| `src/copenclaw/core/worker.py` | `WorkerPool` manages background Copilot CLI sessions; creates isolated workspace directories per task with MCP config and system prompts; streams output to log files; watchdog monitors for idle/stuck workers | Threading, subprocess |
| `src/copenclaw/core/tasks.py` | `TaskManager` — task lifecycle state machine (proposed → approved → running → completed/failed/cancelled); bidirectional inter-tier communication (ITC) via `TaskMessage`; JSON file persistence | Dataclasses, JSON |
| `src/copenclaw/core/session.py` | `SessionStore` — per-channel/per-user session tracking with conversation history; JSON file persistence; configurable turn limits and context caps | JSON files |
| `src/copenclaw/core/scheduler.py` | `Scheduler` — one-shot and cron-based scheduled tasks; persisted to JSON; fires prompts through the orchestrator | croniter |
| `src/copenclaw/core/config.py` | `Settings` dataclass loaded from environment variables / `.env` file; channel credentials, timeouts, rate limits, workspace paths | python-dotenv |
| `src/copenclaw/mcp/server.py` | FastAPI router exposing MCP tools as HTTP endpoints; task dispatch/ITC tools, scheduling, messaging, audit, MCP server registry | FastAPI, Pydantic |
| `src/copenclaw/mcp/protocol.py` | `MCPProtocolHandler` — JSON-RPC 2.0 request/response handler; maps `tools/list` and `tools/call` to internal functions; implements all MCP tool logic | JSON-RPC |
| `src/copenclaw/integrations/copilot_cli.py` | `CopilotCli` class — launches `gh copilot` subprocess with correct args, MCP config, workspace dir, and model selection; streams stdout; handles `--model auto`, prompt-file fallback for long prompts | subprocess |
| `src/copenclaw/integrations/telegram.py` | `TelegramAdapter` — sends messages/images to Telegram via Bot API; chunking for long messages | httpx |
| `src/copenclaw/integrations/teams.py` | `TeamsAdapter` — sends messages to Microsoft Teams via Bot Framework | httpx |
| `src/copenclaw/integrations/whatsapp.py` | `WhatsAppAdapter` — sends messages via WhatsApp Cloud API | httpx |
| `src/copenclaw/integrations/signal.py` | `SignalAdapter` — sends messages via Signal REST API | httpx |
| `src/copenclaw/integrations/slack.py` | `SlackAdapter` — sends messages via Slack Web API | httpx |
| `src/copenclaw/core/templates.py` | System prompt templates for orchestrator, worker, and supervisor roles | String templates |
| `src/copenclaw/core/repair.py` | Self-healing: detects startup/runtime failures and auto-dispatches recovery tasks | JSON state files |
| `src/copenclaw/core/audit.py` | Append-only audit log for all system events | JSON files |
| `src/copenclaw/core/rate_limit.py` | `RateLimiter` — per-sender sliding-window rate limiting for webhook endpoints | In-memory |
| `src/copenclaw/core/mcp_registry.py` | Manages user-installed MCP servers in `~/.copilot/mcp-config.json`; merges into worker configs | JSON |
| `src/copenclaw/cli.py` | Typer CLI entry point (`copenclaw serve`, `copenclaw update`, etc.) | Typer |

## Data Flow

### Inbound Message (Chat → Copilot CLI)

1. **Webhook** — Chat platform sends HTTP POST to `/webhook/telegram`, `/api/messages` (Teams), `/webhook/whatsapp`, `/webhook/signal`, or `/webhook/slack`.
2. **Authentication** — Sender ID checked against per-channel allowlist; Teams tokens validated via `teams_auth.py`.
3. **Normalization** — Gateway extracts text, sender_id, chat_id, and constructs a `ChatRequest` dataclass.
4. **Routing** — `router.handle_chat()` checks for slash-commands, task approval patterns, or falls through to Copilot CLI.
5. **Copilot CLI** — `CopilotCli.run()` launches a `gh copilot` subprocess with the orchestrator system prompt, MCP config, and user message. Output is streamed.
6. **Response** — Streamed output is sent back to the chat channel via the appropriate adapter.

### Task Dispatch (Orchestrator → Worker)

1. **Proposal** — Orchestrator calls `tasks_propose` MCP tool → `TaskManager` creates task in `proposed` state → user asked to approve.
2. **Approval** — User replies "Yes" → `tasks_approve` called → task moves to `approved`.
3. **Worker Launch** — `WorkerPool.launch()` creates isolated workspace directory, writes MCP config pointing to COpenClaw's MCP server, writes worker system prompt to `.github/copilot-instructions.md`, launches `gh copilot` subprocess.
4. **Execution** — Worker Copilot CLI session runs autonomously, calling MCP tools (`task_report`, `task_get_context`) to communicate progress.
5. **Supervisor** (optional) — A second Copilot CLI session monitors worker logs via `task_read_peer` and can send guidance via `task_send_input`.
6. **Completion** — Worker calls `task_report(type="completed")` → `TaskManager` updates state → user notified via chat.

### MCP Tool Calls (Copilot CLI → COpenClaw)

Copilot CLI sessions (orchestrator, worker, supervisor) make HTTP POST requests to COpenClaw's MCP server endpoint. The `MCPProtocolHandler` processes JSON-RPC 2.0 requests, dispatches to the appropriate tool handler, and returns JSON-RPC responses. Tools include task management, scheduled tasks, messaging, audit log, file access, and MCP server registry management.

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Language | Python 3.10+ | Chosen for rapid development, subprocess management, and Copilot CLI compatibility |
| Web Framework | FastAPI + Uvicorn | Async-capable, fast, Pydantic validation, automatic OpenAPI docs |
| AI Backend | GitHub Copilot CLI (`gh copilot`) | All AI reasoning, code gen, tool execution delegated to Copilot |
| Inter-process | MCP (Model Context Protocol) over HTTP | JSON-RPC 2.0; Copilot CLI calls back into COpenClaw |
| Persistence | JSON files | Sessions, tasks, audit log, scheduler state — stored in `.data/` |
| CLI | Typer | `copenclaw serve`, `copenclaw update` commands |
| Scheduling | croniter | Cron expression parsing for recurring scheduled tasks |
| HTTP Client | httpx | Async HTTP for all outbound chat API calls |
| Auth | PyJWT | Teams bearer token validation |
| Config | python-dotenv | `.env` file for secrets and settings |
| Build | Hatchling | PEP 517 build backend; editable install support |

## External Dependencies

| Dependency | Purpose | Risk Level |
|-----------|---------|------------|
| GitHub Copilot CLI (`gh copilot`) | Core AI engine — all code gen, reasoning, tool execution | **High** — entire system depends on it; breaking changes in `gh` CLI would require adaptation |
| Telegram Bot API | Chat channel | Low — stable, well-documented |
| Microsoft Bot Framework | Teams channel | Medium — complex auth flow |
| WhatsApp Cloud API (Meta) | Chat channel | Low — standard REST API |
| Signal REST API | Chat channel | Low — self-hosted signal-cli-rest-api |
| Slack Web API | Chat channel | Low — stable, well-documented |
| GitHub API | PR creation, issue management (via Copilot CLI) | Low — used indirectly through `gh` CLI |

## Key Constraints and Trade-offs

1. **Single-user, single-machine** — COpenClaw runs on one machine for one user. This simplifies security (no multi-tenant auth) but limits scalability. Trade-off: simplicity over scale.
2. **Copilot CLI dependency** — Delegating all AI to Copilot CLI means COpenClaw is minimal (~3K lines), but any Copilot CLI outage or breaking change directly impacts the system. Trade-off: simplicity over independence.
3. **JSON file persistence** — Sessions, tasks, and audit logs are stored as JSON files. This avoids database dependencies but limits query capabilities and concurrent access. Trade-off: zero-dependency persistence over query power (SQLite migration planned).
4. **No web UI** — Smaller attack surface and simpler deployment, but no visual dashboard for monitoring. Trade-off: security over discoverability.
5. **Subprocess-based workers** — Each worker is a separate `gh copilot` process. This provides strong isolation but means no shared memory between workers. Trade-off: isolation over efficiency.

## Open Questions

1. **SQLite migration scope** — Should the session store, task store, and audit log all migrate to SQLite simultaneously, or incrementally?
2. **Context management strategy** — How to implement lossless DAG-based summarization for long-running sessions without exceeding Copilot CLI's context window?
3. **Multi-machine support** — Is there a future need to support distributed workers across multiple machines?
