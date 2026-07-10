"""Regression tests for prompt spill-to-file behavior.

On Windows the Copilot CLI is launched via a ``.cmd``/``.bat``/``.ps1`` shim,
whose arg parser truncates a multi-line ``-p`` argument at the first newline.
Scheduled-task prompts (which are multi-line) therefore lost everything after
line 1.  ``_prompt_needs_spill`` must return True for any multi-line prompt so
the full content is passed via a temp file instead.
"""

from copenclaw.integrations.copilot_cli import CopilotCli


def test_single_line_short_prompt_stays_inline() -> None:
    assert CopilotCli._prompt_needs_spill("hello world", base_cmd_len=200) is False


def test_multiline_short_prompt_spills() -> None:
    prompt = "You are monitoring Alaska cruise deals.\n\nSCENARIO 1: 3 guests, 1 cabin"
    # Short enough to fit inline, but multi-line → must spill to preserve lines 2+.
    assert CopilotCli._prompt_needs_spill(prompt, base_cmd_len=200) is True


def test_carriage_return_prompt_spills() -> None:
    assert CopilotCli._prompt_needs_spill("line1\r\nline2", base_cmd_len=10) is True


def test_long_single_line_prompt_spills() -> None:
    long_prompt = "x" * 7000
    assert CopilotCli._prompt_needs_spill(long_prompt, base_cmd_len=200, headroom=7000) is True


def test_headroom_boundary_single_line() -> None:
    # base(100) + len(prompt) + 10 must exceed headroom(7000) to spill.
    prompt = "y" * 6800
    assert CopilotCli._prompt_needs_spill(prompt, base_cmd_len=100, headroom=7000) is False
    prompt_over = "y" * 6900
    assert CopilotCli._prompt_needs_spill(prompt_over, base_cmd_len=100, headroom=7000) is True
