# Implementation Plan

## Overview

COpenClaw's core orchestration layer is functional: all five chat channels work, task lifecycle is complete, worker/supervisor system is operational, and self-healing is in place. The next phases focus on hardening persistence, improving context management for long-running sessions, adding agent recall capabilities, and enabling autonomous ecosystem monitoring. Each phase builds on the previous, with SQLite migration as the foundation.

## Milestones

### M1 — SQLite Session Store

**Description:** Replace JSON file-based persistence with SQLite for sessions, tasks, and audit log. This eliminates data corruption risks from concurrent writes, enables efficient querying, and provides the foundation for the context management and recall features in M2 and M3.

**Key Deliverables:**
- [ ] SQLite database schema for sessions, tasks, messages, and audit events
- [ ] Migration script to convert existing JSON files to SQLite
- [ ] Updated `SessionStore` class with SQLite backend
- [ ] Updated `TaskManager` class with SQLite backend
- [ ] Updated audit module with SQLite backend
- [ ] Backward compatibility: detect and migrate JSON files on first startup

**Acceptance Criteria:**
- All existing tests pass with the SQLite backend
- Session data persists across process restarts
- Concurrent read/write operations do not corrupt data (WAL mode enabled)
- Migration script successfully converts existing `.data/` JSON files
- No performance regression: task dispatch latency remains under 500ms

**Dependencies:** None

---

### M2 — Lossless Context Management

**Description:** Implement DAG-based conversation summarization inspired by [lossless-claw](https://github.com/lossless-claw/lossless-claw). Instead of truncating conversation history when it exceeds context limits, summarize older turns into compressed nodes while preserving key facts, decisions, and code references. Store summaries in SQLite for retrieval.

**Key Deliverables:**
- [ ] Context DAG data model in SQLite (nodes = conversation turns, edges = dependencies)
- [ ] Summarization engine that compresses older turns while preserving key facts
- [ ] Context assembly: reconstruct relevant context for new Copilot CLI prompts from DAG
- [ ] Integration with `SessionStore` — automatic summarization when turn count exceeds threshold
- [ ] Configurable summarization strategy (aggressive, balanced, conservative)

**Acceptance Criteria:**
- Sessions with 50+ turns produce correct summaries that preserve all key decisions and code references
- Context assembly produces prompts that fit within Copilot CLI's context window
- Summarization runs in under 2 seconds for a 50-turn session
- No information loss for the most recent N turns (configurable, default 10)

**Dependencies:** M1 (requires SQLite backend)

---

### M3 — Agent Recall Tools

**Description:** Provide MCP tools that allow worker and supervisor Copilot CLI sessions to search past conversation history, completed task artifacts, and decision logs. Analogous to `lcm_grep` and `lcm_expand` from lossless-claw, but integrated into COpenClaw's MCP protocol.

**Key Deliverables:**
- [ ] `session_search` MCP tool — full-text search across session history (SQLite FTS5)
- [ ] `session_expand` MCP tool — retrieve full context of a specific session turn or summary node
- [ ] `task_search` MCP tool — search completed task logs, reports, and artifacts
- [ ] `decision_search` MCP tool — search decisions.md-style records across sessions
- [ ] Index builder that indexes session history and task artifacts into FTS5 tables

**Acceptance Criteria:**
- Workers can find relevant past sessions and task artifacts via natural-language queries
- Search results return in under 1 second for a database with 1,000+ sessions
- FTS5 index is automatically updated when sessions are modified
- All recall tools are registered in the MCP protocol handler and available to workers

**Dependencies:** M1, M2 (requires SQLite backend and context DAG)

---

### M4 — Autonomous Tech Radar

**Description:** Scheduled crawling and RSS monitoring of key ecosystem sources (OpenClaw, lossless-claw, Copilot CLI changelogs, Anthropic/OpenAI release notes) to auto-discover stability improvements, new features, and breaking changes. Results are summarized and optionally surfaced to the user via chat.

**Key Deliverables:**
- [ ] RSS/Atom feed parser for monitoring release notes and changelogs
- [ ] Configurable source list (YAML) with crawl intervals and content selectors
- [ ] Integration with `Scheduler` for periodic crawling
- [ ] Digest generation: summarize new findings into actionable briefs
- [ ] Chat notification: optionally push digest summaries to a configured chat channel
- [ ] Integration with ContentCrawler for deep-page content extraction

**Acceptance Criteria:**
- System detects new releases/changelogs within one crawl cycle of publication
- Digest summaries are concise (under 500 words) and highlight actionable items
- No duplicate notifications for previously seen content
- Crawl failures are logged but do not crash the system

**Dependencies:** M1 (requires SQLite for dedup tracking)

## Milestone Dependency Map

```
M1 → M2 → M3
M1 → M4
```

## Scope Notes

| Milestone | Scope | Notes |
|-----------|-------|-------|
| M1 | Medium | Core infrastructure change; touches session, tasks, and audit modules |
| M2 | Large | Novel summarization logic; requires careful design to preserve information |
| M3 | Medium | Builds on M1/M2 infrastructure; mostly MCP tool wiring |
| M4 | Medium | Largely independent; can proceed in parallel with M2/M3 after M1 |
