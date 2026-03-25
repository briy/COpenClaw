# Vision

## Summary

COpenClaw is a multi-channel AI orchestrator that bridges chat applications (Telegram, Teams, WhatsApp, Signal, Slack) to GitHub Copilot CLI, enabling users to command a full agentic AI — capable of writing code, editing files, running shell commands, managing git, and building entire applications — from their phone or any messaging platform.

## Problem Statement

GitHub Copilot CLI is a powerful agentic AI that can autonomously write code, execute commands, manage repositories, and build applications. However, it requires direct terminal access on the host machine. Developers and technical users who want to trigger complex coding and DevOps workflows while away from their workstation — commuting, in meetings, or simply on their phone — have no way to do so.

Existing solutions like OpenClaw reimplement model routing, prompt engineering, and tool execution from scratch, resulting in large, complex codebases. There is no lightweight, self-improving bridge that simply leverages Copilot CLI's existing capabilities and makes them remote-controllable through everyday chat apps.

## Goals

1. **Remote AI access via chat** — Enable users to send natural-language instructions from any supported chat platform (Telegram, Teams, WhatsApp, Signal, Slack) and have Copilot CLI execute them on the host machine.
2. **Autonomous background execution** — Support multi-step, long-running tasks (code generation, refactoring, testing, deployment) that run autonomously with worker/supervisor oversight while the user goes about their day.
3. **Self-improving architecture** — Install in editable mode so the AI agent can read, modify, and improve COpenClaw's own source code, pushing improvements as pull requests upstream.
4. **Minimal codebase, maximum leverage** — Keep the orchestration layer under ~3,000 lines of Python by delegating all AI reasoning, code generation, and tool execution to Copilot CLI.
5. **Robust task lifecycle** — Provide task proposal, approval, dispatch, monitoring, inter-tier communication, and self-healing so that autonomous work is reliable and auditable.

## Non-Goals

1. **Not a general-purpose AI framework** — COpenClaw does not implement its own LLM inference, prompt engineering, or tool-calling protocol. It delegates entirely to Copilot CLI.
2. **Not a web dashboard** — There is no web UI or frontend. The only interfaces are chat channels and a localhost-only MCP endpoint.
3. **Not a multi-tenant SaaS** — COpenClaw runs on a single user's machine for that user's repositories. It is not designed for shared or hosted multi-user environments.
4. **Not a replacement for CI/CD** — While it can create GitHub Actions workflows and push code, it is not a deployment pipeline itself.

## Success Criteria

1. A user can send a natural-language message from Telegram, Teams, WhatsApp, Signal, or Slack and receive the result of Copilot CLI execution within the same chat thread.
2. Background tasks (code generation, refactoring, test-fix loops) complete autonomously with progress updates streamed to the chat channel.
3. The worker/supervisor system detects and recovers from at least 80% of transient failures (CAPIError, process crashes) without user intervention.
4. All task lifecycle events (proposal, approval, dispatch, progress, completion, failure) are logged in a persistent audit trail.
5. The codebase remains under 5,000 lines of Python (excluding tests and templates) while supporting all five chat channels.
