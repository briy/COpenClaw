# Definition of Done

## Completion Criteria

1. [x] All five chat channels (Telegram, Teams, WhatsApp, Signal, Slack) accept inbound messages and return Copilot CLI responses in the same thread.
2. [x] Slash-commands (`/status`, `/tasks`, `/reset`, `/update`) are parsed and handled by the router before falling through to Copilot CLI.
3. [x] Task proposal → approval → dispatch → progress → completion lifecycle works end-to-end with user confirmation required before execution.
4. [x] Workers run in isolated workspace directories with their own MCP config and system prompts; worker output is streamed to per-task log files.
5. [x] Supervisor sessions can monitor worker logs (`task_read_peer`) and send corrective guidance (`task_send_input`).
6. [ ] Session persistence survives process restarts without data loss (currently JSON files; SQLite migration planned).
7. [ ] Context management preserves full conversation history across long-running sessions without exceeding model context limits.
8. [x] Self-healing: the system detects startup/runtime failures and auto-dispatches recovery tasks.
9. [x] Audit trail captures all task lifecycle events, chat messages, and MCP tool calls.
10. [x] Rate limiting prevents abuse on all webhook endpoints.
11. [x] `copenclaw serve` starts the full system (FastAPI + MCP + webhook endpoints) with a single command.
12. [ ] Test coverage exceeds 60% for core modules (router, tasks, worker, session, MCP protocol).

## Operational Criteria

1. [x] Application starts via `copenclaw serve` and is accessible on the configured port.
2. [x] README.md contains installation instructions, channel setup guides, and usage examples.
3. [ ] All tests pass in CI (GitHub Actions workflow).
4. [x] No critical or high-severity bugs remain open (channel_type fix merged 2026-03-24).

## Stretch Goals

1. [ ] SQLite-backed session store with migration from JSON files.
2. [ ] DAG-based lossless context summarization for infinite conversation support.
3. [ ] Agent recall tools — MCP tools for workers to search conversation history and past session artifacts.
4. [ ] Autonomous tech radar — scheduled crawling/RSS monitoring for ecosystem updates.
