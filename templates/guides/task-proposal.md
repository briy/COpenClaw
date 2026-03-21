# Guide: Task Proposals and Worker Management

Read this file before calling tasks_propose or tasks_create.

## Writing a Worker Prompt

The worker is an independent Copilot CLI session — it cannot see your conversation history. Everything it needs must be in the prompt. Include:

- Exact requirements and acceptance criteria
- Technology preferences the user mentioned
- File/folder conventions (project subfolder name, etc.)
- Constraints (no interactive commands, no root-level files)
- Step-by-step plan if the task is complex
- Git repo URL to clone (if working on an existing repo)
- Branch name to work on (create a feature branch if appropriate)
- Instruction to create and maintain a PLAN.md in the project root

PLAN.md is the primary checkpoint mechanism — if the worker's session dies, a new worker reads PLAN.md and resumes. It must be self-contained: objective, steps, current status, key decisions, file locations.

## Writing Supervisor Instructions

Supervisor checks on the worker periodically. Include:
- Goal and acceptance criteria
- What to review in the code
- Verify the worker is making incremental commits and pushing
- Verify PLAN.md exists and is being updated at milestones

## Proposal Response Format (show the user before they approve)

Present proposals like this (no markdown, Telegram-friendly):

📋 Proposed Task: "task-name" (task-id)

Worker Instructions:
[Full prompt or clear summary if very long]

Plan:
- Step 1
- Step 2
- ...

Supervisor: ✅ Enabled (checks every 5m)
Supervisor Focus: [What the supervisor watches for]

Reply Yes to approve or No to reject.

## tasks_propose vs tasks_create

- tasks_propose: Use for user-initiated complex work. Sends a proposal for approval.
- tasks_create: Use ONLY for on_complete hooks, scheduled job actions, or when the user explicitly said "just do it". Dispatches immediately without approval.

## Continuing or Redirecting Existing Tasks

When the user wants to update, continue, or redirect a running or completed task:
1. Use tasks_list to find the task
2. Use tasks_send with the task ID
   - Running tasks: msg_type "instruction" delivers to worker/supervisor inbox
   - Stopped tasks (completed/failed/cancelled): tasks_send auto-resumes the task with a new worker

Do NOT propose a new task when the user is clearly referring to an existing one.

## Task Chaining with on_complete

Set an on_complete prompt on any task. When the task reaches any terminal state (success, failure, cancellation), the system feeds the on_complete prompt to you. You can then use tasks_create to spawn follow-up tasks without user approval — the user pre-authorized this by setting the hook.

Example — iterative improvement loop:
  on_complete: "Review the results. Identify the next most impactful improvements. Use tasks_create to spawn another improvement task with its own on_complete to continue the cycle."

## Task Lifecycle

proposed → [user approves] → running → completed / failed / cancelled
                                ↕
                          paused / needs_input

- proposed: awaiting user approval
- running: worker executing
- completed: worker finished successfully
- failed: worker hit unrecoverable error
- cancelled: user or orchestrator cancelled
- needs_input: worker blocked, needs human decision
