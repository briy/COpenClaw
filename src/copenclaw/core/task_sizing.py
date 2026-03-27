"""Pre-dispatch task sizing and auto-decomposition.

Estimates whether a task fits in a single Copilot CLI session and,
if not, decomposes it into sub-tasks.  All sizing decisions are
deterministic Python (configurable thresholds via env vars).
The decomposition step uses one LLM call to break work into chunks.

STATUS: Built but NOT wired into dispatch flow.  Enable by calling
``check_and_decompose()`` before ``WorkerPool.start_worker()``.
See GitHub issue for activation timeline.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Configurable thresholds (env vars) ─────────────────────────

# Prompt length above which we flag as potentially oversized
_DEFAULT_PROMPT_CHAR_LIMIT = 30_000

# Estimated tokens per prompt char (conservative)
_CHARS_PER_TOKEN = 4

# Max estimated prompt tokens before triggering decomposition
_DEFAULT_PROMPT_TOKEN_LIMIT = 8_000

# If historical telemetry shows similar tasks averaged above this
# many turns, flag for decomposition
_DEFAULT_TURN_LIMIT = 40

# Max sub-tasks when decomposing
_DEFAULT_MAX_SUBTASKS = 5


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


@dataclass
class TaskSizeEstimate:
    """Result of pre-dispatch sizing check."""

    prompt_chars: int
    estimated_prompt_tokens: int
    file_references: int
    repo_references: int
    fits_single_session: bool
    reasons: List[str] = field(default_factory=list)

    # Thresholds used (for transparency/debugging)
    prompt_char_limit: int = 0
    prompt_token_limit: int = 0

    def to_dict(self) -> dict:
        return {
            "prompt_chars": self.prompt_chars,
            "estimated_prompt_tokens": self.estimated_prompt_tokens,
            "file_references": self.file_references,
            "repo_references": self.repo_references,
            "fits_single_session": self.fits_single_session,
            "reasons": self.reasons,
            "prompt_char_limit": self.prompt_char_limit,
            "prompt_token_limit": self.prompt_token_limit,
        }


@dataclass
class SubTask:
    """A decomposed piece of a larger task."""

    title: str
    prompt: str
    depends_on: List[str] = field(default_factory=list)
    estimated_complexity: str = "medium"  # low / medium / high


@dataclass
class DecompositionResult:
    """Result of auto-decomposing an oversized task."""

    original_prompt: str
    sub_tasks: List[SubTask]
    decomposition_reason: str

    def to_dict(self) -> dict:
        return {
            "original_prompt_chars": len(self.original_prompt),
            "sub_task_count": len(self.sub_tasks),
            "decomposition_reason": self.decomposition_reason,
            "sub_tasks": [
                {
                    "title": st.title,
                    "prompt_chars": len(st.prompt),
                    "depends_on": st.depends_on,
                    "estimated_complexity": st.estimated_complexity,
                }
                for st in self.sub_tasks
            ],
        }


# ── Sizing (pure Python, no LLM) ──────────────────────────────

def _count_file_references(prompt: str) -> int:
    """Count likely file path references in a prompt."""
    import re
    # Match patterns like src/foo/bar.py, ./thing.ts, C:\path\file.ext
    patterns = re.findall(
        r'(?:[a-zA-Z]:\\|\.?/)?[\w./-]+\.\w{1,6}',
        prompt,
    )
    # Filter noise (URLs, version numbers, etc.)
    extensions = {'.py', '.ts', '.js', '.tsx', '.jsx', '.md', '.yaml', '.yml',
                  '.json', '.toml', '.cfg', '.ini', '.html', '.css', '.sql',
                  '.sh', '.bat', '.ps1', '.rs', '.go', '.java', '.cs', '.rb'}
    return len([p for p in patterns if any(p.endswith(ext) for ext in extensions)])


def _count_repo_references(prompt: str) -> int:
    """Count likely GitHub repo references (owner/repo patterns)."""
    import re
    return len(re.findall(r'\b[\w-]+/[\w.-]+\b', prompt))


def estimate_task_size(prompt: str) -> TaskSizeEstimate:
    """Estimate whether a task prompt fits in a single CLI session.

    Pure Python — no LLM calls.  Uses configurable thresholds.
    """
    prompt_char_limit = _get_int_env("COPENCLAW_TASK_PROMPT_CHAR_LIMIT", _DEFAULT_PROMPT_CHAR_LIMIT)
    prompt_token_limit = _get_int_env("COPENCLAW_TASK_PROMPT_TOKEN_LIMIT", _DEFAULT_PROMPT_TOKEN_LIMIT)

    prompt_chars = len(prompt)
    estimated_tokens = prompt_chars // _CHARS_PER_TOKEN
    file_refs = _count_file_references(prompt)
    repo_refs = _count_repo_references(prompt)

    reasons = []
    fits = True

    if prompt_chars > prompt_char_limit:
        fits = False
        reasons.append(f"Prompt too long: {prompt_chars:,} chars (limit: {prompt_char_limit:,})")

    if estimated_tokens > prompt_token_limit:
        fits = False
        reasons.append(f"Estimated tokens too high: {estimated_tokens:,} (limit: {prompt_token_limit:,})")

    if file_refs > 15:
        fits = False
        reasons.append(f"Too many file references: {file_refs} (suggests multi-file task)")

    if repo_refs > 3:
        fits = False
        reasons.append(f"Multiple repo references: {repo_refs} (suggests cross-repo task)")

    estimate = TaskSizeEstimate(
        prompt_chars=prompt_chars,
        estimated_prompt_tokens=estimated_tokens,
        file_references=file_refs,
        repo_references=repo_refs,
        fits_single_session=fits,
        reasons=reasons,
        prompt_char_limit=prompt_char_limit,
        prompt_token_limit=prompt_token_limit,
    )

    if not fits:
        logger.warning(
            "Task sizing: OVERSIZED — %d chars, ~%d tokens, %d files, %d repos. Reasons: %s",
            prompt_chars, estimated_tokens, file_refs, repo_refs, "; ".join(reasons),
        )
    else:
        logger.info(
            "Task sizing: OK — %d chars, ~%d tokens, %d files",
            prompt_chars, estimated_tokens, file_refs,
        )

    return estimate


# ── Decomposition (requires one LLM call) ─────────────────────

_DECOMPOSITION_PROMPT_TEMPLATE = """You are a task decomposition assistant. Break the following task into {max_subtasks} or fewer independent sub-tasks that can each be completed in a single Copilot CLI session (~40 turns, ~150K context tokens).

