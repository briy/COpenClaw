#!/usr/bin/env python3
"""
generate-context-brief.py — Extract focused context briefs for large-file edits.

Usage:
    python generate-context-brief.py --file path/to/file.py --target func_name --pattern ref_func
    python generate-context-brief.py --file path/to/file.py --target func_name --pattern ref_func --description "Add X feature"
    python generate-context-brief.py --file path/to/file.py --target func_name --pattern ref_func -o brief.md

The output is a Markdown context brief following the Foundry context-brief-standard.
"""

import argparse
import ast
import re
import sys
from datetime import date
from pathlib import Path


def find_functions_python(source: str) -> dict[str, dict]:
    """Use Python AST to find all top-level function definitions with line ranges."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    lines = source.splitlines()
    functions = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno or start
            functions[node.name] = {
                "name": node.name,
                "start": start,
                "end": end,
                "source": "\n".join(lines[start - 1 : end]),
                "args": [arg.arg for arg in node.args.args],
                "decorators": [
                    ast.dump(d) for d in node.decorator_list
                ],
                "docstring": ast.get_docstring(node) or "",
            }

    return functions


def find_functions_regex(source: str, ext: str) -> dict[str, dict]:
    """Regex-based fallback for non-Python files."""
    lines = source.splitlines()
    functions = {}

    # Language-specific patterns
    patterns = {
        ".ts": r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        ".tsx": r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        ".js": r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        ".jsx": r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
        ".go": r"^func\s+(?:\([^)]*\)\s+)?(\w+)",
        ".rs": r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)",
        ".java": r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(",
        ".cs": r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:async\s+)?(?:\w+\s+)+(\w+)\s*\(",
        ".rb": r"^\s*def\s+(\w+)",
    }

    pattern = patterns.get(ext)
    if not pattern:
        return {}

    current_func = None
    current_start = None

    for i, line in enumerate(lines, 1):
        match = re.match(pattern, line)
        if match:
            # Close previous function
            if current_func:
                functions[current_func]["end"] = i - 1
                functions[current_func]["source"] = "\n".join(
                    lines[functions[current_func]["start"] - 1 : i - 1]
                )
            current_func = match.group(1)
            current_start = i
            functions[current_func] = {
                "name": current_func,
                "start": i,
                "end": len(lines),
                "source": "",
                "args": [],
                "decorators": [],
                "docstring": "",
            }

    # Close last function
    if current_func:
        functions[current_func]["end"] = len(lines)
        functions[current_func]["source"] = "\n".join(
            lines[functions[current_func]["start"] - 1 :]
        )

    return functions


def find_functions(source: str, filepath: Path) -> dict[str, dict]:
    """Find functions using AST for Python, regex for others."""
    ext = filepath.suffix.lower()
    if ext == ".py":
        result = find_functions_python(source)
        if result:
            return result
    return find_functions_regex(source, ext)


def extract_context_lines(lines: list[str], center: int, radius: int = 5) -> str:
    """Extract lines around a center point with line numbers."""
    start = max(0, center - radius - 1)
    end = min(len(lines), center + radius)
    result = []
    for i in range(start, end):
        result.append(f"{i + 1:>4}. {lines[i]}")
    return "\n".join(result)


def generate_brief(
    filepath: Path,
    target_names: list[str],
    pattern_names: list[str],
    description: str = "",
) -> str:
    """Generate a context brief for the given file and functions."""
    source = filepath.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines()
    total_lines = len(lines)
    ext = filepath.suffix.lower()
    lang = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".cs": "csharp",
        ".rb": "ruby",
    }.get(ext, "")

    functions = find_functions(source, filepath)

    brief_parts = []

    # Header
    target_label = ", ".join(f"`{t}`" for t in target_names)
    brief_parts.append(f"# Context Brief: Edit {target_label}")
    brief_parts.append("")
    brief_parts.append(f"**File:** `{filepath}`  ")
    brief_parts.append(f"**Total lines:** {total_lines}  ")
    brief_parts.append(f"**Generated:** {date.today().isoformat()}")
    brief_parts.append("")
    brief_parts.append("---")
    brief_parts.append("")

    # Task description
    if description:
        brief_parts.append("## Task")
        brief_parts.append("")
        brief_parts.append(description)
        brief_parts.append("")

    # Target sections
    for target_name in target_names:
        if target_name in functions:
            func = functions[target_name]
            brief_parts.append(
                f"## Target Section: `{target_name}` (lines {func['start']}–{func['end']})"
            )
            brief_parts.append("")
            brief_parts.append(f"```{lang}")
            brief_parts.append(func["source"])
            brief_parts.append("```")
            brief_parts.append("")
        else:
            brief_parts.append(
                f"## Target Section: `{target_name}` (NEW — not yet in file)"
            )
            brief_parts.append("")
            brief_parts.append(
                f"Function `{target_name}` does not exist yet. Create it following the pattern below."
            )
            brief_parts.append("")

    # Pattern references
    for pattern_name in pattern_names:
        if pattern_name in functions:
            func = functions[pattern_name]
            brief_parts.append(
                f"## Pattern Reference: `{pattern_name}` (lines {func['start']}–{func['end']})"
            )
            brief_parts.append("")
            brief_parts.append(f"```{lang}")
            brief_parts.append(func["source"])
            brief_parts.append("```")
            brief_parts.append("")
        else:
            brief_parts.append(
                f"## Pattern Reference: `{pattern_name}` — ⚠️ NOT FOUND"
            )
            brief_parts.append("")
            brief_parts.append(
                f"Warning: `{pattern_name}` was not found in the file."
            )
            brief_parts.append("")

    # Type signatures — list all function signatures
    brief_parts.append("## Available Functions (signatures)")
    brief_parts.append("")
    brief_parts.append(f"```{lang}")
    for name, func in sorted(functions.items(), key=lambda x: x[1]["start"]):
        args_str = ", ".join(func["args"]) if func["args"] else "..."
        doc = f'  # {func["docstring"][:80]}' if func["docstring"] else ""
        brief_parts.append(
            f"def {name}({args_str}): ...  # lines {func['start']}–{func['end']}{doc}"
        )
    brief_parts.append("```")
    brief_parts.append("")

    # Edit location — show context around insertion point
    # Use the end of the last target function (or last pattern function) as the insertion area
    insertion_candidates = []
    for name in target_names + pattern_names:
        if name in functions:
            insertion_candidates.append(functions[name]["end"])
    if insertion_candidates:
        insert_line = max(insertion_candidates)
        brief_parts.append(f"## Edit Location (around line {insert_line})")
        brief_parts.append("")
        brief_parts.append("```")
        brief_parts.append(extract_context_lines(lines, insert_line, radius=5))
        brief_parts.append("```")
        brief_parts.append("")

    # DO NOT rules
    brief_parts.append("## ⛔ DO NOT Rules")
    brief_parts.append("")
    brief_parts.append(
        "- **DO NOT** read the full file. Everything you need is in this brief."
    )
    brief_parts.append("- **DO NOT** use explore agents to browse the file.")
    brief_parts.append(
        "- **DO NOT** view/read lines outside the ranges specified above."
    )
    brief_parts.append("- **DO NOT** refactor or modify existing functions.")
    brief_parts.append(
        "- **DO NOT** change function signatures unless explicitly instructed."
    )
    brief_parts.append("")

    return "\n".join(brief_parts)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a context brief for large-file edits.",
        epilog="Example: python generate-context-brief.py --file migrate-project.py --target build_changelog_md --pattern build_vision_md",
    )
    parser.add_argument(
        "--file", "-f", required=True, help="Path to the file to generate a brief for"
    )
    parser.add_argument(
        "--target",
        "-t",
        nargs="+",
        required=True,
        help="Target function name(s) to edit or create",
    )
    parser.add_argument(
        "--pattern",
        "-p",
        nargs="+",
        default=[],
        help="Pattern/reference function name(s) to include",
    )
    parser.add_argument(
        "--description",
        "-d",
        default="",
        help="Task description (what the worker should do)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output file path (default: stdout)",
    )

    args = parser.parse_args()
    filepath = Path(args.file)

    if not filepath.exists():
        print(f"Error: File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    brief = generate_brief(filepath, args.target, args.pattern, args.description)

    if args.output:
        Path(args.output).write_text(brief, encoding="utf-8")
        print(f"Brief written to {args.output}", file=sys.stderr)
    else:
        print(brief)


if __name__ == "__main__":
    main()
