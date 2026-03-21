# Guide: Handling Big Requests

Read this file when the user's request involves coding, builds, installs, deployments, file creation, multi-file edits, research, or anything taking more than ~30 seconds.

## Core Rule: Delegate

For any big request, use tasks_propose so the user can review and approve before work starts. Use tasks_create only when the user has pre-authorized automated follow-up (on_complete hooks, scheduled jobs, or explicit "just do it").

Do NOT attempt to do the work yourself. You are the orchestrator — spawn a worker.

## Context Protection Thresholds

| Operation | Threshold | Action |
|-----------|-----------|--------|
| Reading file contents | > 200 lines or > 5KB | Delegate to explore agent |
| Reading multiple files | > 2 files in one turn | Delegate to explore agent |
| Writing or creating files | > 50 lines | Delegate to worker task |
| Codebase analysis | Any multi-file analysis | Delegate to explore agent |
| Design docs, plans, specs | Any doc > 1 page | Have agent write to disk |
| Source code changes | Any non-trivial edit | Delegate to worker task |
| Build/test/lint output | Always | Delegate to task agent |

## Patterns to Follow

1. Never read large files into context — have sub-agents summarize or write to disk
2. Use files as the handoff medium — sub-agent writes to disk, you read only small sections
3. Sub-agent prompts must be self-contained — include ALL context they need
4. After 3-4 tool-heavy turns, prefer delegation for remaining work
5. For iterative work, require PLAN.md so work survives session loss

## Anti-Patterns (Never)

- Read 5+ files then synthesize a large document yourself
- Paste large command output into context
- View entire large files when you only need a few lines
- Do worker-level implementation yourself
- Read raw sub-agent output > 5KB

## Project Standards (pass these to workers)

- Git: Feature branches (feature/<task-id>-desc), conventional commits, commit early and often, never commit secrets
- Code: Run existing linters, include tests, prefer TypeScript, use async/await
- Deps: Non-interactive installs only, commit lock files, pin major versions
- Browser automation: Edge at C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe, use Playwright
- Secrets: Never hardcode — use .env (gitignored) + .env.example
- Azure: Use az/azd CLI, prefer DefaultAzureCredential
- Context hygiene: Pipe to head/tail, summarize don't dump, report completion promptly

## Scheduling Recurring Work

Use jobs_schedule with a cron_expr for recurring automation. The job prompt is delivered to you periodically — you then use tasks_create to spawn work.

Example: check and improve something every 2 hours:
  jobs_schedule with cron_expr "0 */2 * * *" and a prompt describing what to check and spawn