Rules:
- Each sub-task must be self-contained and independently executable
- Include clear entry and exit criteria for each sub-task
- Specify dependencies between sub-tasks (which must complete first)
- Estimate complexity as low/medium/high
- Return ONLY valid JSON matching this schema:

{{
  "sub_tasks": [
    {{
      "title": "short descriptive title",
      "prompt": "full task prompt for the worker",
      "depends_on": ["title of dependency if any"],
      "estimated_complexity": "low|medium|high"
    }}
  ]
}}

TASK TO DECOMPOSE:
{task_prompt}"""


def decompose_task(
    prompt: str,
    cli: "CopilotCli",
    max_subtasks: int = 0,
) -> Optional[DecompositionResult]:
    """Break an oversized task into sub-tasks using one LLM call.

    This is the ONE place where we use an LLM for infrastructure — because
    deciding how to split work requires understanding the work.

    Returns None if decomposition fails.
    """
    from copenclaw.integrations.copilot_cli import CopilotCli  # noqa: F811

    if max_subtasks <= 0:
        max_subtasks = _get_int_env("COPENCLAW_MAX_SUBTASKS", _DEFAULT_MAX_SUBTASKS)

    decomp_prompt = _DECOMPOSITION_PROMPT_TEMPLATE.format(
        max_subtasks=max_subtasks,
        task_prompt=prompt[:20_000],  # Cap to avoid blowing the decomposer's own context
    )

    try:
        raw = cli.run_prompt(
            decomp_prompt,
            log_prefix="DECOMPOSER",
            resume_id=None,  # Always fresh session for decomposition
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Decomposition LLM call failed: %s", exc)
        return None

    # Parse the JSON response
    try:
        # Find JSON block in the response (may be wrapped in markdown)
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start < 0 or json_end <= json_start:
            logger.error("No JSON found in decomposition response")
            return None

        parsed = json.loads(raw[json_start:json_end])
        sub_tasks_raw = parsed.get("sub_tasks", [])
        if not sub_tasks_raw:
            logger.error("Empty sub_tasks in decomposition response")
            return None

        sub_tasks = [
            SubTask(
                title=st.get("title", f"sub-task-{i}"),
                prompt=st.get("prompt", ""),
                depends_on=st.get("depends_on", []),
                estimated_complexity=st.get("estimated_complexity", "medium"),
            )
            for i, st in enumerate(sub_tasks_raw)
        ]

        result = DecompositionResult(
            original_prompt=prompt,
            sub_tasks=sub_tasks,
            decomposition_reason=f"Task exceeded sizing limits",
        )

        logger.info(
            "Decomposed task into %d sub-tasks: %s",
            len(sub_tasks),
            [st.title for st in sub_tasks],
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("Failed to parse decomposition JSON: %s", exc)
        return None


# ── Combined check (entry point for dispatch flow) ─────────────

def check_and_decompose(
    prompt: str,
    cli: "CopilotCli",
) -> tuple[bool, Optional[DecompositionResult]]:
    """Check task size and decompose if needed.

    Returns ``(fits, decomposition)``:
    - ``(True, None)`` — task fits, dispatch normally
    - ``(False, DecompositionResult)`` — task was oversized, here are sub-tasks
    - ``(False, None)`` — task was oversized but decomposition failed

    NOTE: Not yet wired into dispatch flow.  Call this before
    ``WorkerPool.start_worker()`` when ready to activate.
    """
    estimate = estimate_task_size(prompt)

    if estimate.fits_single_session:
        return (True, None)

    logger.info("Task flagged as oversized, attempting decomposition...")
    decomposition = decompose_task(prompt, cli)
    return (False, decomposition)
